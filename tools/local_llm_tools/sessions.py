"""Local child agents, backed by a SQLite inbox and a Bash-friendly CLI."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from .registry import integer, string
from .runtime import RuntimeStore
from .memory import MemoryStore, redact

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "local-llm-tools"
MAX_CHILDREN = 4


def process_identity(pid: int) -> str | None:
    """Linux process start ticks distinguish a worker from a reused PID."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        fields = data[data.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


class SessionManager:
    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR, config: dict | None = None):
        self.state_dir = Path(state_dir or DEFAULT_STATE_DIR).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = config or {}
        self._processes = {}
        self.runtime = RuntimeStore(self.state_dir)
        self.max_children = self.config.get("max_children", MAX_CHILDREN)
        if type(self.max_children) is not int or not 1 <= self.max_children <= 16:
            raise ValueError("max_children must be an integer from 1 to 16")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, pid INTEGER, identity TEXT,
                    status TEXT NOT NULL, config TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, reply_to INTEGER
                );
                CREATE INDEX IF NOT EXISTS messages_inbox
                    ON messages(session_id, role, status, id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "approval_id" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN approval_id TEXT")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.state_dir / "sessions.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def locked(self):
        with (self.state_dir / "launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _row(self, session_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{12}", session_id):
            raise ValueError("Invalid session ID")
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown session: {session_id}")
        saved_config = json.loads(row["config"])
        if self.config.get("agent_id") and saved_config.get("parent_agent_id") != self.config["agent_id"]:
            raise PermissionError("An agent can access only children it created")
        return dict(row)

    def status(self, session_id: str) -> dict:
        row = self._row(session_id)
        child = self._processes.get(session_id)
        if child is not None and child.poll() is not None:
            self._processes.pop(session_id, None)
        if row["status"] == "starting" and not row["pid"] and time.time() - row["created"] > 10:
            with self.connect() as db:
                db.execute("UPDATE sessions SET status='failed', updated=? WHERE id=?", (time.time(), session_id))
            row["status"] = "failed"
        if row["status"] not in {"stopped", "failed"} and row["pid"]:
            if process_identity(row["pid"]) != row["identity"]:
                with self.connect() as db:
                    db.execute("UPDATE sessions SET status='stopped', updated=? WHERE id=?",
                               (time.time(), session_id))
                    db.execute("UPDATE messages SET status='cancelled' WHERE session_id=? AND status IN ('queued','running')",
                               (session_id,))
                row["status"] = "stopped"
        return {key: row[key] for key in ("id", "pid", "status", "created", "updated")}

    def list(self) -> list[dict]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM sessions ORDER BY created")]
        result = []
        for session_id in ids:
            try:
                result.append(self.status(session_id))
            except PermissionError:
                continue
        return result

    def start(self, task: str = "", model=None, memory_mode="fresh", context="", memory_ids=None, enable_tools=True, vision=None) -> dict:
        if os.environ.get("LOCAL_LLM_AGENT_CHILD") == "1" or self.config.get("children") is False:
            raise PermissionError("Child agents cannot spawn more agents")
        model = model or self.config.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A model is required to start an agent")
        if memory_mode not in {"fresh", "shared", "selected"}:
            raise ValueError("memory_mode must be fresh, shared, or selected")
        if not isinstance(context, str) or len(context) > 32000:
            raise ValueError("context must be a string of at most 32000 characters")
        if type(enable_tools) is not bool:
            raise ValueError("enable_tools must be boolean")
        if vision is not None and type(vision) is not bool:
            raise ValueError("vision must be boolean")
        if vision is None:
            vision = bool(self.config.get("vision", False)) if model == self.config.get("model") else False
        if redact(context) != context or redact(task) != task:
            raise ValueError("Do not put credential-like content in child tasks or context")
        if memory_mode == "fresh" and (context or memory_ids):
            raise ValueError("Fresh children receive only their task; use selected mode to supply background")
        allowed_ids = MemoryStore(self.state_dir).accessible_ids(self.config.get("agent_id", "main"), scope=self.config.get("memory_scope", "private"), selected=self.config.get("memory_ids", []))
        memory_ids = memory_ids or []
        if not isinstance(memory_ids, list) or any(not isinstance(item, str) for item in memory_ids):
            raise ValueError("memory_ids must be an array of strings")
        if set(memory_ids) - allowed_ids:
            raise PermissionError("Children may receive only memories accessible to the parent")
        if not isinstance(task, str) or len(task) > 32_000 or (task and not task.strip()):
            raise ValueError("Task must be empty or contain 1–32,000 characters")
        for root in self.config.get("roots", []):
            if not Path(root).expanduser().is_dir():
                raise ValueError(f"File root must be an existing directory: {root}")
        with self.locked():
            active = [row for row in self.list() if row["status"] not in {"stopped", "failed"}]
            if len(active) >= self.max_children:
                raise ValueError(f"At most {self.max_children} child agents may run; stop an existing child first")
            session_id = uuid.uuid4().hex[:12]
            config = dict(self.config, children=False, yes=False, model=model,
                          state_dir=str(self.state_dir), agent_id="child-" + session_id,
                          parent_agent_id=self.config.get("agent_id", "main"),
                          memory_scope={"fresh": "private", "selected": "selected", "shared": "shared"}[memory_mode],
                          initial_context=context if memory_mode != "fresh" else "",
                          memory_ids=sorted(allowed_ids) if memory_mode == "shared" else memory_ids if memory_mode != "fresh" else [], enable_tools=enable_tools, vision=vision)
            config.pop("task_id", None)
            now = time.time()
            with self.connect() as db:
                db.execute("INSERT INTO sessions VALUES (?,NULL,NULL,'starting',?,?,?)",
                           (session_id, json.dumps(config, default=str), now, now))
            env = dict(os.environ, LOCAL_LLM_AGENT_CHILD="1")
            package_root = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
            child = None
            try:
                with (self.state_dir / f"{session_id}.log").open("ab") as log:
                    child = subprocess.Popen(
                        [sys.executable, "-m", "local_llm_tools.worker", str(self.state_dir), session_id],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        env=env, start_new_session=True,
                    )
                self._processes[session_id] = child
                identity = process_identity(child.pid)
                with self.connect() as db:
                    db.execute("UPDATE sessions SET pid=?,identity=?,updated=? WHERE id=?",
                               (child.pid, identity, time.time(), session_id))
                if task:
                    self.send(session_id, task)
            except Exception:
                if child is not None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
                    self._processes.pop(session_id, None)
                with self.connect() as db:
                    db.execute("UPDATE sessions SET status='failed' WHERE id=?", (session_id,))
                raise
            self.runtime.event("session_spawn", {"session_id": session_id, "model": model, "memory_mode": memory_mode, "enable_tools": enable_tools}, agent_id=self.config.get("agent_id", "main"))
            return self.status(session_id)

    def resume(self, session_id):
        """Restart a stopped process with its original identity and durable context.

        Cancelled tasks remain cancelled; pending approvals remain pending. An
        interrupted action is never replayed automatically by the Agent layer.
        """
        if os.environ.get("LOCAL_LLM_AGENT_CHILD") == "1" or self.config.get("children") is False:
            raise PermissionError("Child agents cannot restart other agents")
        with self.locked():
            row = self._row(session_id)
            if self.status(session_id)["status"] not in {"stopped", "failed"}:
                return self.status(session_id)
            if len([item for item in self.list() if item["status"] not in {"stopped", "failed"}]) >= self.max_children:
                raise ValueError(f"At most {self.max_children} child agents may run")
            with self.connect() as db:
                db.execute("UPDATE sessions SET status='starting',pid=NULL,identity=NULL,updated=? WHERE id=?", (time.time(), session_id))
            env = dict(os.environ, LOCAL_LLM_AGENT_CHILD="1")
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
            try:
                with (self.state_dir / f"{session_id}.log").open("ab") as log:
                    child = subprocess.Popen([sys.executable, "-m", "local_llm_tools.worker", str(self.state_dir), session_id], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
                self._processes[session_id] = child
                with self.connect() as db:
                    db.execute("UPDATE sessions SET pid=?,identity=?,updated=? WHERE id=?", (child.pid, process_identity(child.pid), time.time(), session_id))
            except Exception:
                with self.connect() as db:
                    db.execute("UPDATE sessions SET status='failed' WHERE id=?", (session_id,))
                raise
            self.runtime.event("session_resume", {"session_id": session_id}, agent_id=self.config.get("agent_id", "main"))
            return self.status(session_id)

    def send(self, session_id: str, message: str) -> dict:
        if not isinstance(message, str) or not message.strip() or len(message) > 32_000:
            raise ValueError("Message must contain 1–32,000 characters")
        if redact(message) != message:
            raise ValueError("Do not put credential-like content in child messages")
        if self.status(session_id)["status"] in {"stopped", "failed", "stopping"}:
            raise ValueError("Agent is not running")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row[0] in {"stopped", "failed", "stopping"}:
                raise ValueError("Agent is not running")
            cursor = db.execute("INSERT INTO messages(session_id,role,content,status,created) VALUES (?,'user',?,'queued',?)",
                                (session_id, message, time.time()))
            message_id = cursor.lastrowid
        self.runtime.event("session_send", {"session_id": session_id, "message_id": message_id, "message": message}, agent_id=self.config.get("agent_id", "main"))
        return {"session_id": session_id, "message_id": message_id, "status": "queued"}

    def read(self, session_id: str, after_id: int = 0, limit: int = 5, wait_seconds: int = 0) -> dict:
        status = self.status(session_id)
        if after_id < 0 or not 1 <= limit <= 100 or not 0 <= wait_seconds <= 30:
            raise ValueError("after_id must be nonnegative, limit 1–100, and wait_seconds 0–30")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and status["status"] not in {"stopped", "failed", "waiting_approval"}:
            with self.connect() as db:
                reply = db.execute("SELECT id FROM messages WHERE session_id=? AND id>? AND role IN ('assistant','error') LIMIT 1",
                                   (session_id, after_id)).fetchone()
            if reply:
                break
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
            status = self.status(session_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,role,content,status,created,reply_to,approval_id FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
                (session_id, after_id, limit)).fetchall()
        messages, remaining = [], 60_000
        for row in rows:
            if remaining <= 0:
                break
            message = dict(row)
            original_length = len(message["content"])
            message["content"] = message["content"][:min(12_000, remaining)]
            message["content_truncated"] = original_length != len(message["content"])
            message["content_length"] = original_length
            remaining -= len(message["content"])
            messages.append(message)
        return {"session_id": session_id, "status": status["status"], "messages": messages,
                "next_after_id": messages[-1]["id"] if messages else after_id}

    def stop(self, session_id: str) -> dict:
        with self.locked():
            state = self.status(session_id)
            if state["status"] in {"stopped", "failed"}:
                return state
            row = self._row(session_id)
            if not row["pid"]:
                raise RuntimeError("Worker is still starting")
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                raise RuntimeError("Stopping workers requires Linux pidfd support")
            try:
                fd = os.pidfd_open(row["pid"])
            except ProcessLookupError:
                return self.status(session_id)
            try:
                if process_identity(row["pid"]) != row["identity"]:
                    return self.status(session_id)
                with self.connect() as db:
                    db.execute("UPDATE sessions SET status='stopping',updated=? WHERE id=?", (time.time(), session_id))
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and process_identity(row["pid"]) == row["identity"]:
                    time.sleep(0.05)
                if process_identity(row["pid"]) == row["identity"]:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                with self.connect() as db:
                    db.execute("UPDATE sessions SET status='stopped',updated=? WHERE id=?", (time.time(), session_id))
                    db.execute("UPDATE messages SET status='cancelled' WHERE session_id=? AND status IN ('queued','running')", (session_id,))
            except ProcessLookupError:
                pass
            finally:
                os.close(fd)
            child = self._processes.pop(session_id, None)
            if child is not None:
                child.wait(timeout=2)
            self.runtime.event("session_stop", {"session_id": session_id}, agent_id=self.config.get("agent_id", "main"))
            return self.status(session_id)


def register_agent_tools(registry, config: dict, state_dir: Path):
    if config.get("children") is False or os.environ.get("LOCAL_LLM_AGENT_CHILD") == "1":
        return
    manager = SessionManager(state_dir, config)
    sid = {"session_id": string(minLength=12, maxLength=12)}
    registry.add(manager.start, "Start a child with an installed Ollama model and inherited permissions. Choose fresh, shared, or selected memories and optional background. No tools supports models without tool calling.",
                 {"task": string(maxLength=32000), "model": string(),
                  "memory_mode": {"type": "string", "enum": ["fresh", "shared", "selected"]},
                  "context": string(maxLength=32000), "memory_ids": {"type": "array", "items": string(), "maxItems": 100},
                  "enable_tools": {"type": "boolean"}, "vision": {"type": "boolean"}}, name="spawn_agent")
    registry.add(manager.resume, "Restart a stopped child with its original context. Cancelled tasks stay cancelled; pending approvals remain pending.", sid, ("session_id",), name="resume_agent")
    registry.add(manager.send, "Queue a task or follow-up for a child agent. Returns immediately.",
                 {**sid, "message": string(minLength=1, maxLength=32_000)}, ("session_id", "message"), name="send_agent")
    registry.add(manager.read, "Read child messages and results; use wait_seconds=30 when awaiting work and next_after_id for subsequent messages. Long content is explicitly marked truncated; full content remains in the session database.",
                 {**sid, "after_id": integer(0, 2**63 - 1, 0), "limit": integer(1, 100, 5),
                  "wait_seconds": integer(0, 30, 0)}, ("session_id",), name="read_agent")
    registry.add(manager.status, "Get a child agent's current status.", sid, ("session_id",), name="agent_status")
    registry.add(manager.list, "List child agents, including completed sessions.", {}, name="list_agents")
    registry.add(manager.stop, "Stop a child agent and cancel its pending work.", sid, ("session_id",), name="stop_agent")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Launch a persistent child agent")
    start.add_argument("--model", required=True)
    start.add_argument("--base-url", default="http://localhost:11434")
    start.add_argument("--root", action="append", default=[])
    start.add_argument("--task", default="")
    start.add_argument("--read-only", action="store_true")
    start.add_argument("--no-internet", action="store_true")
    start.add_argument("--yes", action="store_true", help="Rejected: blanket approval is not supported")
    start.add_argument("--memory-mode", choices=["fresh", "shared", "selected"], default="fresh")
    start.add_argument("--context", default="")
    start.add_argument("--memory-id", action="append", default=[])
    start.add_argument("--no-tools", action="store_true")
    start.add_argument("--vision", action="store_true", help="Selected model accepts screenshot images")
    start.add_argument("--max-children", type=int, default=4)
    start.add_argument("--script", action="append", default=[], metavar="NAME=PATH")
    start.add_argument("--searxng-url")
    start.add_argument("--max-steps", type=int, default=20)
    subparsers = [start]
    for command in ("send", "read", "status", "stop", "resume", "list"):
        sub = commands.add_parser(command)
        subparsers.append(sub)
        if command != "list":
            sub.add_argument("session_id")
        if command == "send":
            sub.add_argument("message")
        if command == "read":
            sub.add_argument("--after-id", type=int, default=0)
            sub.add_argument("--limit", type=int, default=5)
            sub.add_argument("--wait", type=int, default=0, dest="wait_seconds")
    for sub in subparsers:
        sub.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        config = {}
        if args.command == "start":
            if args.yes:
                raise ValueError("--yes is disabled; each sensitive action requires individual approval")
            if not 1 <= args.max_steps <= 100:
                raise ValueError("--max-steps must be 1–100")
            scripts = {}
            for spec in args.script:
                name, separator, path = spec.partition("=")
                if not separator or not name or not path:
                    raise ValueError("--script requires NAME=PATH")
                scripts[name] = str(Path(path).expanduser().resolve())
            config = {"model": args.model, "base_url": args.base_url,
                      "roots": [str(Path(p).expanduser().resolve()) for p in args.root],
                      "read_only": args.read_only, "no_internet": args.no_internet, "yes": False, "max_children": args.max_children,
                      "allowed_scripts": scripts, "searxng_url": args.searxng_url, "max_steps": args.max_steps}
        manager = SessionManager(args.state_dir, config)
        if args.command == "start":
            result = manager.start(args.task, memory_mode=args.memory_mode, context=args.context, memory_ids=args.memory_id, enable_tools=not args.no_tools, vision=args.vision)
        elif args.command == "send":
            result = manager.send(args.session_id, args.message)
        elif args.command == "read":
            result = manager.read(args.session_id, args.after_id, args.limit, args.wait_seconds)
        elif args.command == "list":
            result = manager.list()
        else:
            result = getattr(manager, args.command)(args.session_id)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
