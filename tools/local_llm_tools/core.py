"""Compose tools independently of the model provider."""
from pathlib import Path
from typing import Callable

from .registry import ToolRegistry
from .files import FileTools
from .system import register_system_tools
from .web import register_web_tools


def default_tools(
    allowed_roots: list[Path] | tuple[Path, ...] = (), *,
    allow_writes: bool = False, allow_internet: bool = True,
    approve: Callable[[str, dict], bool] | None = None,
    allowed_scripts: dict[str, Path] | None = None,
    searxng_url: str | None = None,
    config: dict | None = None,
) -> ToolRegistry:
    """Build the file/network tools with the same authority boundaries everywhere.

    Runtime state and application code cannot be edited or read through model
    file tools, even when a broad parent folder is allowed. This prevents an
    agent from granting itself approval by editing a database or its own code.
    """
    from .runtime import DEFAULT_STATE_DIR
    config = config or {}
    registry = ToolRegistry()
    files = FileTools(allowed_roots, approve, protected_paths=[config.get("state_dir") or DEFAULT_STATE_DIR])
    files.register(registry, allow_writes)
    sandbox = None
    if allowed_scripts:
        from .sandbox import ScriptSandbox
        sandbox = ScriptSandbox(config.get("state_dir") or DEFAULT_STATE_DIR, files)
    register_system_tools(registry, approve, allowed_scripts or {}, sandbox=sandbox)
    if allow_internet:
        register_web_tools(registry, files if allow_writes else None, searxng_url)
    return registry
