"""Shared, durable activity and per-action approval records.

This module is the authority boundary between an agent asking to act and a human
allowing that specific action. Models receive tool errors or a suspended task;
they never receive a tool for approving their own requests. SQLite transactions
make a decision consumable exactly once, even if two workers resume together.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "local-llm-tools"
_SECRET_KEYS = re.compile(r"password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|credential", re.I)
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+|(?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*)[^\s,;]+")


def redact(value, *, hide_content=False):
    """Make a bounded, credential-redacted representation suitable for display.

    Activity logs describe files/scripts rather than copying their contents. An
    approval is different: users must see the proposed action, so normal content
    remains visible there, with credential-like values still masked.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _SECRET_KEYS.search(str(key)):
                result[key] = "[redacted]"
            elif hide_content and key in {"content", "text", "code", "body", "stdout", "stderr", "images", "thinking"}:
                result[key] = {"omitted": True, "length": len(str(item))}
            else:
                result[key] = redact(item, hide_content=hide_content)
        return result
    if isinstance(value, (tuple, list)):
        return [redact(item, hide_content=hide_content) for item in value[:100]]
    if isinstance(value, str):
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[private key redacted]", value, flags=re.S)
        value = _SECRET_TEXT.sub(lambda match: match.group(1) + "[redacted]", value)
        return value[:20_000] + ("… [truncated]" if len(value) > 20_000 else "")
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


class ApprovalRequired(Exception):
    """Suspend execution until the human decides the durable approval record."""

    def __init__(self, approval_id: str):
        self.approval_id = approval_id
        super().__init__(f"Waiting for approval {approval_id}")


class RuntimeStore:
    """One shared authority/log database for terminal, dashboard, and workers."""

    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir or DEFAULT_STATE_DIR).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A private directory also protects SQLite journals and dashboard tokens.
        os.chmod(self.state_dir, 0o700)
        self.path = self.state_dir / "runtime.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time REAL NOT NULL, kind TEXT NOT NULL,
                    agent_id TEXT, task_id TEXT, details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL,
                    details TEXT NOT NULL, status TEXT NOT NULL,
                    agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approval_occurrence
                    ON approvals(agent_id,task_id,fingerprint,status);
            """)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        """Use short transactions; never keep a transaction open during a prompt."""
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, kind, details, agent_id=None, task_id=None):
        """Append redacted structured activity; callers may separately render it."""
        safe = redact(details, hide_content=True)
        with self.connect() as db:
            cursor = db.execute("INSERT INTO events(time,kind,agent_id,task_id,details) VALUES(?,?,?,?,?)",
                                (time.time(), str(kind), agent_id, task_id, json.dumps(safe)))
            return cursor.lastrowid

    def events(self, after_id=0, limit=100):
        """Read activity incrementally without repeatedly shipping the full log."""
        if not 1 <= limit <= 500 or after_id < 0:
            raise ValueError("Invalid event cursor or limit")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(row, details=json.loads(row["details"])) for row in rows]

    def approvals(self, status="pending"):
        """Return recent approval records, including consumed decisions if asked."""
        with self.connect() as db:
            if status in {None, "all"}:
                rows = db.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT 500").fetchall()
            else:
                rows = db.execute("SELECT * FROM approvals WHERE status=? ORDER BY created DESC LIMIT 500", (status,)).fetchall()
        return [dict(row, details=json.loads(row["details"])) for row in rows]

    def get_approval(self, approval_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown approval request")
        return dict(row, details=json.loads(row["details"]))

    def request(self, action, details, agent_id, task_id=None):
        """Create or recover the pending request for this exact suspended action.

        A hash binds the approval to the original payload, not a model-written
        summary. Consumed approvals are excluded, so repeating the operation
        requires a new human decision. Denied retries stay denied for this task.
        """
        encoded = json.dumps({"action": action, "details": details}, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        agent_id, task_id = str(agent_id), str(task_id or "")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM approvals WHERE agent_id=? AND task_id=?
                AND fingerprint=? AND status IN ('pending','approved','denied')
                ORDER BY created DESC LIMIT 1""", (agent_id, task_id, fingerprint)).fetchone()
            if row:
                return dict(row, details=json.loads(row["details"]))
            approval_id, now = uuid.uuid4().hex, time.time()
            safe_details = redact(details)
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)",
                       (approval_id, action, json.dumps(safe_details), "pending", agent_id, task_id, fingerprint, now, now))
        self.event("approval_requested", {"approval_id": approval_id, "action": action, "details": safe_details}, agent_id, task_id)
        return self.get_approval(approval_id)

    def decide(self, approval_id, approved: bool):
        """Human-facing operation. No agent tool exposes this method."""
        if type(approved) is not bool:
            raise ValueError("Decision must be a boolean")
        status = "approved" if approved else "denied"
        with self.connect() as db:
            cursor = db.execute("UPDATE approvals SET status=?,updated=? WHERE id=? AND status='pending'",
                                (status, time.time(), approval_id))
            if cursor.rowcount != 1:
                raise ValueError("Approval is missing or has already been decided")
        row = self.get_approval(approval_id)
        self.event("approval_decided", {"approval_id": approval_id, "status": status}, row["agent_id"], row["task_id"])
        return row

    def consume(self, approval_id):
        """Atomically spend permission immediately before the approved operation."""
        with self.connect() as db:
            cursor = db.execute("UPDATE approvals SET status='consumed',updated=? WHERE id=? AND status='approved'",
                                (time.time(), approval_id))
            if cursor.rowcount != 1:
                raise PermissionError("This action does not have an unused approval")
        row = self.get_approval(approval_id)
        self.event("approval_consumed", {"approval_id": approval_id, "action": row["action"]}, row["agent_id"], row["task_id"])


