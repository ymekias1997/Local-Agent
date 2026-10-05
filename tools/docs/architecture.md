# Implementation map

The model proposes actions. Python validates arguments, checks policy, performs
the action, records the outcome, and returns evidence to the model. The model
has no tool for changing its grants or approving requests.

## Components

| Module | Responsibility |
| --- | --- |
| `agent.py` | Ollama transport, context budget, action loop, checkpoints, compaction, screenshot attachment |
| `registry.py` | Explicit tool registration and input-schema validation |
| `runtime.py` | Append-only activity records and exact, single-use approvals |
| `memory.py` | Scoped long-term facts, original message history, current-context checkpoints |
| `service.py` | Durable task queue, schedules, watches, pause/recovery, terminal controls |
| `dashboard.py` and `static/` | Authenticated loopback API and browser interface |
| `sessions.py` and `worker.py` | Separate agent processes, delegated memory, messages, restart |
| `files.py` | Granted-root text/binary operations and configuration/deletion gates |
| `web.py` | Bounded HTTP, search, approved downloads |
| `sandbox.py` | Generated code isolation and explicit result commits |
| `capabilities.py` | Ollama management and provider registration |
| `browser.py` | Private Chromium DevTools pipe, page inspection and approved interactions |
| `desktop.py` | Omarchy/Wayland capture and approved input |
| `system.py` | Clock, notifications, registered sandboxed scripts |

## Approval lifecycle

1. A sensitive tool constructs concrete action details before the external effect.
2. `make_approver` hashes the action plus its exact arguments. A pending record
   binds them to a particular agent/task. It cannot reuse an approval for changed
   arguments or hide oversized detail behind a truncated display.
3. The terminal or dashboard lets the human decide. An absent terminal leaves a
   request pending; it does not imply permission.
4. A background Agent saves its pending tool batch and cursor, then suspends.
5. On resumption, the same invocation atomically consumes the approval. Earlier
   calls in the batch are not replayed. A subsequent identical action needs a
   fresh approval.
6. Logs report the outcome. A crash during an effect leaves an uncertain outcome
   requiring human inspection before continuation.

Approval authority stays outside archived conversations and compaction summaries.
The dashboard token, databases, and runtime implementation are excluded from file
tools and generated-code snapshots. Host settings and executable startup locations
receive additional approval checks, even under a broad root grant.

## Persistence and recovery

Four SQLite databases separate memory/context, approval/activity authority, tasks,
and child sessions. Connections are short-lived and operations use transactions.
Generated artifacts and screenshots reside under the same private state directory.

The Agent checkpoints before executing a call and after recording its result.
`ready` and `pending_approval` checkpoints are resumable. An `executing` checkpoint
does not prove whether an external effect completed: recovery requires an operator
note, advances past that call without replaying it, then resumes normal planning.
That is intentionally not advertised as exactly-once external execution.

The scheduler caches waiting Agents so an approval round-trip does not discard
the live browser session. A service crash cannot preserve private browser pipes;
the agent must navigate again with approval before further browser input.

Recurring templates generate separate execution rows. Missed intervals coalesce,
and a prior paused/approval-waiting run prevents a backlog. File triggers use
bounded metadata polling and coalesce changes until a run can be queued.

## Memory and context

The original sanitized message stream is append-only. Working context is a
separate replaceable checkpoint. Compaction summarizes the context, links back to
source row IDs, and retains a fallback if the model cannot summarize. Large inputs
are excerpted for the summarizer while originals remain retrievable.

Automatic fact extraction is a model operation, so its results are evidence that
can be corrected or forgotten, not guaranteed truth. Credentials in recognized
formats are excluded. Generic secrets cannot always be detected from prose.

A fresh child receives only its task and common permission rules. Selected/shared
children receive explicit memory references checked against the parent's access.
Child context never grants new permissions. The parent chooses a different model,
whether tools are enabled, and whether screenshots should be attached as images.

## Generated-code isolation

Bubblewrap provides the boundary for arbitrary Python/Bash. Regular input files
are copied into bounded read-only snapshots; there are no live roots, host sockets,
credentials, or network access. A writable staging workspace holds outputs.
`prlimit` and bounded pipe readers constrain resources. Missing isolation support
causes a clear error; execution never falls back to the host.

Outputs return to real folders only through `commit_sandbox_file` and the normal
file broker. Downloads, host configuration changes, and permanent deletion remain
separate approved operations. This deliberately means a generated script cannot
perform an approved network operation directly; it requests the appropriate tool.

## Adding a tool

Write a normal Python function and register its input schema. Keep the schema and
function validation aligned. For a sensitive effect, call the provided approver
before changing state; allow `ApprovalRequired` to propagate. Do not catch it and
execute anyway, convert it into success, or introduce an auto-approve path.

Avoid secrets in arguments. Record useful operational detail without copying
whole private files into activity logs. Return structured success/failure evidence.
Document the function's purpose, permissions, side effects, limits, and any recovery
ambiguity. Test denied/pending approval before testing the allowed path.

## Deployment boundaries

The provided systemd unit follows Omarchy's graphical-session lifecycle so the
Wayland environment is present. It does not configure privileged ydotool access,
install Ollama, download models, or enable pre-login user lingering. Those are
separate operator setup actions requiring explicit permission where applicable.

Browser and desktop input is conservatively approval-gated on every operation.
The code cannot infer every site's/app's business meaning or prevent a separate
host process from changing what a coordinate or selector points to. Page/window
identity checks reduce accidental mismatch but do not establish semantic certainty.
