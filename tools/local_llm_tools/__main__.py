"""Interactive terminal, direct tool calls, and durable agent sessions.

The CLI only renders activity and asks the human for decisions. Approval state,
memories, and task progress live in the same stores used by the dashboard; neither
interface owns a separate permission policy.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from .agent import Agent
from .runtime import DEFAULT_STATE_DIR, ApprovalRequired, RuntimeStore, make_approver, redact


def confirm(name, details):
    """Ask about one exact action. A pipe/background process leaves it pending."""
    if not sys.stdin.isatty():
        return None
    print(f"\nAPPROVAL REQUIRED: {name}", file=sys.stderr)
    print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
    print("Type yes to allow this one action, or no to deny: ", end="", file=sys.stderr, flush=True)
    return input().strip().lower() == "yes"


def render_event(event):
    """Keep the user informed without printing raw private file/script contents."""
    clock = datetime.now().astimezone().strftime("%H:%M:%S")
    kind = event.get("event", "activity")
    details = redact({key: value for key, value in event.items() if key != "event"}, hide_content=True)
    print(f"[{clock}] {kind}\n{json.dumps(details, indent=2, ensure_ascii=False)}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an Ollama agent with durable memory and per-action approvals.")
    parser.add_argument("task", nargs="?", help="task; omit for interactive chat")
    parser.add_argument("--model", help="installed Ollama model name")
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--root", action="append", default=[], help="allowed file folder; repeat to grant more")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--agent-id", default="main", help="persistent conversation identity")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--no-internet", action="store_true")
    parser.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--script", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--searxng-url")
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--context-limit", type=int, default=60000, help="working-context character compaction threshold")
    parser.add_argument("--context-tokens", type=int, default=16384, help="Ollama num_ctx; reserves space for tools and responses")
    parser.add_argument("--vision", action="store_true", help="send captured screenshots to a vision-capable model")
    parser.add_argument("--children", action="store_true", help="enable delegated agent tools")
    parser.add_argument("--no-auto-memory", action="store_true", help="disable automatic fact extraction")
    parser.add_argument("--no-tools", action="store_true", help="use a model without tool-calling support")
    parser.add_argument("--memory-scope", choices=["private", "shared"], default="shared")
    parser.add_argument("--every", type=int, help="create a durable recurring task (run the task service to execute it)")
    parser.add_argument("--resume", action="store_true", help="resume this agent's suspended task")
    parser.add_argument("--tools", action="store_true", help="print tool schemas without contacting the model")
    parser.add_argument("--call", metavar="TOOL", help="call a tool directly")
    parser.add_argument("--arguments", default="{}", help="JSON for --call")
    args = parser.parse_args()
    # The former blanket switch is deliberately rejected instead of silently
    # granting permission contrary to the user's per-occurrence approval rule.
    if args.yes:
        parser.error("--yes was removed: downloads and sensitive actions require individual approval")
    if args.every is not None and (args.every < 5 or not args.task):
        parser.error("--every requires a task and at least five seconds")
    if not args.model and not (args.tools or args.call):
        parser.error("--model is required")
    try:
        scripts = {}
        for entry in args.script:
            name, separator, path = entry.partition("=")
            if not separator or not name.isidentifier() or not path or name in scripts:
                raise ValueError("--script must be a unique NAME=PATH")
            scripts[name] = str(Path(path).expanduser().resolve(strict=True))
        roots = [str(Path(root).expanduser().resolve(strict=True)) for root in args.root]
        state_dir = str(args.state_dir.expanduser().resolve())
        config = {
            "model": args.model or "not-connected", "base_url": args.base_url,
            "roots": roots, "read_only": args.read_only, "no_internet": args.no_internet,
            "allowed_scripts": scripts, "searxng_url": args.searxng_url,
            "max_steps": args.max_steps, "context_limit": args.context_limit, "context_tokens": args.context_tokens,
            "vision": args.vision,
            "children": args.children, "state_dir": state_dir, "agent_id": args.agent_id,
            "memory_scope": args.memory_scope, "auto_memory": not args.no_auto_memory,
            "enable_tools": not args.no_tools,
            "restore_model": False,
        }
        runtime = RuntimeStore(state_dir)
        if args.every:
            from .service import TaskStore
            task = TaskStore(state_dir).add(args.task, config, interval=args.every)
            print(json.dumps(task, indent=2))
            print("Recurring task saved. Run scripts/agent-control.sh serve with this state directory to execute it.", file=sys.stderr)
            return 0
        approval = make_approver(runtime, args.agent_id, interactive_callback=confirm)
        agent = Agent(config, approve=approval, on_event=render_event)
        if args.tools:
            print(json.dumps(agent.tools.definitions(), indent=2))
            return 0
        if args.call:
            print(json.dumps(agent.tools.call(args.call, args.arguments), indent=2, ensure_ascii=False))
            return 0
        if args.task or args.resume:
            print(agent.run(args.task or "Resume the suspended task", resume=args.resume))
            return 0
        print("Agent ready: /compact, /memories, /approvals, /resume, /recover INSPECTION_NOTE, /reset, /exit.")
        while True:
            try:
                task = input("You> ").strip()
            except EOFError:
                return 0
            if task == "/exit":
                return 0
            try:
                if task == "/compact":
                    print(json.dumps(agent.compact(), indent=2, ensure_ascii=False))
                elif task == "/memories":
                    print(json.dumps(agent.memory.search(agent.agent_id, scope=config["memory_scope"]), indent=2))
                elif task == "/approvals":
                    print(json.dumps(runtime.approvals(), indent=2))
                elif task == "/resume":
                    print(agent.run("Resume the suspended task", resume=True))
                elif task.startswith("/recover "):
                    print(json.dumps(agent.resolve_interruption(task[len("/recover "):])))
                elif task == "/reset":
                    # Start a new identity without deleting the old history or its
                    # pending approvals. The user can return with --agent-id.
                    config["agent_id"] = "chat-" + uuid.uuid4().hex[:12]
                    approval = make_approver(runtime, config["agent_id"], interactive_callback=confirm)
                    agent = Agent(config, approve=approval, on_event=render_event)
                    print(f"Fresh conversation: {agent.agent_id}")
                elif task:
                    print(agent.run(task))
            except ApprovalRequired as error:
                print(f"Waiting for approval {error.approval_id}. Review it in the dashboard or agent-control CLI.")
            except Exception as error:
                print(f"Error: {error}", file=sys.stderr)
    except ApprovalRequired as error:
        print(json.dumps({"status": "waiting_approval", "approval_id": error.approval_id}))
        return 2
    except KeyboardInterrupt:
        print("Stopped; history and pending approvals are retained.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
