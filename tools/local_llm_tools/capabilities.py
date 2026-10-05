"""Brokered Ollama management and optional local action providers.

All URLs and runtime directories originate in operator configuration. Models can
choose model names but cannot redirect the Ollama broker to an arbitrary service.
Model pulls and deletion consume a per-action approval before making any request.
API reference: https://github.com/ollama/ollama/blob/main/docs/api.md
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .registry import string


class NoRedirect(HTTPRedirectHandler):
    """A model server cannot redirect a trusted control request elsewhere."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PermissionError("Ollama API redirects are disabled")


def require_approval(approve, action, arguments):
    """Preserve ApprovalRequired exceptions so the runtime can pause and resume."""
    if approve is None or approve(action, arguments) is not True:
        raise PermissionError(f"Explicit approval is required for {action}")


class OllamaTools:
    def __init__(self, config, approve=None, agent=None):
        self.base_url = config.get("base_url", "http://localhost:11434").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Ollama URL must be HTTP(S) without credentials, query or fragment")
        self.approve, self.agent = approve, agent
        self.no_internet = config.get("no_internet", False)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _request(self, endpoint, body=None, method=None, timeout=120):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base_url + "/api/" + endpoint, data=data,
                          headers={"Content-Type": "application/json"}, method=method)
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise RuntimeError("Ollama response exceeds 4 MB")
        result = json.loads(raw) if raw.strip() else {"ok": True}
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result

    def ollama_list(self):
        return self._request("tags")

    def ollama_status(self):
        return self._request("ps")

    def ollama_show(self, model: str):
        return self._request("show", {"model": model})

    def _installed(self, model):
        models = self.ollama_list().get("models", [])
        names = {item.get("name", item.get("model")) for item in models}
        if model not in names and model + ":latest" not in names:
            raise ValueError("Model is not installed; request an approved ollama_pull first")

    def ollama_pull(self, model: str):
        if self.no_internet:
            raise PermissionError("Model downloads are disabled by no_internet")
        require_approval(self.approve, "ollama_pull", {
            "model": model, "server": self.base_url, "source": "Ollama model registry",
            "destination": "The configured Ollama server's model storage", "size": "unknown until registry transfer",
            "effect": "Download model files; consumes bandwidth and disk; does not execute a prompt"})
        return self._request("pull", {"model": model, "stream": False}, timeout=3600)

    def ollama_delete(self, model: str):
        require_approval(self.approve, "ollama_delete", {"model": model, "server": self.base_url,
                         "effect": "Permanently remove this model from Ollama storage; re-download requires approval"})
        return self._request("delete", {"model": model}, method="DELETE")

    def ollama_load(self, model: str):
        self._installed(model)
        return self._request("generate", {"model": model, "stream": False, "keep_alive": "5m"})

    def ollama_unload(self, model: str):
        self._installed(model)
        return self._request("generate", {"model": model, "stream": False, "keep_alive": 0})

    def ollama_switch(self, model: str):
        self._installed(model)
        if self.agent is None:
            raise RuntimeError("Model switching requires an attached agent runtime")
        metadata = self.ollama_show(model)
        capabilities = metadata.get("capabilities") if isinstance(metadata, dict) else None
        if isinstance(capabilities, list):
            # Match the next request to what the selected model can actually do.
            # Older servers without capability metadata retain operator settings.
            self.agent.config["vision"] = "vision" in capabilities
            self.agent.config["enable_tools"] = "tools" in capabilities
        return self.agent.switch_model(model)

    def register(self, registry):
        for name, description in (
            ("ollama_list", "List installed Ollama models."), ("ollama_status", "List models currently loaded in Ollama."),
            ("ollama_show", "Inspect a model's metadata and capabilities."),
            ("ollama_pull", "Download an Ollama model only after explicit user approval; may take several minutes."),
            ("ollama_delete", "Permanently remove a model only after explicit user approval."),
            ("ollama_load", "Load an installed model into memory without generating text."),
            ("ollama_unload", "Unload an installed model to release memory."),
            ("ollama_switch", "Switch this agent's model while preserving its working context."),
        ):
            if name == "ollama_pull" and self.no_internet:
                continue
            takes_model = name not in {"ollama_list", "ollama_status"}
            registry.add(getattr(self, name), description,
                         {"model": string(minLength=1, maxLength=300)} if takes_model else {},
                         ["model"] if takes_model else [])


def capability_status():
    """Report installed helpers without installing or starting anything."""
    names = ("bwrap", "prlimit", "chromium", "chromium-browser", "grim", "wtype", "ydotool", "hyprctl", "tesseract")
    return {"executables": {name: shutil.which(name) for name in names},
            "script_policy": "Isolated regular-file snapshots; no network; explicit output commits",
            "desktop_policy": "Every input action requires approval", "browser_policy": "Fresh profile; approved interactions; downloads denied"}


def register_capability_tools(registry, config, approve, files, agent=None):
    """Attach independent brokers; dependency errors surface only when used."""
    from .sandbox import ScriptSandbox
    from .desktop import DesktopTools
    from .browser import BrowserTools
    state = Path(config.get("state_dir") or Path.home() / ".local/state/local-agent")
    OllamaTools(config, approve, agent).register(registry)
    registry.add(capability_status, "Report installed capabilities and missing optional dependencies.", {})
    if not config.get("read_only", False):
        ScriptSandbox(state, files).register(registry)
    DesktopTools(state, approve).register(registry)
    if not config.get("no_internet", False):
        BrowserTools(state, approve).register(registry)
