# Local agent for Omarchy

A Python agent runtime for **Ollama on Omarchy/Arch Linux**, with a terminal,
a local browser dashboard, durable memory, scheduled/event-triggered tasks,
individual approvals, and delegated agents. Python 3.10+ and the standard library
run the application. Optional system helpers enable isolated scripts and computer
interaction; nothing is downloaded or installed automatically.

## Start

Use the name of an **already installed** Ollama model. The default server is
`http://localhost:11434`; change it with `--base-url`.

```bash
cd /home/ymekias/Work/local-llm-tools
mkdir -p workspace
./scripts/run-agent.sh --model YOUR_MODEL --root "$PWD/workspace" --children
```

Terminal commands include `/compact`, `/memories`, `/approvals`, `/resume`,
`/recover INSPECTION_NOTE`, `/reset`, and `/exit`. History persists under the
agent identity (`--agent-id main` by default). `/reset` creates a new identity;
it does not delete the previous history.

Start the dashboard and task scheduler in another terminal:

```bash
./scripts/agent-control.sh serve --model YOUR_MODEL --root "$PWD/workspace"
```

Open `http://127.0.0.1:8765` and enter the token printed by:

```bash
./scripts/agent-control.sh token
```

Both interfaces share state in `~/.local/state/local-llm-tools`. Override with
`--state-dir PATH` (before the subcommand for `agent-control.sh`). The dashboard
only listens on loopback and requires a private bearer token for all APIs.

## What is included

| Area | Capabilities |
| --- | --- |
| Files | List with pagination, metadata, text and binary reading/writing, append, search, directory creation, approved file deletion |
| Internet | HTTP(S) text fetching, links, approved downloads, Brave/SearXNG search |
| Browser | Isolated Chromium profile, navigation, rendered page text, forms, clicks, screenshots |
| Desktop | Wayland screenshots, OCR, typing, keys, pointer/click controls |
| Code | Generate and run Python or Bash in a disposable, network-isolated workspace; explicitly commit outputs |
| Ollama | List/inspect models, loaded-model status, approved pulls/deletion, load/unload, model switching |
| Memory | Source-linked facts/preferences/lessons, search, correction, approved forgetting, archived conversations |
| Context | Automatic compaction, `/compact`, history retrieval, configurable context budget |
| Tasks | Durable queue, one-shot/interval schedules, file change triggers, pause/cancel/resume, reviewed crash recovery |
| Delegation | Separate processes, different models, fresh/shared/selected memory, queued messages, restartable workers |
| Visibility | Verbose terminal output, live dashboard events, retained redacted activity and approval records |

Use `./scripts/run-agent.sh --tools` to see the exact tool schemas. Some tools
need a configured file root or an installed helper before they can execute.

## Permission rules

Every download and permanent deletion requires a separate human decision. The
same applies to model downloads/deletion, memory deletion, and changes to known
host configuration locations. The review includes the exact action, affected
resources, purpose, expected effects, and size when known. A decision is bound
to that action and can be consumed once. **The old `--yes` flag is rejected.**

Because arbitrary website and application interactions can spend money, send
messages, or change accounts, **every browser navigation/input and every desktop
input asks for approval**. This is deliberately more conservative than trying
to infer the meaning of a button from its label. Credentials should be entered
through a separate human-managed workflow, not placed in task text or memories.

Foreground terminal approval asks immediately. A background task or child pauses
at the exact call until you approve or deny it in either interface:

```bash
./scripts/agent-control.sh approvals
./scripts/agent-control.sh approve APPROVAL_ID
./scripts/agent-control.sh deny APPROVAL_ID
```

A download approval does not permit installation or execution. Browser automatic
file downloads are denied; use the approved `download_file` tool. Page resources
needed to browse are fetched normally after navigation approval. `fetch_url`
reads HTTP text without saving an arbitrary file download.

Grant dedicated data/work folders with repeatable `--root`. File tools exclude
application code and private runtime state even inside a broader granted folder,
reject direct symlink mutations and multiple-hardlink files, and require approval
for known system/user configuration locations. These brokers are not protection
against a separate malicious host process racing filesystem paths. Read-only file
mode is `--read-only`; `--no-internet` removes web/browser tools and model pulls.

## Generated scripts and terminal commands

