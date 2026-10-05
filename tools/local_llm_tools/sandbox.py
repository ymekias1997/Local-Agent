"""Run generated programs in disposable Linux namespaces, then explicitly export files.

The agent may write arbitrary code, so Python-level restrictions are insufficient.
Bubblewrap receives copies of regular input files, never live project mounts or
agent state. Networking, credentials, desktop sockets and host process IDs are
absent. Only the separate commit tool can write a result back to a configured root.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import time
import uuid

from .registry import string


class ScriptSandbox:
    def __init__(self, state_dir, files):
        self.directory = Path(state_dir).expanduser().resolve() / "sandbox"
        self.files = files
        self.excluded = (Path(state_dir).expanduser().resolve(), Path(__file__).resolve().parent.parent)

    def _snapshot(self, target):
        """Bound copy cost and skip links, sockets and devices, including hardlinks."""
        size = count = 0
        for index, root in enumerate(self.files.roots):
            if any(root == excluded or root.is_relative_to(excluded) for excluded in self.excluded):
                continue
            dest = target / f"root{index}"
            dest.mkdir()
            for parent, directories, filenames in os.walk(root, followlinks=False):
                directories[:] = [name for name in directories
                                  if not (Path(parent) / name).is_symlink()
                                  and not any((Path(parent) / name) == excluded or (Path(parent) / name).is_relative_to(excluded)
                                              for excluded in self.excluded)]
                relative = Path(parent).relative_to(root)
                (dest / relative).mkdir(parents=True, exist_ok=True)
                for name in filenames:
                    source = Path(parent) / name
                    info = source.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        continue
                    count += 1
                    size += info.st_size
                    if count > 5000 or size > 100_000_000:
                        raise ValueError("Sandbox inputs exceed 5000 files or 100 MB; configure a smaller root")
                    # O_NOFOLLOW also rejects a symlink substituted during traversal.
                    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "rb") as stream:
                        current = os.fstat(stream.fileno())
                        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                            continue
                        data = stream.read(100_000_001 - size + info.st_size)
                    if len(data) + size - info.st_size > 100_000_000:
                        raise ValueError("Sandbox inputs grew beyond 100 MB")
                    (dest / relative / name).write_bytes(data)

    def run_code(self, language: str, code: str, arguments=None) -> dict:
        if language not in {"python", "bash"} or len(code.encode()) > 1_000_000:
            raise ValueError("Use python or bash with code up to 1 MB")
        bwrap, prlimit = shutil.which("bwrap"), shutil.which("prlimit")
        if not bwrap or not prlimit:
            raise RuntimeError("Isolated execution requires bubblewrap and util-linux (prlimit); no host fallback")
        run_id = uuid.uuid4().hex
        run_dir = self.directory / run_id
        workspace, inputs = run_dir / "workspace", run_dir / "inputs"
        workspace.mkdir(parents=True, mode=0o700)
        inputs.mkdir()
        self._snapshot(inputs)
        script = run_dir / ("program.py" if language == "python" else "program.sh")
        script.write_text(code, encoding="utf-8")
        command = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                   "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/workspace",
                   "--ro-bind", "/usr", "/usr"]
        for name in ("bin", "sbin", "lib", "lib64"):
            source = Path("/") / name
            if source.is_symlink():
                command += ["--symlink", os.readlink(source), str(source)]
            elif source.exists():
                command += ["--ro-bind", str(source), str(source)]
        command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                    "--ro-bind", str(inputs), "/inputs", "--bind", str(workspace), "/workspace",
                    "--ro-bind", str(script), "/program", "--chdir", "/workspace", "--",
                    prlimit, "--cpu=30", "--as=1073741824", "--fsize=10485760", "--nproc=64", "--",
                    "/usr/bin/python3" if language == "python" else "/usr/bin/bash", "/program", *(arguments or [])]
        # Output is drained incrementally so an infinite print loop cannot fill RAM.
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        deadline, total, truncated, timed_out = time.monotonic() + 35, 0, False, False
        try:
            for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                for key, _ in selector.select(0.1):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    take = min(len(data), 20000 - total)
                    output[key.data].extend(data[:take])
                    total += take
                    truncated |= take < len(data)
                if truncated:
                    break
            if not timed_out and not truncated:
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            selector.close()
            process.stdout.close()
            process.stderr.close()
        return {"run_id": run_id, "returncode": process.returncode, "timed_out": timed_out,
                "truncated": truncated, **{key: value.decode(errors="replace") for key, value in output.items()},
                "workspace": str(workspace), "inputs": "/inputs/root0, /inputs/root1, ... (read only copies)"}

    def run_command(self, command: str) -> dict:
        """Commands share the exact same namespace isolation as generated scripts."""
        return self.run_code("bash", command)

    def commit_sandbox_file(self, run_id: str, relative_path: str, destination: str) -> dict:
        if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
            raise ValueError("Invalid sandbox run ID")
        workspace = self.directory / run_id / "workspace"
        source = workspace / relative_path
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(workspace) or any(p.is_symlink() for p in [source, *source.parents] if p != workspace.parent):
            raise PermissionError("Only ordinary files inside this run's workspace can be committed")
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1_000_000:
            raise ValueError("Commit requires a regular file of at most 1 MB without hardlinks")
        return self.files.save_bytes(destination, resolved.read_bytes(), "overwrite")

    def register(self, registry):
        registry.add(self.run_code, "Execute generated code without network or host writes. Inputs are copied under /inputs/rootN; write outputs under /workspace. CPU 30s, wall 35s, output 20 KB.",
                     {"language": string(enum=["python", "bash"]), "code": string(maxLength=1_000_000),
                      "arguments": {"type": "array", "items": string(maxLength=4096), "maxItems": 100}}, ["language", "code"])
        registry.add(self.run_command, "Execute a Bash command in the same disposable no-network sandbox as run_code.",
                     {"command": string(maxLength=1_000_000)}, ["command"])
        registry.add(self.commit_sandbox_file, "Copy a generated sandbox file to an allowed file root. Existing destination is overwritten.",
                     {key: string(minLength=1, maxLength=4096) for key in ("run_id", "relative_path", "destination")},
                     ["run_id", "relative_path", "destination"])
