"""Wayland desktop observation and approval-gated input for Omarchy.

Screenshots are local captures, not downloads. Inputs can affect any application,
so every click, key and text insertion is individually approved by the operator.
Helpers are discovered at use time; this module never installs dependencies.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import uuid

from .capabilities import require_approval
from .registry import integer, string


class DesktopTools:
    def __init__(self, state_dir, approve):
        self.artifacts = Path(state_dir) / "artifacts"
        self.approve = approve

    @staticmethod
    def _executable(name):
        path = shutil.which(name)
        if not path:
            raise RuntimeError(f"Desktop helper {name!r} is missing; installation requires your approval")
        return path

    def _run(self, name, args):
        result = subprocess.run([self._executable(name), *args], capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(f"{name} failed: {result.stderr.decode(errors='replace')[:2000]}")
        return result.stdout.decode(errors="replace")[:20000]

    def desktop_screenshot(self):
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.artifacts / f"desktop-{uuid.uuid4().hex}.png"
        self._run("grim", [str(path)])
        # OCR is optional; an image artifact remains available to the operator or
        # a vision-capable frontend even when OCR is not installed.
        text = self._run("tesseract", [str(path), "stdout"]) if shutil.which("tesseract") else None
        return {"path": str(path), "image_path": str(path), "text": text, "kind": "local_screen_capture"}

    def _active_window(self):
        """Bind operator approval to the focused application, not just raw keys."""
        window = json.loads(self._run("hyprctl", ["activewindow", "-j"]))
        return {key: window.get(key) for key in ("address", "class", "title")}

    def _same_window(self, original):
        if self._active_window().get("address") != original.get("address"):
            raise PermissionError("Focused application changed during approval; inspect the desktop again")

    def desktop_type(self, text: str):
        self._executable("wtype")
        window = self._active_window()
        require_approval(self.approve, "desktop_type", {"text": text, "window": window,
                         "effect": "Type this exact text into the focused application; application shortcuts or submissions may have external effects"})
        self._same_window(window)
        self._run("wtype", ["--", text])
        return {"typed_characters": len(text)}

    def desktop_key(self, key: str):
        self._executable("wtype")
        window = self._active_window()
        require_approval(self.approve, "desktop_key", {"key": key, "window": window, "effect": "Send this key to the focused application; may submit or confirm an action"})
        self._same_window(window)
        self._run("wtype", ["-k", key])
        return {"key": key, "sent": True}

    def desktop_click(self, x: int, y: int, button: str = "left"):
        self._executable("hyprctl")
        self._executable("ydotool")
        window = self._active_window()
        require_approval(self.approve, "desktop_click", {"window_before_click": window, "x": x, "y": y, "button": button,
                         "effect": "Move the pointer and click the application at these screen coordinates; may trigger a sensitive action"})
        self._same_window(window)
        self._run("hyprctl", ["dispatch", "movecursor", str(x), str(y)])
        self._run("ydotool", ["click", {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[button]])
        return {"clicked": True, "x": x, "y": y, "button": button}

    def register(self, registry):
        registry.add(self.desktop_screenshot, "Capture the Wayland desktop to a local PNG and extract text when tesseract is installed.", {})
        registry.add(self.desktop_type, "Type text into the focused desktop application after specific user approval.", {"text": string(maxLength=10000)}, ["text"])
        registry.add(self.desktop_key, "Send a key name such as Return or Escape after user approval; requires wtype.", {"key": string(minLength=1, maxLength=80)}, ["key"])
        registry.add(self.desktop_click, "Click screen coordinates after approval; requires Hyprland and a running ydotool daemon.",
                     {"x": integer(0, 32000, 0), "y": integer(0, 32000, 0), "button": string(enum=["left", "right", "middle"])}, ["x", "y"])