`run_code(language, code, arguments)` and `run_command(command)` use Bubblewrap:

- `/inputs/root0`, `/inputs/root1`, … contain read-only copies of regular files
  from granted folders. Symlinks, hardlinks, sockets, private state, and this
  application's source are excluded.
- `/workspace` is disposable writable output. There is no network, host desktop
  socket, inherited credential environment, or writable host project mount.
- `commit_sandbox_file` copies a chosen output into an allowed real directory.
  Known host configuration destinations still require approval.
- Execution has a 30-second CPU limit, approximately 35-second wall limit, a
  memory limit, and bounded output. Snapshot inputs cap at 5,000 files/100 MB.

There is **no unrestricted host-shell fallback**. Downloads must go through the
approval broker; generated scripts cannot silently run `curl` or package managers
on the host. Installed system libraries are available; missing Python packages
are not downloaded automatically.

`--script NAME=/path/to/script.py` registers existing Python source in this same
sandbox. It is not a privilege exemption.

## Ollama and model choice

The top agent can inspect installed models and switch its own model while keeping
working context. It can start children on different models. Downloads always
require approval first. A model must support native tool calling to operate tools;
use `--no-tools` or a child's `enable_tools=false` for reasoning-only models.

Use `--vision` with a vision-capable model to send captured screenshots as image
input. Without it, desktop OCR and browser text snapshots still work. Only trusted
capture tools can attach local image artifacts; arbitrary tool text cannot cause
an image/file attachment.

`--context-tokens 16384` sets Ollama's requested context capacity. The runtime
reserves room for tool schemas and an answer, then estimates when compaction is
needed. `--context-limit` adds a character ceiling. This is an estimate, not the
model's exact tokenizer; adjust for model/hardware limits. Too-small windows fail
with an actionable error rather than silently dropping tool schemas.