def explain_action(name, arguments):
    """Add concrete policy context without trusting a model to classify risk."""
    return {
        "action": name,
        "proposed_arguments": arguments,
        "purpose": arguments.get("purpose", arguments.get("reason", f"Perform the requested {name} operation")),
        "expected_effect": {
            "delete_file": "Permanently remove the specified file.",
            "download_file": "Retrieve data from the source URL and save it at the destination.",
            "ollama_pull": "Download model files into Ollama's model storage.",
            "ollama_delete": "Permanently remove the selected installed model.",
        }.get(name, "Execute exactly the described action; browser or desktop input may affect the active application."),
        "reversibility": "Deletion and external effects may not be reversible. Downloading does not authorize execution.",
        "size": arguments.get("size", "Unknown unless supplied by the source"),
    }


def make_approver(store, agent_id, task_id=None, interactive_callback=None):
    """Return the capability tools' approval callback.

    In terminal mode the callback can ask immediately. In service/child mode a
    pending request suspends the agent; a terminal or dashboard decision resumes
    it later. There is intentionally no blanket auto-approval switch.
    """
    def approve(name, arguments):
        # Reject a payload that the review UI would truncate. A human must never
        # approve an unseen suffix while the fingerprint authorizes the full action.
        def reviewable(value):
            if isinstance(value, str) and len(value) > 20_000:
                raise ValueError("Approval details exceed the reviewable text limit; split the action")
            if isinstance(value, (list, tuple)):
                if len(value) > 100:
                    raise ValueError("Approval has too many items; split the action")
                for item in value:
                    reviewable(item)
            if isinstance(value, dict):
                for item in value.values():
                    reviewable(item)
        reviewable(arguments)
        details = explain_action(name, arguments)
        request = store.request(name, details, agent_id, task_id)
        if request["status"] == "pending" and interactive_callback is not None:
            decision = interactive_callback(name, request["details"])
            # None means no interactive terminal is available, not denial.
            if decision is not None:
                request = store.decide(request["id"], bool(decision))
        if request["status"] == "denied":
            raise PermissionError(f"User denied {name}")
        if request["status"] == "pending":
            raise ApprovalRequired(request["id"])
        store.consume(request["id"])
        return True
    return approve
