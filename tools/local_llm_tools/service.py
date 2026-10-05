"""Durable task queue, schedule/watch triggers, and the local service CLI.

Each execution has a stable agent id and therefore its own persisted context.
A process crash never replays an uncertain action: running records become paused
and need an explicit resume. Scheduling templates create separate execution rows.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class TaskInterrupted(BaseException):
    """Cooperative stop that cannot be mistaken for an ordinary tool failure."""


class TaskStore:
    """SQLite queue shared by dashboard, CLI and scheduler; one connection per call."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "tasks.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL, config TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                next_run REAL, interval REAL, watch_path TEXT, snapshot TEXT,
                parent_id TEXT, agent_id TEXT NOT NULL, result TEXT, error TEXT,
                approval_id TEXT, attempts INTEGER NOT NULL DEFAULT 0)""")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("Unknown task")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def add(self, prompt, config, scheduled_at=None, interval=None, watch_path=None, parent_id=None):
        """Create a task or recurring template. Watches must stay in granted roots."""
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise ValueError("Task must contain 1 to 32000 characters")
        from .memory import redact
        if redact(prompt) != prompt:
            raise ValueError("Task instructions contain credential-like text; use a separate credential workflow")
        if scheduled_at is not None and (type(scheduled_at) not in {int, float} or not math.isfinite(scheduled_at) or scheduled_at < 0):
            raise ValueError("Scheduled timestamp must be a finite nonnegative number")
        if interval is not None and (type(interval) not in {int, float} or not math.isfinite(interval) or interval < 1):
            raise ValueError("Interval must be at least one second")
        if watch_path:
            watched = Path(watch_path).expanduser().resolve()
            roots = [Path(p).expanduser().resolve() for p in config.get("roots", [])]
            if not any(watched.is_relative_to(root) for root in roots):
                raise ValueError("Watch path must be inside a granted root")
            if watched.is_relative_to(self.state_dir) or self.state_dir.is_relative_to(watched):
                raise ValueError("Watch path must not include private agent state")
            watch_path = str(watched)
        now = time.time()
        task_id = uuid.uuid4().hex
        status = "scheduled" if interval is not None or watch_path or scheduled_at is not None else "queued"
        with self.connect() as db:
            db.execute("""INSERT INTO tasks
                (id,prompt,config,status,created,updated,next_run,interval,watch_path,snapshot,parent_id,agent_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id, prompt, json.dumps(config), status,
                now, now, scheduled_at if scheduled_at is not None else now, interval, watch_path,
                self.snapshot(watch_path) if watch_path else None, parent_id, "task-" + task_id))
        return self.get(task_id)

    def get(self, task_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list(self):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 1000")]

    def update(self, task_id, **fields):
        allowed = {"status", "next_run", "snapshot", "result", "error", "approval_id"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid task update")
        from .memory import redact
        for key in ("result", "error"):
            if key in fields:
                fields[key] = redact(fields[key])
        fields["updated"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), task_id))
        return self.get(task_id)

    def control(self, task_id, action):
        task = self.get(task_id)
        if action == "pause":
            return self.update(task_id, status="paused")
        if action == "cancel":
            return self.update(task_id, status="cancelled")
        if action == "resume":
            if task["status"] == "waiting_approval":
                raise ValueError("Decide the pending approval before resuming")
            if task["status"] not in {"paused", "failed", "cancelled"}:
                raise ValueError("Only paused, failed or cancelled tasks can resume")
            return self.update(task_id, status="scheduled" if task["interval"] or task["watch_path"] else "queued", error=None)
        raise ValueError("Unknown control")

    @staticmethod
    def snapshot(path):
        """Record metadata only; no private file contents enter watch state."""
        root = Path(path)
        entries = []
        paths = [root]
        if root.is_dir():
            # A bounded poll prevents a huge tree from monopolizing the daemon.
            import itertools
            paths += list(itertools.islice(root.rglob("*"), 10000))
        for item in paths:
            try:
                stat = item.lstat()
                entries.append((str(item), stat.st_mtime_ns, stat.st_size))
            except OSError:
                entries.append((str(item), None, None))
        return json.dumps(sorted(entries))

    def tick(self):
        """Convert due templates to queued executions, coalescing missed intervals."""
        now = time.time()
        for task in self.list():
            if task["status"] != "scheduled":
                continue
            if task["watch_path"]:
                signature = self.snapshot(task["watch_path"])
                if signature == task["snapshot"]:
                    continue
            elif task["next_run"] > now:
                continue
            if task["interval"] or task["watch_path"]:
                # Avoid a growing backlog while a prior run waits for approval.
                if any(t["parent_id"] == task["id"] and t["status"] in {"queued", "running", "waiting_approval", "paused"} for t in self.list()):
                    continue
                self.add(task["prompt"], task["config"], parent_id=task["id"])
                if task["watch_path"]:
                    self.update(task["id"], snapshot=signature)
                if task["interval"]:
                    self.update(task["id"], next_run=now + task["interval"])
            else:
                self.update(task["id"], status="queued")

    def claim(self, eligible_ids=None):
        """An immediate transaction ensures only one worker claims a queue row."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sql, parameters = "SELECT * FROM tasks WHERE status='queued'", []
            if eligible_ids is not None:
                if not eligible_ids:
                    return None
                sql += " AND id IN (" + ",".join("?" for _ in eligible_ids) + ")"
                parameters = list(eligible_ids)
            row = db.execute(sql + " ORDER BY created LIMIT 1", parameters).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated=? WHERE id=?", (time.time(), row["id"]))
        return self.get(row["id"])

    def recover(self, runtime=None):
        """Resume only checkpoints proven to precede or follow an external effect.

        An executing checkpoint has an uncertain outcome and is never replayed.
        Pending approval checkpoints retain their human decision; a checkpoint
        whose arguments were redacted cannot safely reconstruct the original call.
        """
        from .memory import MemoryStore
        memory = MemoryStore(self.state_dir)
        approvals = runtime.approvals(status="all") if runtime else []
        for task in self.list():
            if task["status"] != "running":
                continue
            checkpoint = memory.load(task["agent_id"])
            if checkpoint and checkpoint.get("pending_redacted"):
                self.update(task["id"], status="paused", error="Interrupted arguments were redacted; review before recovery")
                continue
            phase = checkpoint.get("phase") if checkpoint else None
            if phase == "ready":
                self.update(task["id"], status="queued", error=None)
            elif phase == "pending_approval":
                pending = next((a for a in approvals if a["agent_id"] == task["agent_id"] and a["task_id"] == task["id"] and a["status"] == "pending"), None)
                if pending:
                    self.update(task["id"], status="waiting_approval", approval_id=pending["id"], error=None)
                else:
                    # The agent re-enters the exact approval gate. It consumes an
                    # unused grant or asks again; it cannot infer permission.
                    self.update(task["id"], status="queued", error=None)
            else:
                self.update(task["id"], status="paused", error="Service interrupted with no safe checkpoint; inspect activity before recovery")


class Scheduler:
    """Single execution worker; controls are checked between model/tool events."""

    def __init__(self, store, runtime, base_config=None, agent_factory=None):
        self.store, self.runtime = store, runtime
        self.base_config = base_config or {}
        self.agent_factory = agent_factory
        self.stopping = threading.Event()
        self.thread = None
        # Keep browser pipes and other session resources alive across approval
        # waits. A bounded cache limits unattended waiting sessions on this host.
        self.agents = {}
        self.max_active = 8
        self.store.recover(runtime)

    def run_once(self):
        from .agent import Agent
        from .runtime import ApprovalRequired, make_approver
        for task_id in list(self.agents):
            if self.store.get(task_id)["status"] in {"completed", "failed", "cancelled"}:
                self.release(task_id)
        self.store.tick()
        # Decisions do not execute actions in HTTP threads. They only release the
        # suspended task, whose persisted agent context owns the exact next action.
        for task in self.store.list():
            if task["status"] == "waiting_approval" and task["approval_id"]:
                approval = self.runtime.get_approval(task["approval_id"])
                if approval["status"] in {"approved", "denied"}:
                    self.store.update(task["id"], status="queued")
        task = self.store.claim(list(self.agents) if len(self.agents) >= self.max_active else None)
        if task is None:
            return False
        config = {**self.base_config, **task["config"], "state_dir": str(self.store.state_dir), "agent_id": task["agent_id"], "task_id": task["id"], "yes": False}
        def event(details):
            state = self.store.get(task["id"])["status"]
            if self.stopping.is_set() or state != "running":
                raise TaskInterrupted()
            # Agent persists its own detailed events; this hook only enforces stops.
        self.runtime.event("task_started", {"attempt": task["attempts"], "model": config.get("model")}, agent_id=task["agent_id"], task_id=task["id"])
        try:
            agent = self.agents.get(task["id"])
            if agent is None:
                agent = (self.agent_factory or Agent)(config, approve=make_approver(self.runtime, task["agent_id"], task["id"]), on_event=event)
                self.agents[task["id"]] = agent
            result = agent.run(task["prompt"], resume=task["attempts"] > 1)
            # A stop arriving after the final model response must still win.
            if self.store.get(task["id"])["status"] == "running":
                self.store.update(task["id"], status="completed", result=result, approval_id=None)
        except ApprovalRequired as exc:
            if self.store.get(task["id"])["status"] == "running":
                self.store.update(task["id"], status="waiting_approval", approval_id=str(exc.approval_id))
        except TaskInterrupted:
            if self.store.get(task["id"])["status"] == "running":
                self.store.update(task["id"], status="paused")
        except Exception as exc:
            # Automatic retries could repeat a payment or deletion whose response
            # was lost. Keep failures inspectable and require explicit resume.
            if self.store.get(task["id"])["status"] == "running":
                self.store.update(task["id"], status="failed", error=f"{type(exc).__name__}: {exc}")
        status = self.store.get(task["id"])["status"]
        if status in {"completed", "failed", "cancelled"}:
            self.release(task["id"])
        self.runtime.event("task_state", {"status": status}, agent_id=task["agent_id"], task_id=task["id"])
        return True

    def release(self, task_id):
        """Close only agent-owned browser resources when its task terminates."""
        agent = self.agents.pop(task_id, None)
        registry = getattr(agent, "tools", None)
        if registry and any(tool["name"] == "browser_close" for tool in registry.definitions()):
            try:
                registry.call("browser_close", {})
            except Exception as exc:
                self.runtime.event("resource_cleanup_error", {"error": str(exc)}, task_id=task_id)

    def start(self):
        def loop():
            while not self.stopping.is_set():
                try:
                    busy = self.run_once()
                except Exception as exc:
                    self.runtime.event("scheduler_error", {"error": str(exc)})
                    busy = False
                if not busy:
                    self.stopping.wait(1)
        self.thread = threading.Thread(target=loop, name="agent-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)
        # Never close resources underneath an active tool. The daemon thread
        # cooperatively stops at its next boundary; process exit handles a hang.
        if not self.thread or not self.thread.is_alive():
            for task_id in list(self.agents):
                self.release(task_id)


def compact_task(store, task_id):
    """Compact an inactive task's context without executing its outstanding tools."""
    from .agent import Agent
    task = store.get(task_id)
    if task["status"] not in {"completed", "failed"}:
        raise ValueError("Only completed or failed task contexts can be compacted from this interface")
    config = {**task["config"], "state_dir": str(store.state_dir), "agent_id": task["agent_id"], "task_id": task_id}
    return Agent(config).compact()


