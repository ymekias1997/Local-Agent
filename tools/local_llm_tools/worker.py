"""Private worker process; use sessions.py or scripts/agent-instance.sh to launch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

from .sessions import SessionManager
from .runtime import RuntimeStore, make_approver, ApprovalRequired
from .memory import redact


class WorkerStop(BaseException):
    pass


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit("Usage: python -m local_llm_tools.worker STATE_DIR SESSION_ID")
    state_dir, session_id = args
    os.environ["LOCAL_LLM_AGENT_CHILD"] = "1"
    manager = SessionManager(Path(state_dir))

    def stop(signum, frame):
        raise WorkerStop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    final_status = "stopped"
    try:
        # The launcher publishes PID identity before the worker starts consuming.
        deadline = time.monotonic() + 5
        while True:
            row = manager._row(session_id)
            if row["pid"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Launcher did not register worker PID")
            time.sleep(0.02)
        config = json.loads(row["config"])
        config["children"] = False
        config["yes"] = False
        config["state_dir"] = str(manager.state_dir)
        config["agent_id"] = "child-" + session_id
        runtime = RuntimeStore(manager.state_dir)
        current_task = {"id": None}
        def approve(name, arguments):
            # Each inbox message has its own approval namespace. A grant for one
            # message cannot accidentally authorize another child conversation.
            return make_approver(runtime, config["agent_id"], current_task["id"])(name, arguments)
        from .agent import Agent
        agent = Agent(config, approve=approve)
        with manager.connect() as db:
            db.execute("UPDATE sessions SET status='idle',updated=? WHERE id=? AND status='starting'",
                       (time.time(), session_id))
        while True:
            with manager.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                state = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
                if state in {"stopping", "stopped", "failed"}:
                    break
                message = db.execute(
                    "SELECT id,content,status,approval_id FROM messages WHERE session_id=? AND role='user' AND status IN ('waiting_approval','queued') ORDER BY CASE status WHEN 'waiting_approval' THEN 0 ELSE 1 END,id LIMIT 1",
                    (session_id,)).fetchone()
                resuming = bool(message and message["status"] == "waiting_approval")
                if resuming and runtime.get_approval(message["approval_id"])["status"] == "pending":
                    message = None
                if message:
                    db.execute("UPDATE messages SET status='running' WHERE id=?", (message["id"],))
                    db.execute("UPDATE sessions SET status='busy',updated=? WHERE id=?", (time.time(), session_id))
            if message is None:
                time.sleep(0.2)
                continue
            current_task["id"] = f"session:{session_id}:message:{message['id']}"
            if hasattr(agent, "config"):
                agent.config["task_id"] = current_task["id"]
            try:
                if message["content"].strip() == "/compact":
                    result = json.dumps(agent.compact(), ensure_ascii=False)
                elif resuming:
                    result = agent.run(message["content"], resume=True)
                else:
                    result = agent.run(message["content"])
                role, status = "assistant", "done"
            except ApprovalRequired as exc:
                with manager.connect() as db:
                    db.execute("UPDATE messages SET status='waiting_approval',approval_id=? WHERE id=?", (exc.approval_id, message["id"]))
                    db.execute("UPDATE sessions SET status='waiting_approval',updated=? WHERE id=?", (time.time(), session_id))
                continue
            except Exception as exc:
                result = f"{type(exc).__name__}: {exc}"
                role, status = "error", "failed"
            with manager.connect() as db:
                db.execute("UPDATE messages SET status=? WHERE id=?", (status, message["id"]))
                db.execute("INSERT INTO messages(session_id,role,content,status,created,reply_to) VALUES (?,?,?,?,?,?)",
                           (session_id, role, str(redact(result)), status, time.time(), message["id"]))
                db.execute("UPDATE sessions SET status='idle',updated=? WHERE id=? AND status='busy'",
                           (time.time(), session_id))
    except WorkerStop:
        pass
    except Exception as exc:
        final_status = "failed"
        with manager.connect() as db:
            db.execute("INSERT INTO messages(session_id,role,content,status,created) VALUES (?,'error',?,'failed',?)",
                       (session_id, redact(f"{type(exc).__name__}: {exc}"), time.time()))
        print(redact(f"Worker failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with manager.connect() as db:
            db.execute("UPDATE sessions SET status=?,updated=? WHERE id=?", (final_status, time.time(), session_id))
            db.execute("UPDATE messages SET status='cancelled' WHERE session_id=? AND status IN ('queued','running')",
                       (session_id,))
    return 1 if final_status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
