"""Clock, notifications, and explicitly registered Python scripts.

Registered script tools run through the isolated workspace runner. The internal
execute_script helper manages subprocess IO for trusted host-side callers; it is
not exposed as a model tool or as a way around the isolated runner.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

from .registry import string


def current_time() -> dict:
    now = datetime.now().astimezone()
    return {"iso": now.isoformat(), "local": now.isoformat(), "timezone": str(now.tzinfo), "unix": now.timestamp()}


def notify_user(title: str, message: str) -> dict:
    executable = shutil.which("notify-send")
    if executable:
        try:
            completed = subprocess.run([executable, "--", title, message], capture_output=True, timeout=5, check=False)
            if completed.returncode == 0:
                return {"delivered": True, "channel": "desktop"}
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Return the notification for CLI/UI rendering; never corrupt JSON stdout.
    return {"delivered": False, "channel": "tool_result", "title": title, "message": message}


def execute_script(path: Path, arguments: list[str], timeout: float = 30, max_output: int = 20000) -> dict:
    """Run Python without a shell, bounding captured output and child lifetime."""
    process = subprocess.Popen([sys.executable, str(path), *arguments], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total, timed_out, truncated = 0, False, False
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                data = os.read(key.fileobj.fileno(), 4096)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                take = min(len(data), max_output - total)
                output[key.data].extend(data[:take])
                total += take
                if take < len(data):
                    truncated = True
                    break
            if truncated:
                break
        if not timed_out and not truncated:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        # Also reap descendants retaining pipes or surviving a completed script.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return {"returncode": process.returncode, "stdout": output["stdout"].decode("utf-8", errors="replace"),
            "stderr": output["stderr"].decode("utf-8", errors="replace"), "timed_out": timed_out, "truncated": truncated}


def register_system_tools(registry, approve=None, allowed_scripts=None, sandbox=None):
    scripts = {name: Path(path).expanduser().resolve() for name, path in (allowed_scripts or {}).items()}

    def run_script(name: str, arguments: list[str] | None = None) -> dict:
        """Run registered source in the same isolation as model-written code.

        A startup allowlist names useful scripts; it is not a way to bypass
        download or deletion policy with unrestricted host execution.
        """
        if name not in scripts:
            raise PermissionError("Script is not registered")
        path = scripts[name]
        if not path.is_file() or path.suffix != ".py":
            raise ValueError("Registered script must be an existing Python file")
        args = arguments or []
        if sandbox is None:
            raise PermissionError("Registered scripts require the isolated script runner")
        return sandbox.run_code("python", path.read_text(encoding="utf-8"), args)

    registry.add(current_time, "Get the local date, time, UTC offset, and Unix timestamp.", {})
    registry.add(current_time, "Alias of current_time.", {}, name="get_time")
    registry.add(notify_user, "Send a desktop notification or return a message for the terminal/UI if unavailable.", {"title": string(minLength=1, maxLength=200), "message": string(maxLength=4000)}, ["title", "message"])
    if scripts:
        registry.add(run_script, "Run registered Python source in an isolated workspace with no network or host file writes. Use approved tools for downloads and committing output.", {"name": string(enum=sorted(scripts)), "arguments": {"type": "array", "items": string(maxLength=4096), "maxItems": 100}}, ["name"])