def recover_task(store, task_id, note, scheduler=None):
    """Record a human's observed outcome without replaying an interrupted action.

    CLI callers take the daemon lock so they cannot race a tool still running in
    another process. Dashboard callers already know the live scheduler cache.
    This function is deliberately absent from the model's tool registry.
    """
    from .agent import Agent
    from .memory import redact
    import fcntl
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 4000:
        raise ValueError("Provide an observed-outcome note of 1 to 4000 characters")
    if redact(note) != note:
        raise ValueError("Recovery notes must not contain credentials")
    lock = None
    try:
        if scheduler is None:
            lock = (store.state_dir / "service.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("The service is running; resolve this task through its dashboard") from exc
        elif task_id in scheduler.agents:
            raise ValueError("This task still has a live agent; stop the service before recovering it")
        task = store.get(task_id)
        if task["status"] not in {"paused", "failed"}:
            raise ValueError("Only interrupted paused or failed tasks can be recovered")
        config = {**task["config"], "state_dir": str(store.state_dir), "agent_id": task["agent_id"], "task_id": task_id}
        result = Agent(config).resolve_interruption(note)
        store.update(task_id, status="paused", error=None)
        return result
    finally:
        if lock is not None:
            lock.close()


def register_task_tools(registry, config):
    """Let an agent schedule work without expanding its inherited permissions.

    A task's roots, model endpoint, and security configuration are captured from
    the caller. Only the prompt and trigger are model-selectable. The parent's
    identity is retained for inspecting and controlling tasks it created.
    """
    from .registry import string
    store = TaskStore(config["state_dir"])
    owner = config.get("agent_id", "main")
    def create_task(prompt, interval=None, scheduled_at=None, watch_path=None):
        child_config = dict(config)
        child_config.pop("agent_id", None)
        child_config.pop("task_id", None)
        child_config["task_owner"] = owner
        return store.add(prompt, child_config, scheduled_at, interval, watch_path)
    def list_tasks():
        return [task for task in store.list() if task["config"].get("task_owner") == owner]
    def control_task(task_id, action):
        if store.get(task_id)["config"].get("task_owner") != owner:
            raise PermissionError("Agent can control only its own scheduled tasks")
        return store.control(task_id, action)
    registry.add(create_task, "Create durable work now, at a Unix timestamp, on an interval, or after changes to a granted watch path. Sensitive actions still wait for user approval.",
                 {"prompt": string(maxLength=32000), "interval": {"type":"number", "minimum":1},
                  "scheduled_at": {"type":"number"}, "watch_path": string()}, ["prompt"], name="schedule_task")
    registry.add(list_tasks, "Inspect tasks scheduled by this agent.", {}, [], name="list_tasks")
    registry.add(control_task, "Pause, cancel or resume a task created by this agent. Pending approvals must be decided by the human.",
                 {"task_id": string(), "action": {"type":"string", "enum":["pause","resume","cancel"]}},
                 ["task_id","action"], name="control_task")


def main(argv=None):
    """Terminal controls use the exact same durable stores as the dashboard."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="~/.local/state/local-llm-tools")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "add"):
        p = sub.add_parser(name)
        p.add_argument("--model", required=True)
        p.add_argument("--root", action="append", default=[])
        p.add_argument("--base-url", default="http://localhost:11434")
        p.add_argument("--read-only", action="store_true")
        p.add_argument("--no-internet", action="store_true")
        p.add_argument("--no-auto-memory", action="store_true")
        p.add_argument("--memory-scope", choices=("private", "shared"), default="shared")
        p.add_argument("--vision", action="store_true")
        p.add_argument("--context-tokens", type=int, default=16384)
        p.add_argument("--searxng-url")
        p.add_argument("--max-steps", type=int, choices=range(1, 101), default=16, metavar="1..100")
        if name == "serve":
            p.add_argument("--port", type=int, default=8765)
        else:
            p.add_argument("prompt")
            p.add_argument("--at", type=float, help="Unix timestamp")
            p.add_argument("--interval", type=float)
            p.add_argument("--watch")
    for name in ("tasks", "events", "approvals", "token"):
        sub.add_parser(name)
    for name in ("pause", "resume", "cancel", "approve", "deny", "compact"):
        sub.add_parser(name).add_argument("id")
    recovery = sub.add_parser("recover")
    recovery.add_argument("id")
    recovery.add_argument("--note", required=True)
    memories = sub.add_parser("memories")
    memories.add_argument("--owner", default="main")
    memories.add_argument("--query", default="")
    for name in ("memory-update", "memory-forget"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--owner", required=True)
        if name == "memory-update":
            p.add_argument("content")
    args = parser.parse_args(argv)
    from .runtime import RuntimeStore
    store, runtime = TaskStore(args.state_dir), RuntimeStore(args.state_dir)
    if args.command in {"serve", "add"}:
        config = {"model": args.model, "roots": args.root, "base_url": args.base_url, "children": True, "auto_memory": not args.no_auto_memory, "memory_scope": args.memory_scope, "vision": args.vision, "context_tokens": args.context_tokens, "read_only": args.read_only, "no_internet": args.no_internet, "searxng_url": args.searxng_url, "max_steps": args.max_steps, "state_dir": str(store.state_dir)}
    if args.command == "serve":
        import fcntl
        # A lifetime lock avoids a second daemon misclassifying live work as crashed.
        lock = (store.state_dir / "service.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from .dashboard import make_server
        scheduler = Scheduler(store, runtime, config)
        server = make_server(store, runtime, scheduler, port=args.port)
        scheduler.start()
        print(f"Dashboard: http://127.0.0.1:{args.port}\nRead login token with: python -m local_llm_tools.service --state-dir {store.state_dir} token", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            scheduler.stop()
            server.server_close()
            lock.close()
        return
    if args.command == "add":
        result = store.add(args.prompt, config, args.at, args.interval, args.watch)
    elif args.command in {"pause", "resume", "cancel"}:
        result = store.control(args.id, args.command)
    elif args.command in {"approve", "deny"}:
        result = runtime.decide(args.id, args.command == "approve")
    elif args.command == "recover":
        result = recover_task(store, args.id, args.note)
    elif args.command == "compact":
        result = compact_task(store, args.id)
    elif args.command == "tasks":
        result = store.list()
    elif args.command == "events":
        result = runtime.events(limit=100)
    elif args.command == "token":
        from .dashboard import auth_token
        result = auth_token(store.state_dir)
    elif args.command in {"memories", "memory-update", "memory-forget"}:
        from .memory import MemoryStore
        memory = MemoryStore(args.state_dir)
        if args.command == "memories":
            result = memory.search(args.owner, args.query, scope="shared", limit=100)
        elif args.command == "memory-update":
            result = memory.update(args.owner, args.id, args.content)
        else:
            result = memory.forget(args.owner, args.id)
    else:
        result = runtime.approvals()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
