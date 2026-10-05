"""Loopback-only dashboard with bearer authentication and no external assets.

The token is entered by the human and retained only in this page's memory. Host
and Origin checks additionally prevent DNS rebinding and cross-site requests.
All model-derived strings are displayed as text, never interpreted as HTML.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def auth_token(state_dir):
    path = Path(state_dir) / "dashboard.token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
    return token


def make_server(store, runtime, scheduler, host="127.0.0.1", port=8765):
    """Build but do not start the server, allowing a caller to manage its lifetime."""
    if host != "127.0.0.1":
        raise ValueError("Dashboard must bind to 127.0.0.1")
    token = auth_token(store.state_dir)
    static = Path(__file__).with_name("static")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # URLs and request bodies can contain private context. Application
            # events are already recorded through the structured runtime store.
            pass

        def respond(self, status, payload, content_type="application/json"):
            data = json.dumps(payload).encode() if content_type == "application/json" else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def check_origin(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                raise PermissionError("Use the dashboard's 127.0.0.1 address")
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + expected:
                raise PermissionError("Cross-origin access denied")

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route()

        def route(self):
            try:
                self.check_origin()
                parsed = urlsplit(self.path)
                if self.command == "GET" and parsed.path in {"/", "/app.js", "/style.css"}:
                    name, mime = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}[parsed.path]
                    return self.respond(200, (static / name).read_bytes(), mime)
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise PermissionError("Enter the dashboard token to continue")
                data = {}
                if self.command == "POST":
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("Expected application/json")
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 65536:
                        raise ValueError("Request must be 1 to 65536 bytes")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("Expected a JSON object")
                query = parse_qs(parsed.query)
                path = parsed.path
                if self.command == "GET":
                    if path == "/api/tasks":
                        result = store.list()
                    elif path == "/api/events":
                        result = runtime.events(after_id=int(query.get("after", [0])[0]), limit=200)
                    elif path == "/api/approvals":
                        result = runtime.approvals()
                    elif path == "/api/memories":
                        from .memory import MemoryStore
                        result = MemoryStore(store.state_dir).search(query.get("owner", ["main"])[0], query.get("q", [""])[0], scope="shared", limit=100)
                    else:
                        return self.respond(404, {"error": "Unknown endpoint"})
                elif path == "/api/tasks":
                    # HTTP clients may choose prompts and timing, but cannot expand
                    # filesystem access or change the daemon's security policy.
                    result = store.add(data["prompt"], scheduler.base_config, data.get("at"), data.get("interval"), data.get("watch"))
                elif path == "/api/control":
                    result = store.control(data["id"], data["action"])
                    runtime.event("task_control", {"action": data["action"]}, task_id=data["id"])
                elif path == "/api/recover":
                    from .service import recover_task
                    result = recover_task(store, data["id"], data["note"], scheduler)
                elif path == "/api/compact":
                    from .service import compact_task
                    result = compact_task(store, data["id"])
                elif path == "/api/approval":
                    if type(data.get("approved")) is not bool:
                        raise ValueError("approved must be boolean")
                    result = runtime.decide(data["id"], data["approved"])
                elif path in {"/api/memory/update", "/api/memory/forget"}:
                    from .memory import MemoryStore
                    memory = MemoryStore(store.state_dir)
                    if path.endswith("update"):
                        result = memory.update(data["owner"], data["id"], data["content"])
                    else:
                        result = memory.forget(data["owner"], data["id"])
                    runtime.event("memory_user_edit", {"action": path.rsplit("/", 1)[-1], "id": data["id"]})
                else:
                    return self.respond(404, {"error": "Unknown endpoint"})
                self.respond(200, result)
            except PermissionError as exc:
                self.respond(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception as exc:
                runtime.event("dashboard_error", {"error": type(exc).__name__})
                self.respond(500, {"error": "Request failed; inspect service activity"})

    return ThreadingHTTPServer((host, port), Handler)