Protocol references: [Ollama chat/tool calls](https://docs.ollama.com/api/chat)
and [Ollama API operations](https://github.com/ollama/ollama/blob/main/docs/api.md).

## Long-term and short-term memory

Long-term memories contain facts, preferences, lessons, and source-linked summaries.
Working contexts contain the current instructions, exchanges, and tool results.
Automatic extraction is enabled in the terminal and service; `--no-auto-memory`
disables fact extraction while preserving conversation and compaction storage.

Automatic compaction and `/compact` preserve archived original messages, save a
summary with history references, and replace the working context. Compaction waits
until a tool batch and any approval are resolved. If model summarization fails,
a labeled excerpt fallback keeps source references. Summaries never grant permission.
Credentials recognized in structured fields/common formats are redacted; general
free-form secret detection is not guaranteed. Do not enter secrets into task text.

Top-level agents share long-term memory by default; use `--memory-scope private`
for a private context. Fresh children get only their task and policy. The top
agent can instead grant selected memories/background or shared memory access.
Correcting/forgetting a memory does not erase its original archived conversation.

```bash
./scripts/agent-control.sh memories --owner main --query preference
./scripts/agent-control.sh memory-update MEMORY_ID --owner main 'Corrected fact'
./scripts/agent-control.sh memory-forget MEMORY_ID --owner main
```

## Tasks, triggers, and recovery

The service must be running to execute queued/scheduled tasks. Each task has a
stable context identity. Scheduled templates and file watches persist across
service restarts; overdue intervals coalesce instead of flooding the queue.

```bash
./scripts/agent-control.sh add --model YOUR_MODEL --root "$PWD/workspace" \
  --interval 300 'Check notes.txt for unfinished tasks and notify me if needed.'
./scripts/agent-control.sh add --model YOUR_MODEL --root "$PWD/workspace" \
  --watch "$PWD/workspace" 'Review changed files and report useful next steps.'
./scripts/agent-control.sh tasks
./scripts/agent-control.sh pause TASK_ID
./scripts/agent-control.sh resume TASK_ID
./scripts/agent-control.sh cancel TASK_ID
```

`--at UNIX_TIMESTAMP` schedules a one-shot task. The agent also has scheduling and
task-control tools. Watchers poll bounded directory metadata; they are not an OS
journal, and rapidly created/deleted transient files can be missed.

Safe checkpoints resume after restart. If a crash happened during an action and
its outcome is unknown, the task pauses for inspection instead of replaying it.
After checking the affected application/file, record what happened and resume:

```bash
./scripts/agent-control.sh recover TASK_ID --note 'Inspected the file: the change is present.'
./scripts/agent-control.sh resume TASK_ID
```

Pause/cancel takes effect between actions; a tool already executing may finish.
Tasks are bounded by model/tool-call budgets and failures remain inspectable.
A long-running goal can continue through task steps and subsequent queued work;
the service does not assume an incomplete or failed task succeeded.

## Other agent instances

```bash
./scripts/agent-instance.sh start --model ANOTHER_INSTALLED_MODEL \
  --root "$PWD/workspace" --memory-mode fresh \
  --task 'Review notes.txt and report suggestions.'
./scripts/agent-instance.sh read SESSION_ID --wait 30
./scripts/agent-instance.sh send SESSION_ID 'Explain your most important suggestion.'
./scripts/agent-instance.sh stop SESSION_ID
./scripts/agent-instance.sh resume SESSION_ID
```

`spawn_agent` accepts `model`, `memory_mode` (`fresh`, `selected`, `shared`),
`context`, `memory_ids`, `enable_tools`, and `vision`. Selected IDs must be
accessible to the parent. Shared children can use shared memories and the parent's
explicitly delegated private records, while writing new shared memories.
Each child has separate working context and inherits the same permissions.

Follow-ups queue in order. Pending approvals block later messages until decided.
History and checkpoints survive worker restart. Four active children per parent
are allowed by default; `max_children` is a bounded operator configuration value.
Children cannot recursively launch grandchildren through these tools.

## Required local helpers

| Feature | Helpers |
| --- | --- |
| Agent reasoning | Running Ollama server and installed model |
| Isolated scripts | `bwrap`, `prlimit` |
| Browser | Chromium |
| Desktop capture | `grim`; optional `tesseract` for OCR |
| Desktop typing/keys | `wtype` and a Wayland session |
| Desktop clicks | `hyprctl`, `ydotool`, and an accessible ydotool daemon |
| Notifications | `notify-send` and a desktop notification session |

`capability_status` reports helpers without installing anything. On the development
machine, Ollama 0.33.3 and ydotool 1.0.4 were installed with operator authorization.
Both passed temporary daemon startup checks; no models were downloaded. The
`uinput` kernel module was loaded, and the packaged device rule grants the existing
`input` group access. No persistent services were enabled by these checks.
See [dependency verification](docs/dependency-verification.md) for results and
the remaining live-model setup.

For web search, configure either `BRAVE_SEARCH_API_KEY` or `SEARXNG_URL` (or
`--searxng-url`); SearXNG needs JSON search enabled. Fetching and approved file
downloads need no search key. See [SearXNG search](https://docs.searxng.org/dev/search_api.html)
and [Brave authentication](https://api-dashboard.search.brave.com/documentation/guides/authentication).

## Start with your user session

Review the generated systemd unit before installing it:

```bash
python scripts/install-user-service.py --model YOUR_MODEL \
  --root "$PWD/workspace" --print-only
```

Running the same command without `--print-only` explicitly writes and enables
`~/.config/systemd/user/local-agent.service`. It downloads nothing. An existing
unit is not overwritten unless you pass `--replace`. The template is also in
`deploy/local-agent.service.example`.

The user service starts with your user session; desktop actions require an active
Hyprland/Wayland session. This does not enable system-wide boot execution before
login or user lingering. Keep the model server available separately.

## Development and verification

```bash
python -m unittest discover -s tests -v
python tests/browser_smoke.py
```

Tests cover policy boundaries, memory/compaction, real worker processes with a
mock model server, dashboard HTTP security, and real Bubblewrap isolation.
The separate Chromium smoke check opens only `about:blank` and captures a PNG;
it makes no external navigation. Socket/namespace-restricted environments need
permission to run those integration tests. Tests never download models or packages.

Live Ollama reasoning, authenticated websites, and desktop input need validation
in your configured environment. Screenshots can contain private screen content;
artifacts and archives stay in the private state directory and are not automatically
purged. Inspect disk use as retained history grows.

Implementation responsibilities are documented in [architecture.md](docs/architecture.md).
The full agreed scope is recorded in [requirements.md](docs/requirements.md).
