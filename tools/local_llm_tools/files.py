"""File actions confined to configured directories."""
from __future__ import annotations

import itertools
import base64
import hashlib
import os
from pathlib import Path
import tempfile

from .registry import integer, string, validate

MAX_FILE_BYTES = 1_000_000
PATH = string(minLength=1, maxLength=4096)
CONTENT = string(maxLength=MAX_FILE_BYTES)


class FileTools:
    def __init__(self, roots, approve=None, protected_paths=()):
        """Resolve grants once and exclude the agent's own authority/configuration.

        File access is a brokered capability. It is deliberately separate from
        generated-code execution, which uses disposable filesystem snapshots.
        """
        self.roots = tuple(Path(root).expanduser().resolve(strict=True) for root in roots)
        if any(not root.is_dir() for root in self.roots):
            raise ValueError("Allowed roots must be existing directories")
        self.approve = approve or (lambda name, arguments: False)
        self.protected_paths = tuple(Path(p).expanduser().resolve() for p in protected_paths) + (Path(__file__).resolve().parent.parent,)

    def path(self, path: str, *, mutation=False) -> Path:
        validate(path, PATH, "path")
        if not self.roots:
            raise PermissionError("No file roots configured")
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            requested = self.roots[0] / requested
        resolved = requested.resolve()
        if any(resolved == protected or protected in resolved.parents for protected in self.protected_paths):
            raise PermissionError("Agent code and private runtime state are not exposed as file tools")
        if not any(resolved == root or root in resolved.parents for root in self.roots):
            raise PermissionError("Path is outside allowed roots")
        if mutation and (resolved in self.roots or requested.is_symlink()):
            raise PermissionError("Cannot modify an allowed root or a symlink directly")
        return resolved

    @staticmethod
    def regular(path: Path) -> None:
        if not path.is_file():
            raise ValueError("Expected an existing regular file")
        if path.stat().st_nlink > 1:
            raise PermissionError("Files with multiple hard links are not exposed through agent file tools")

    @staticmethod
    def is_host_configuration(path: Path) -> bool:
        """Conservatively identify settings and executable startup locations.

        Ordinary workspace edits are automatic. Shell startup files, user config,
        credentials, executable paths and system settings need a human decision
        because they can affect accounts or execute later outside the sandbox.
        """
        system = [Path(p) for p in ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/var")]
        if any(path == root or root in path.parents for root in system):
            return True
        home = Path.home().resolve()
        if path.is_relative_to(home):
            relative = path.relative_to(home)
            # This deliberately covers more than just known shell init filenames:
            # newly introduced hidden configuration files must not bypass policy.
            return bool(relative.parts and relative.parts[0].startswith("."))
        return False

    def _approve_configuration_write(self, target, data, mode):
        if not self.is_host_configuration(target):
            return
        try:
            preview = data.decode("utf-8")
        except UnicodeError as error:
            raise PermissionError("Binary host configuration changes are not supported through file tools") from error
        if len(preview) > 20_000:
            raise PermissionError("Host configuration changes must be small enough to review completely")
        arguments = {"path": str(target), "mode": mode, "bytes": len(data), "content": preview,
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "purpose": "Change host configuration or an executable startup location"}
        if not self.approve("change_host_configuration", arguments):
            raise PermissionError("Host configuration change was not approved")

    def list_files(self, path: str = ".", limit: int = 100, offset: int = 0) -> dict:
        """Page directory entries without materializing an arbitrarily large tree."""
        validate(limit, integer(1, 500, 100), "limit")
        validate(offset, integer(0, 1_000_000, 0), "offset")
        target = self.path(path)
        entries = []
        with os.scandir(target) as iterator:
            for entry in itertools.islice(iterator, offset, offset + limit + 1):
                kind = "symlink" if entry.is_symlink() else "directory" if entry.is_dir(follow_symlinks=False) else "file"
                entries.append({"name": entry.name, "kind": kind})
        return {"path": str(target), "entries": entries[:limit], "truncated": len(entries) > limit,
                "next_offset": offset + min(len(entries), limit), "note": "Directory changes can shift pagination offsets."}

    def file_info(self, path: str) -> dict:
        """Inspect metadata before choosing text, binary, or image handling."""
        target = self.path(path)
        info = target.stat()
        return {"path": str(target), "kind": "directory" if target.is_dir() else "file",
                "bytes": info.st_size, "modified": info.st_mtime}

    def read_bytes(self, path: str, offset: int = 0, length: int = 16384) -> dict:
        """Read a bounded binary slice; base64 preserves arbitrary file contents."""
        validate(offset, integer(0, 2**63 - 1, 0), "offset")
        validate(length, integer(1, 65536, 16384), "length")
        target = self.path(path)
        self.regular(target)
        with target.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(length)
        return {"path": str(target), "base64": base64.b64encode(data).decode(),
                "next_offset": offset + len(data), "eof": offset + len(data) >= target.stat().st_size}

    def write_bytes(self, path: str, base64_data: str, mode: str = "create_only") -> dict:
        """Create/replace binary data under the same path policy as text writes."""
        data = base64.b64decode(base64_data, validate=True)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Binary write exceeds 1 MB per-call limit")
        return self.save_bytes(path, data, mode)

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> dict:
        """Read human-readable UTF-8 text; read_bytes handles long or binary data."""
        validate(start_line, integer(1, 1_000_000, 1), "start_line")
        validate(max_lines, integer(1, 1000, 200), "max_lines")
        target = self.path(path)
        self.regular(target)
        with target.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Text file exceeds 1 MB limit")
        lines = raw.decode("utf-8").splitlines(keepends=True)
        selected = "".join(lines[start_line - 1:start_line - 1 + max_lines])
        return {"path": str(target), "start_line": start_line, "text": selected[:20_000], "total_lines": len(lines), "truncated": len(selected) > 20_000 or start_line - 1 + max_lines < len(lines)}

    def search_files(self, path: str, query: str, limit: int = 20) -> dict:
        """Search literal text with bounded traversal, skipping inaccessible data."""
        validate(query, string(minLength=1, maxLength=500), "query")
        validate(limit, integer(1, 100, 20), "limit")
        target = self.path(path)
        if not target.exists():
            raise FileNotFoundError(str(target))
        candidates = [target] if target.is_file() else target.rglob("*")
        matches, examined, truncated = [], 0, False
        for candidate in candidates:
            examined += 1
            if examined > 5000:
                break
            try:
                file = self.path(str(candidate))
                if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
                    if file.is_file():
                        truncated = True
                    continue
                self.regular(file)
                with file.open("rb") as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
                    continue
                for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
                    if query.casefold() in line.casefold():
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
                        matches.append({"file": str(file), "line": number, "text": line[:500], "text_truncated": len(line) > 500})
            except (OSError, UnicodeError):
                continue
        return {"matches": matches, "truncated": truncated or examined > 5000}

    def save_bytes(self, path: str, data: bytes, mode: str = "create_only") -> dict:
        """Commit brokered output; replacements are staged before atomic rename.

        Downloads and sandbox commits share this path enforcement. Callers own
        download approval and size limits; this helper never performs networking.
        """
        if not isinstance(data, bytes):
            raise ValueError("data must be bytes")
        validate(mode, string(enum=["create_only", "append", "overwrite"]), "mode")
        target = self.path(path, mutation=True)
        if target.exists():
            self.regular(target)
        self._approve_configuration_write(target, data, mode)
        if mode == "create_only":
            with target.open("xb") as stream:
                stream.write(data)
        elif mode == "append":
            with target.open("ab") as stream:
                stream.write(data)
        elif mode == "overwrite":
            descriptor, temporary = tempfile.mkstemp(prefix=".agent-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                if target.exists():
                    os.chmod(temporary, target.stat().st_mode & 0o777)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            raise ValueError("Unknown write mode")
        return {"path": str(target), "bytes_written": len(data), "mode": mode}

    def write_text(self, path: str, content: str, mode: str = "overwrite") -> dict:
        validate(content, CONTENT, "content")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Content exceeds 1 MB limit")
        return self.save_bytes(path, data, mode)

    def create_file(self, path: str, content: str = "") -> dict:
        return self.write_text(path, content, "create_only")

    def write_file(self, path: str, content: str) -> dict:
        return self.write_text(path, content)

    def append_file(self, path: str, content: str) -> dict:
        return self.write_text(path, content, "append")

    def make_directory(self, path: str) -> dict:
        target = self.path(path, mutation=True)
        if self.is_host_configuration(target) and not target.exists():
            if not self.approve("change_host_configuration", {"path": str(target), "operation": "make_directory",
                                                               "purpose": "Create a host configuration directory"}):
                raise PermissionError("Host configuration change was not approved")
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        return {"path": str(target), "created": not existed}

    def delete_file(self, path: str) -> dict:
        """Delete exactly the approved, unchanged file and never follow a symlink."""
        target = self.path(path, mutation=True)
        self.regular(target)
        original = target.stat()
        if not self.approve("delete_file", {"path": str(target), "bytes": original.st_size,
                                             "modified_ns": original.st_mtime_ns, "inode": original.st_ino,
                                             "purpose": "Permanently remove this file"}):
            raise PermissionError("Deletion was not approved")
        if self.path(path, mutation=True) != target:
            raise PermissionError("Path changed during approval")
        current = target.stat()
        if (original.st_dev, original.st_ino, original.st_mtime_ns, original.st_size) != (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size):
            raise PermissionError("File changed during approval")
        target.unlink()
        return {"path": str(target), "deleted": True}

    def register(self, registry, writes):
        registry.add(self.list_files, "List a directory. Relative paths use the first allowed root; use next_offset for more entries.", {"path": PATH, "limit": integer(1, 500, 100), "offset": integer(0, 1_000_000, 0)})
        registry.add(self.file_info, "Inspect a path's type, byte size and modification time.", {"path": PATH}, ["path"])
        registry.add(self.read_bytes, "Read a bounded slice of any regular file as base64. Useful for binary files or very long text lines.",
                     {"path": PATH, "offset": integer(0, 2**63 - 1, 0), "length": integer(1, 65536, 16384)}, ["path"])
        registry.add(self.read_file, "Read a UTF-8 text file with line pagination and a 20,000 character output cap; files must be at most 1 MB.", {"path": PATH, "start_line": integer(1, 1_000_000, 1), "max_lines": integer(1, 1000, 200)}, ["path"])
        search = {"path": PATH, "query": string(minLength=1, maxLength=500), "limit": integer(1, 100, 20)}
        registry.add(self.search_files, "Search file contents for case-insensitive literal text. Examines up to 5000 entries.", search, ["path", "query"])
        registry.add(self.search_files, "Alias of search_files.", search, ["path", "query"], name="search_text")
        if not writes:
            return
        registry.add(self.write_bytes, "Write base64-encoded binary data, up to 1 MB per call. Defaults to exclusive creation.",
                     {"path": PATH, "base64_data": string(maxLength=1_400_000), "mode": string(enum=["create_only", "overwrite", "append"])}, ["path", "base64_data"])
        for name, description, required in [
            ("create_file", "Create a UTF-8 text file; fail if it exists.", ["path"]),
            ("write_file", "Replace a text file completely, or create it if missing.", ["path", "content"]),
            ("append_file", "Append text to a file, creating it if missing.", ["path", "content"]),
        ]:
            registry.add(getattr(self, name), description, {"path": PATH, "content": CONTENT}, required)
        registry.add(self.write_text, "Create, overwrite, or append text (compatibility tool).", {"path": PATH, "content": CONTENT, "mode": string(enum=["overwrite", "append", "create_only"])}, ["path", "content"])
        registry.add(self.make_directory, "Create a directory and missing parents.", {"path": PATH}, ["path"])
        registry.add(self.delete_file, "Permanently delete one regular file after user approval. Does not delete directories.", {"path": PATH}, ["path"])
