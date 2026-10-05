# Local-Agent documentation

Local-Agent is a Python runtime that connects an Ollama model to validated local tools. It provides interactive chat, direct JSON tool calls, a browser dashboard, persistent memory, scheduled work, and separate child-agent processes. Its computer-interaction helpers target Omarchy/Arch Linux and Wayland. Python 3.10+ and the standard library run the application; individual capabilities require additional installed programs.

This guide describes the checked-in implementation. Tool entries link to their source modules; the live `--tools` output is the authoritative schema for the configuration you launch.

## Contents

- [Setup and first run](#setup-and-first-run)
- [How execution and permissions work](#how-execution-and-permissions-work)
- [Command-line interfaces](#command-line-interfaces)
- [Tool reference](#tool-reference)
- [Ollama and capability tools](#ollama-and-capability-tools)
- [Memory tools](#memory-tools)
- [Task tools](#task-tools)
- [Child-agent tools](#child-agent-tools)
- [Dashboard and HTTP API](#dashboard-and-http-api)
- [Embedding and development](#embedding-and-development)
- [Troubleshooting](#troubleshooting)

## Setup and first run

Run these commands from this repository's root. Replace `YOUR_MODEL` with a model already installed on your Ollama server. The project does not install Ollama or download a model on startup.

```bash
mkdir -p workspace
./tools/scripts/run-agent.sh --model YOUR_MODEL --root "$PWD/workspace" --children
```

Ollama defaults to `http://localhost:11434`; pass `--base-url` to use another endpoint. Native tool calling is needed for model-driven actions. `--no-tools` supports conversation with models that do not support tool calling. Use `--vision` only with a model that can accept images.

An example first prompt is: `List the files in the workspace, then create notes.txt containing a short project checklist.` Relative tool paths refer to the first `--root`. Repeat `--root` to grant additional folders. Roots must exist. The application source and private state are protected even if their parent directory is granted.

To run one task and exit:

```bash
./tools/scripts/run-agent.sh --model YOUR_MODEL --root "$PWD/workspace" \
  'Read notes.txt and summarize the remaining tasks.'
```

To inspect tools or invoke one without contacting a model:

```bash
./tools/scripts/run-agent.sh --root "$PWD/workspace" --children --tools
./tools/scripts/run-agent.sh --root "$PWD/workspace" \
  --call list_files --arguments '{"path":"."}'
./tools/scripts/run-agent.sh --call capability_status
```

These commands still initialize persistent state. Tool availability depends on flags, while helper availability is checked when the relevant tool executes. `--arguments` takes one JSON object; unknown argument names are rejected. A tool call that needs human approval cannot bypass it by using `--call`.

### Dependencies and configuration

| Feature | Requirement |
| --- | --- |
| Model-driven reasoning | Reachable Ollama server and an installed model |
| Python/Bash sandbox | `bwrap`, `prlimit`, Linux namespace support |
| Browser automation | `chromium` or `chromium-browser` |
| Desktop screenshots | `grim` and a usable Wayland session |
| OCR | `tesseract` in addition to screenshot support |
| Desktop text and keys | `wtype` and Wayland |
| Pointer actions | `hyprctl`, `ydotool`, and a working ydotool daemon/device setup |
| Desktop notifications | `notify-send` and a notification session; the tool reports delivery failure if unavailable |
| Web search | `BRAVE_SEARCH_API_KEY`, or `SEARXNG_URL`/`--searxng-url` with JSON search enabled |

`capability_status` checks executable paths, not whether the model server, display, or input daemon is operational. Fetching URLs and approved downloads do not require a search-provider key. Shell launchers accept `LOCAL_LLM_PYTHON` to select the Python interpreter; installation of this package is not required when using them. The package also defines `local-agent` and `agent-control` entry points for an installed environment in [pyproject.toml](tools/pyproject.toml).

## How execution and permissions work

[Agent](tools/local_llm_tools/agent.py) sends working context and enabled tool schemas to Ollama. The model returns an answer or tool calls. [ToolRegistry](tools/local_llm_tools/registry.py) validates arguments, invokes the registered Python function, and checks that its result can be JSON-encoded. The agent records results and asks the model for the next step. Calls within a batch execute sequentially. A run defaults to 16 model steps, permits at most 8 calls in one response, and caps the task at 64 tool calls; exhausting a budget produces an error rather than claiming completion.

### Access and approval boundaries

- File tools work within operator-granted roots. Ordinary permitted file writes do not ask for approval; permanent deletion and changes to recognized host-configuration locations do.
- File downloads, model downloads/deletion, and model-requested memory deletion require individual approval.
- Browser navigation, clicks, and typing, and all desktop input require approval for each action. Screenshots and inspection have separate read tools.
- Generated scripts run in Bubblewrap against bounded read-only snapshots and a disposable output folder, without network or a writable host mount. Moving an output to the host requires `commit_sandbox_file`.
- `--read-only` removes file mutation/download and generated-code tools, but is **not a global prohibition on side effects**: desktop input, browser interactions, memory, tasks, and model management have their own policies. Explicitly registered scripts remain sandboxed.
- `--no-internet` removes web/browser tools and model pulls. It does not disable the configured Ollama connection or desktop input.

The approval broker records the exact proposed operation, resources, and effects. A decision is tied to that invocation and consumed once. Changing arguments requires a new decision. The removed `--yes` option is rejected. A download approval does not authorize installation or execution.

In an interactive terminal, type `yes` to approve the displayed action. Non-interactive calls leave approval pending. Review background approvals in the dashboard or with `agent-control.sh approvals`, then `approve ID` or `deny ID`. Resume an interactive suspended task with `/resume` or `--resume`; the running service and child workers handle their own waiting work. A direct `--call` has no agent-loop continuation: invoke the same tool with the same arguments and identity again after deciding its approval. Its pending-approval exit code is 2; general errors use 1 and keyboard interruption uses 130.

### Persistence, context, and recovery

The default state directory is `~/.local/state/local-llm-tools`. Use the same `--state-dir` across interfaces to share approvals, tasks, and memories. It holds four SQLite databases (`runtime.sqlite3`, `memory.sqlite3`, `tasks.sqlite3`, `sessions.sqlite3`), a dashboard token, child logs, and generated/captured artifacts. State directories and databases receive restrictive permissions. Artifact/history retention is not automatically purged.

Conversation identity defaults to `main`. Returning with the same `--agent-id` restores its context. Long-term memories are distinct from archived messages and the active working context. Top-level CLI agents use shared memory by default; `--memory-scope private` limits memory access. Automatic extraction stores model-proposed facts after answers; disable extraction with `--no-auto-memory` while retaining history and checkpoints.

Automatic compaction and `/compact` summarize working context and retain references to archived originals. The context budget reserves space for schemas and answers; it estimates size rather than using the model's tokenizer. If summarization fails, an explicitly labeled excerpt fallback preserves references. Compaction waits for unresolved tool calls/approvals. `--context-tokens` requests Ollama context capacity; `--context-limit` adds a compaction threshold.

A checkpoint saved during execution does not prove an action completed. After a crash, inspect the affected file/application and activity before using recovery. Recovery records an operator note and skips replay of the uncertain call; then resume the task. Recognized credential formats are redacted, but arbitrary prose secrets and private screenshots cannot be reliably sanitized. Do not put credentials in task text or memories.

Sources: [runtime.py](tools/local_llm_tools/runtime.py), [memory.py](tools/local_llm_tools/memory.py), [architecture](tools/docs/architecture.md).

## Command-line interfaces

### Interactive agent: `tools/scripts/run-agent.sh`

| Option | Meaning/default |
| --- | --- |
| `--model NAME` | Required except for `--tools`/`--call` |
| `--base-url URL` | Ollama endpoint; `http://localhost:11434` |
| `--root PATH` | Repeatable file grants; none by default |
| `--state-dir PATH`, `--agent-id ID` | Persistent storage and conversation identity (`main`) |
| `--read-only`, `--no-internet` | Restrict the tool set as described above |
| `--children` | Enable child-agent tools |
| `--vision`, `--no-tools` | Image support or reasoning without tools |
| `--script NAME=PATH` | Register an existing Python script for sandboxed execution; repeatable |
| `--searxng-url URL` | Configure the SearXNG search endpoint |
| `--max-steps N` | 1–100 model steps; default 16 |
| `--context-tokens N` | 4,096–262,144 requested tokens; default 16,384 |
| `--context-limit N` | At least 4,000; default 60,000 character setting used in budget calculation |
| `--memory-scope shared\|private` | Default `shared` |
| `--no-auto-memory` | Disable automatic fact extraction |
| `--every SECONDS` | Save recurring positional task; minimum 5 seconds; service executes it |
| `--resume` | Continue this identity's suspended task |
| `--tools` | Print enabled JSON tool schemas |
| `--call NAME --arguments JSON` | Execute a single tool directly; arguments default to `{}` |

Interactive commands are `/compact`, `/memories`, `/approvals`, `/resume`, `/recover INSPECTION_NOTE`, `/reset`, and `/exit`. `/reset` switches to a newly generated identity without deleting old history or approvals. Reopen an old conversation using its `--agent-id`.

Source: [__main__.py](tools/local_llm_tools/__main__.py).

### Service and task controls: `tools/scripts/agent-control.sh`

Start a service in another terminal, then retrieve the token and open `http://127.0.0.1:8765`:

```bash
./tools/scripts/agent-control.sh serve --model YOUR_MODEL --root "$PWD/workspace"
# In another terminal:
./tools/scripts/agent-control.sh token
```

Custom state must go **before the subcommand**, for example `agent-control.sh --state-dir /path/to/state tasks`. `serve` starts the scheduler and dashboard and accepts `--port` (8765 by default). Only one service may own a state directory. Both `serve` and `add` accept model, root, endpoint, read-only, no-internet, automatic-memory, memory-scope, vision, context-token, search-endpoint, and max-step settings. Service-created configurations enable child tools. An `add` command saves its own supplied configuration; it does not inherit CLI options from an already-running service.

```bash
./tools/scripts/agent-control.sh add --model YOUR_MODEL --root "$PWD/workspace" \
  'Summarize notes.txt.'
./tools/scripts/agent-control.sh add --model YOUR_MODEL --root "$PWD/workspace" \
  --interval 300 'Check notes.txt for unfinished work.'
./tools/scripts/agent-control.sh add --model YOUR_MODEL --root "$PWD/workspace" \
  --watch "$PWD/workspace" 'Summarize changed files.'
./tools/scripts/agent-control.sh tasks
./tools/scripts/agent-control.sh approvals
./tools/scripts/agent-control.sh approve APPROVAL_ID
./tools/scripts/agent-control.sh deny APPROVAL_ID
./tools/scripts/agent-control.sh pause TASK_ID
./tools/scripts/agent-control.sh resume TASK_ID
./tools/scripts/agent-control.sh cancel TASK_ID
./tools/scripts/agent-control.sh events
```

`--at UNIX_TIMESTAMP` schedules a one-shot task. Intervals in `add` are numeric seconds, minimum 1. Prefer one trigger per task: a watch takes precedence over time checks if combined. Recurring/watch templates create separate execution rows; overdue intervals coalesce, and an existing queued, running, waiting, or paused execution prevents a backlog. Watches poll modification time and size for the root and up to 10,000 descendants, so transient changes can be missed. The scheduler executes queued work with one worker. Pause/cancel is cooperative between actions; an in-progress action may finish.

Other controls:

```bash
./tools/scripts/agent-control.sh compact TASK_ID
./tools/scripts/agent-control.sh recover TASK_ID --note 'Inspected the output: the file was written.'
./tools/scripts/agent-control.sh resume TASK_ID
./tools/scripts/agent-control.sh memories --owner main --query preference
./tools/scripts/agent-control.sh memory-update MEMORY_ID --owner main 'Corrected fact'
./tools/scripts/agent-control.sh memory-forget MEMORY_ID --owner main
```

The last command is an explicit operator deletion; unlike the model's `forget_memory`, it directly removes the owned memory without creating another approval request. Archived source messages remain.

Source: [service.py](tools/local_llm_tools/service.py).

### Child process controls: `tools/scripts/agent-instance.sh`

```bash
./tools/scripts/agent-instance.sh start --model YOUR_MODEL --root "$PWD/workspace" \
  --memory-mode fresh --task 'Review notes.txt and suggest improvements.'
./tools/scripts/agent-instance.sh read SESSION_ID --wait 30
./tools/scripts/agent-instance.sh send SESSION_ID 'Explain your first suggestion.'
./tools/scripts/agent-instance.sh status SESSION_ID
./tools/scripts/agent-instance.sh list
./tools/scripts/agent-instance.sh stop SESSION_ID
./tools/scripts/agent-instance.sh resume SESSION_ID
```

`start` supports `--base-url`, repeatable `--root`/`--script`/`--memory-id`, `--read-only`, `--no-internet`, `--memory-mode fresh|selected|shared`, `--context`, `--no-tools`, `--vision`, `--max-children` (4), `--searxng-url`, and `--max-steps` (20). `read` supports `--after-id`, `--limit` (5), and `--wait` (0–30 seconds). `--state-dir` may precede or follow these subcommands. Use the returned `next_after_id` for subsequent reads. A worker stays available after answering; stop it when finished to release an active-child slot.

Source: [sessions.py](tools/local_llm_tools/sessions.py).

### Optional systemd user service

Preview the exact generated unit before installation:

```bash
python3 tools/scripts/install-user-service.py --model YOUR_MODEL \
  --root "$PWD/workspace" --print-only
```

The same command without `--print-only` writes `~/.config/systemd/user/local-agent.service`, imports selected display variables, reloads systemd, and enables/starts the user service. `--replace` explicitly permits overwriting an existing unit. Other options are repeatable `--root`, `--state-dir`, `--port` (1024–65535), and `--vision`. This follows the graphical user session; it does not install dependencies, start Ollama, enable lingering, or arrange execution before login.

Source: [installer](tools/scripts/install-user-service.py), [example unit](tools/deploy/local-agent.service.example).

## Tool reference

The examples below use `{"name": "tool_name", "arguments": {...}}` to show a tool selection and its JSON arguments. In Python, execute that selection with `registry.call("tool_name", arguments)`; these objects are not shell commands. A tool must be enabled in the registry before it can be called. Required arguments are marked **required**; omitted optional arguments use the Python function defaults shown below.

`ToolRegistry.call` accepts an arguments object or a JSON-encoded object, rejects unknown tools and unexpected argument keys, validates the registered types, required fields, enums and bounds, checks the function signature, then checks that the result is JSON serializable. It does not fill schema defaults itself. Validation and execution failures raise exceptions to its caller; the agent runtime handles its own error reporting. Source: [registry.py](tools/local_llm_tools/registry.py).

### Registration and access boundaries

`default_tools(...)` creates the file, clock, notification and optional registered-script tools. Its defaults are no file roots, `allow_writes=False`, and `allow_internet=True`. File read tools are registered even with no roots, but calls cannot access files until existing directory roots are supplied. Internet tools require `allow_internet`; downloading additionally requires file writes. Browser, desktop, generated-code and model-management tools are added by the agent's capability registration, not by `default_tools` alone. Source: [core.py](tools/local_llm_tools/core.py), [capabilities.py](tools/local_llm_tools/capabilities.py).

File paths accept 1–4,096 characters. Relative paths resolve against the first allowed root; `~` expands before resolution. Resolved paths must remain within an allowed root, including after symlink resolution. Agent application code and private runtime state are protected even when an enclosing root is allowed. Regular-file operations reject files with multiple hard links. Mutations cannot target an allowed root itself or a directly supplied symlink. Parent directories must already exist for writes; use `make_directory` first. Source: [files.py](tools/local_llm_tools/files.py).

Ordinary workspace writes are automatic once write tools are enabled. Writes to recognized host configuration locations require `change_host_configuration` approval: system trees such as `/etc`, `/usr`, `/var`, and paths whose first component beneath the user's home starts with `.`. Such content must decode as UTF-8 and fit the 20,000-character review limit. File deletion and downloads have their own per-action approval. An omitted approval callback denies approval-dependent operations. These are the application's controls, separate from any permissions of a coding assistant editing this repository. Source: [files.py](tools/local_llm_tools/files.py), [web.py](tools/local_llm_tools/web.py).

## File tools

All tools in this section follow the path rules above. Write operations accept at most 1,000,000 bytes per call unless a tool explicitly says otherwise; that is a decimal MB. Text content also has a 1,000,000-character schema limit. Source for this section: [files.py](tools/local_llm_tools/files.py).

### `list_files`

Lists one directory without recursively reading its contents. Arguments: `path` defaults to `"."`; `limit` defaults to 100 (1–500); `offset` defaults to 0 (0–1,000,000). Returns the resolved `path`, `entries` containing `name` and `kind` (`file`, `directory`, or `symlink`), `truncated`, `next_offset`, and a pagination note. Entries use filesystem iteration order, not sorted order; directory changes may shift offsets. Continue using `next_offset` when `truncated` is true.

```json
{"name":"list_files","arguments":{"path":"reports","limit":50,"offset":0}}
```

### `file_info`

Inspects metadata. Argument: `path` **required**. Returns `path`, `kind` (`directory` or `file`), `bytes`, and `modified` as a Unix timestamp. Metadata follows an allowed symlink target; `kind` is a simple directory-versus-other classification, not a detailed filesystem type check. A missing path raises an error.

```json
{"name":"file_info","arguments":{"path":"reports/summary.txt"}}
```

### `read_file`

Reads UTF-8 text with line selection. Arguments: `path` **required**; `start_line` defaults to 1 (1–1,000,000, one-based); `max_lines` defaults to 200 (1–1,000). The entire source must fit 1,000,000 bytes and decode as UTF-8. Returns `path`, `start_line`, `text`, `total_lines`, and `truncated`. Output is capped at 20,000 characters, so a very long line can be cut even when the requested line range fits; use `read_bytes` for lossless chunks.

```json
{"name":"read_file","arguments":{"path":"reports/summary.txt","start_line":201,"max_lines":100}}
```

### `read_bytes`

Reads a bounded binary slice. Arguments: `path` **required**; `offset` defaults to 0 (0–9,223,372,036,854,775,807 bytes); `length` defaults to 16,384 (1–65,536 bytes). Returns `path`, `base64`, `next_offset`, and `eof`. Decode `base64` to obtain the exact bytes and continue at `next_offset` until `eof`; unlike `read_file`, the source has no 1 MB total-file restriction.

```json
{"name":"read_bytes","arguments":{"path":"images/chart.png","offset":0,"length":65536}}
```

### `search_files`

Searches a single file or recursively searches a directory for case-insensitive literal text, not a regular expression. Arguments: `path` and `query` **required**; `query` is 1–500 characters; `limit` defaults to 20 (1–100 matching lines). Examines at most 5,000 entries, skips files larger than 1 MB, NUL-containing files, non-UTF-8 data and inaccessible files. Returns `matches` with `file`, one-based `line`, `text` (up to 500 characters), and `text_truncated`, plus overall `truncated`. Results are bounded and are not a guarantee that every file was examined.

```json
{"name":"search_files","arguments":{"path":"notes","query":"next steps","limit":20}}
```

### `search_text`

Compatibility alias of `search_files`, with identical arguments, return value and limits.

```json
{"name":"search_text","arguments":{"path":"notes","query":"deadline"}}
```

### `create_file`

Requires write tools. Arguments: `path` **required**; `content` defaults to `""`. Exclusively creates a UTF-8 text file and fails if it already exists. Returns `path`, `bytes_written`, and `mode: "create_only"`.

```json
{"name":"create_file","arguments":{"path":"notes/todo.txt","content":"Review the report.\n"}}
```

### `write_file`

Requires write tools. Arguments: `path` and `content` **required**. Replaces the entire UTF-8 file or creates it if missing. Replacement is staged in a temporary file in the same directory and committed with `os.replace`; an existing file's permission bits are preserved. Returns `path`, `bytes_written`, and `mode: "overwrite"`.

```json
{"name":"write_file","arguments":{"path":"notes/todo.txt","content":"Report reviewed.\n"}}
```

### `append_file`

Requires write tools. Arguments: `path` and `content` **required**. Appends UTF-8 bytes, creating the file if missing; it does not automatically insert a newline. Returns `path`, `bytes_written`, and `mode: "append"`. The size cap applies to this call's content, not the resulting file size.

```json
{"name":"append_file","arguments":{"path":"notes/log.txt","content":"2026-10-05: report reviewed.\n"}}
```

### `write_text`

Requires write tools. Compatibility entry point for explicit text write modes. Arguments: `path` and `content` **required**; `mode` defaults to `"overwrite"`, with `"append"` and `"create_only"` also accepted. Uses the same write implementation and returns `path`, `bytes_written`, and `mode`.

```json
{"name":"write_text","arguments":{"path":"notes/new.txt","content":"First entry.\n","mode":"create_only"}}
```

### `write_bytes`

Requires write tools. Arguments: `path` and `base64_data` **required**; `mode` defaults to `"create_only"`, with `"overwrite"` and `"append"` also accepted. `base64_data` may contain at most 1,400,000 characters and must be valid base64; decoded content is limited to 1,000,000 bytes. Returns `path`, `bytes_written`, and `mode`. The example writes the UTF-8 bytes for `hello` followed by a newline, but arbitrary binary data is supported in ordinary workspace files.

```json
{"name":"write_bytes","arguments":{"path":"exports/hello.bin","base64_data":"aGVsbG8K","mode":"create_only"}}
```

### `make_directory`

Requires write tools. Argument: `path` **required**. Creates the directory and missing parents; an existing directory is accepted. Returns `path` and `created` (false when the target already existed). Creating a new host configuration directory requires approval.

```json
{"name":"make_directory","arguments":{"path":"reports/2026/october"}}
```

### `delete_file`

Requires write tools and explicit approval for this deletion. Argument: `path` **required**. Deletes one existing regular file, never a directory. Approval includes resolved path, size, modification time, inode and purpose; the tool checks the target again afterward and rejects a changed path or changed file. Returns `path` and `deleted: true`.

```json
{"name":"delete_file","arguments":{"path":"reports/obsolete-draft.txt"}}
```

## Web tools

These tools are registered only when internet access is enabled. HTTP helpers accept HTTP(S) URLs with a hostname, reject embedded credentials and control characters, and follow validated redirects. Cross-origin redirects drop custom headers other than `User-Agent` and `Accept`. Requests have a 15-second socket timeout and a checked 30-second overall read deadline. There is no public-host-only filter: HTTP(S) services reachable from the host may also be reachable through these tools. Retrieved content is untrusted data. Source for this section: [web.py](tools/local_llm_tools/web.py).

### `search_web`

Arguments: `query` **required** (1–1,000 characters); `limit` defaults to 5 (1–20). Uses the supplied `searxng_url`, otherwise `SEARXNG_URL`, in preference to `BRAVE_SEARCH_API_KEY`. A SearXNG server must allow JSON search; requests go to its `/search` endpoint. Without either provider configuration, the tool raises a setup error. Responses are capped at 2 MB. Returns `provider` (`searxng` or `brave`), `results` containing `title` (up to 500 characters), `url` (4,096), and `snippet` (2,000), and `untrusted_content: true`. Search does not fetch result pages.

```json
{"name":"search_web","arguments":{"query":"Python pathlib documentation","limit":5}}
```

### `fetch_url`

Argument: `url` **required** (1–4,096 characters). Fetches up to 2 MB of text, JSON, XML or XHTML; binary content types are rejected. HTML is parsed into visible text, excluding script/style/noscript/template content, without running JavaScript. Captures up to 100 link candidates and deduplicates their resolved HTTP(S) URLs. Returns final `url`, `content_type`, `text` (up to 20,000 characters), `links`, `truncated`, and `untrusted_content: true`. It has no output-pagination parameter; use downloads or another source when the text cap loses required content.

```json
{"name":"fetch_url","arguments":{"url":"https://example.com/"}}
```

### `download_file`

Requires internet access, write tools, an allowed destination and exact approval before issuing the download request. Arguments: `url` and `destination` **required** (each 1–4,096 characters). Downloads up to 10,000,000 bytes into a new file; existing targets and host configuration destinations are rejected. It does not issue a HEAD preflight and honestly reports the size as unknown within the limit during approval. Returns `path`, `bytes_written`, `mode: "create_only"`, and the final `url`. Downloads can therefore be larger than the ordinary 1 MB file-write limit.

```json
{"name":"download_file","arguments":{"url":"https://example.com/data.csv","destination":"downloads/data.csv"}}
```

## Clock, notifications and registered scripts

Source for this section: [system.py](tools/local_llm_tools/system.py), with script isolation in [sandbox.py](tools/local_llm_tools/sandbox.py).

### `current_time`

No arguments. Returns `iso` and `local` (the same local ISO timestamp with UTC offset), `timezone` (local timezone string), and `unix` (seconds since the epoch). Uses the host clock and timezone.

```json
{"name":"current_time","arguments":{}}
```

### `get_time`

Alias of `current_time`; no arguments and identical output.

```json
{"name":"get_time","arguments":{}}
```

### `notify_user`

Arguments: `title` **required** (1–200 characters) and `message` **required** (up to 4,000). Invokes `notify-send` if available, with a five-second timeout. Success returns `delivered: true` and `channel: "desktop"`. When the helper is missing, fails or times out, returns `delivered: false`, `channel: "tool_result"`, and the original `title` and `message` for the terminal/UI to render. This fallback is a result object, not proof of a displayed desktop notification.

```json
{"name":"notify_user","arguments":{"title":"Report ready","message":"The summary is saved in reports/summary.txt."}}
```

### `run_script`

Registered only when the operator supplies a nonempty `allowed_scripts` mapping. Arguments: `name` **required** (one of the configured names); `arguments` defaults to an empty list (up to 100 strings, each up to 4,096 characters). The mapped source must be an existing `.py` file. Its contents run through the isolated Python runner described below, with the same return structure as `run_code`; the original script's directory is not its working directory. It has no unrestricted host-execution fallback, and loading a named script does not grant network access or host file writes. In a library integration, `default_tools(allowed_scripts={"summarize": Path("/path/to/summarize.py")}, ...)` configures the name used in this example.

```json
{"name":"run_script","arguments":{"name":"summarize","arguments":["/inputs/root0/data.csv"]}}
```

## Isolated code execution

The agent registers these tools when `read_only` is false. They require Linux, working namespace support, `bwrap` (Bubblewrap), and `prlimit` (util-linux); absent helpers cause an error, with no host fallback. Python and Bash are invoked at `/usr/bin/python3` and `/usr/bin/bash` inside the sandbox, not from an activated project virtual environment. Source for this section: [sandbox.py](tools/local_llm_tools/sandbox.py); enablement: [capabilities.py](tools/local_llm_tools/capabilities.py).

Each run creates a separate state-directory workspace. Allowed roots are copied to `/inputs/root0`, `/inputs/root1`, and so on as read-only snapshots; application code, runtime state, symlinks, hard-linked files and special files are omitted. Inputs are limited to 5,000 files and 100,000,000 bytes across roots. Working directory and `HOME` are `/workspace`; write generated files there. Networking, host process IDs, credentials, desktop sockets and the host environment are not exposed. System executable/library directories are mounted read-only, and `/tmp` is a temporary filesystem.

Resource limits are 30 CPU seconds, a checked 35-second wall deadline, 1 GiB address space, 10 MiB per output file, and a process-count limit of 64. Captured stdout and stderr share a 20,000-byte limit. Exceeding captured output stops the process group; truncation does not mean that the program ran to completion. Run directories are retained for explicit export; the runner does not automatically copy outputs back or clean old artifacts.

### `run_code`

Arguments: `language` **required**, either `"python"` or `"bash"`; `code` **required**, up to 1,000,000 characters and 1,000,000 encoded bytes; `arguments` defaults to an empty list (up to 100 strings, each up to 4,096 characters). Executes source as `/program` with arguments. Returns `run_id`, `returncode`, `timed_out`, `truncated`, decoded `stdout` and `stderr`, host artifact `workspace`, and an `inputs` description. Inspect exit status and truncation before trusting an output artifact. Importing optional packages requires them to be available inside the mounted system environment.

```json
{"name":"run_code","arguments":{"language":"python","code":"from pathlib import Path\np = Path('/inputs/root0/notes.txt')\nPath('/workspace/line-count.txt').write_text(str(len(p.read_text().splitlines())) + '\\n')\nprint('Wrote line-count.txt')"}}
```

### `run_command`

Argument: `command` **required** (up to 1,000,000 characters and the runner's 1 MB encoded-source limit). Equivalent to running Bash source through `run_code`; returns its same result fields and obeys the same restrictions. Shell operators and pipelines run inside this disposable namespace. A command such as `rm` affects sandbox-writable files, not the original input roots.

```json
{"name":"run_command","arguments":{"command":"wc -l /inputs/root0/notes.txt > /workspace/line-count.txt"}}
```

### `commit_sandbox_file`

Arguments: `run_id`, `relative_path`, and `destination` **required** (each 1–4,096 characters at schema level). `run_id` must actually be the 32-character lowercase hexadecimal ID returned by a completed run. Exports an existing ordinary file under that run's workspace to an allowed host destination, overwriting an existing destination. Symlinks, hard links, paths escaping the workspace, and files larger than 1,000,000 bytes are rejected. Destination handling uses normal file-write and host-configuration approval rules; parent directories must exist. Returns `path`, `bytes_written`, and `mode: "overwrite"`. Replace the illustrative ID below with the real result's `run_id`.

```json
{"name":"commit_sandbox_file","arguments":{"run_id":"0123456789abcdef0123456789abcdef","relative_path":"line-count.txt","destination":"reports/line-count.txt"}}
```

## Isolated browser tools

Available in the agent when internet access is enabled. Require `chromium` or `chromium-browser`; the module does not install it. The driver starts headless Chromium lazily, controls it through private DevTools pipes, and uses a fresh profile under the runtime state directory, separate from the user's browser login sessions. Automatic browser downloads are denied; use approved `download_file` instead. There is no model-facing arbitrary JavaScript execution tool. Each protocol call has a 30-second timeout and a 20 MB buffered-response bound. Source for this section: [browser.py](tools/local_llm_tools/browser.py); enablement: [capabilities.py](tools/local_llm_tools/capabilities.py).

### `browser_navigate`

Argument: `url` **required** (1–8,000 characters), HTTP(S) with hostname and no embedded credentials. Requires approval for the exact navigation, which may run page scripts and follow redirects to other sites. Starts the browser if needed and returns `url`, the protocol `navigation` result, and a `note`. This confirms navigation started, not that loading finished; inspect the page using `browser_snapshot` afterward.

```json
{"name":"browser_navigate","arguments":{"url":"https://example.com/"}}
```

### `browser_snapshot`

No arguments. Reads current page `url`, `title`, `text` (first 20,000 characters of body text), and `elements` (first 150 links, buttons, inputs, textareas and selects). Element descriptions include tag, ID, name, text (up to 200 characters), type and href where applicable. Starts a blank browser if none exists; navigating first is normally useful. No separate approval is requested for the snapshot. Page text remains untrusted, and a snapshot does not guarantee an asynchronously loading page has finished.

```json
{"name":"browser_snapshot","arguments":{}}
```

### `browser_click`

Argument: `selector` **required** (1–2,000 characters), a CSS selector. Requires a live browser and approval containing the current page URL and selector. Rechecks the URL after approval and rejects a changed page. Clicks the first matching DOM element, raises an error if none matches, and returns `clicked: true`. A successful click can submit a form or perform another external action; the return value does not establish the outcome of a site's request.

```json
{"name":"browser_click","arguments":{"selector":"button[type='submit']"}}
```

### `browser_type`

Arguments: `selector` **required** (1–2,000 characters) and `text` **required** (up to 10,000). Requires a live browser and approval of the current URL, selector and exact text, then rechecks the URL. Focuses the first matching element, replaces its `.value`, and dispatches bubbling `input` and `change` events. Returns `typed: true`; missing elements cause an error. It does not simulate individual keystrokes or implement a generic contenteditable editor, and a site may submit data in response to these events.

```json
{"name":"browser_type","arguments":{"selector":"input[name='q']","text":"local agent documentation"}}
```

### `browser_screenshot`

No arguments. Starts the browser if needed, captures the current page as PNG, and saves it under the browser's state-directory artifact folder. Returns `path` and `image_path` pointing to the same local file; the result is not inline image bytes. No separate approval is requested for capture.

```json
{"name":"browser_screenshot","arguments":{}}
```

### `browser_close`

No arguments. Requests graceful Chromium shutdown, then terminates remaining browser processes and closes control pipes. Returns `closed: true`, including if no browser is running. It does not delete retained profile/screenshot artifacts. A later navigation can start the driver again.

```json
{"name":"browser_close","arguments":{}}
```

## Desktop tools

The agent registers desktop tools even if optional executables are missing; dependency failures appear when a tool is used. Helpers must be installed and available to the process in a working Wayland/Hyprland desktop session. Calls have a 15-second helper timeout and bounded textual output. Input actions require individual approval and compare the active window address before and after approval. They operate on the actual desktop, so a screenshot and target inspection are useful before sending input. Source for this section: [desktop.py](tools/local_llm_tools/desktop.py); enablement: [capabilities.py](tools/local_llm_tools/capabilities.py).

### `desktop_screenshot`

No arguments. Uses `grim` to save a PNG under the runtime state's `artifacts` directory. If `tesseract` is installed, runs OCR and returns up to 20,000 characters; otherwise `text` is null. Returns `path`, `image_path`, `text`, and `kind: "local_screen_capture"`. Capture itself has no approval prompt. If installed OCR fails, the tool raises even though the image may already have been saved.

```json
{"name":"desktop_screenshot","arguments":{}}
```

### `desktop_type`

Argument: `text` **required** (up to 10,000 characters). Requires `wtype` and `hyprctl`; approval includes exact text and the active window's address, class and title. After confirming the same window remains focused, inserts text using `wtype`. Returns `typed_characters`, the Python character count. It sends text to the current application and does not independently verify the resulting field content.

```json
{"name":"desktop_type","arguments":{"text":"Draft report ready for review."}}
```

### `desktop_key`

Argument: `key` **required** (1–80 characters), a key name accepted by `wtype`, such as `Return` or `Escape`. Requires `wtype` and `hyprctl`; approval is bound to the key and active window, which is rechecked. Sends one `wtype -k` key and returns `key` and `sent: true`. There is no separate modifier/chord parameter.

```json
{"name":"desktop_key","arguments":{"key":"Escape"}}
```

### `desktop_click`

Arguments: `x` and `y` **required** integers (0–32,000); `button` defaults to `"left"`, with `"right"` and `"middle"` also accepted. Requires `hyprctl`, `ydotool`, and a usable ydotool daemon. After approving the coordinates, button and currently focused window, rechecks that window, moves the pointer with Hyprland, then issues the click. Returns `clicked: true`, `x`, `y`, and `button`. Coordinates are screen positions; the previously focused window is not necessarily the application under the pointer.

```json
{"name":"desktop_click","arguments":{"x":640,"y":480,"button":"left"}}
```

## Ollama and capability tools

Source: [capabilities.py](tools/local_llm_tools/capabilities.py). These tools use the operator-configured `--base-url`; tool arguments cannot select another server. Ollama control requests disable proxies and redirects and bound responses to 4 MB. Ordinary requests time out after 120 seconds; pulls allow 3,600 seconds. Unless stated otherwise, results are the server's decoded JSON. Every `model` argument is required and must contain 1–300 characters. Replace `YOUR_MODEL` with an installed model name except when deliberately requesting a pull.

### `capability_status`

Arguments: `{}`. Returns an `executables` mapping of helper names to executable paths or `null`, plus script, desktop, and browser policy descriptions. Does not install or start helpers, probe Ollama, or confirm display/daemon access. The checked names include Bubblewrap, prlimit, Chromium variants, grim, wtype, ydotool, hyprctl, and tesseract; `notify-send` is not included in this report.

### `ollama_list`

Arguments: `{}`. Reads `/api/tags` and returns installed model metadata, normally under `models`. Use these names for switching or children.

### `ollama_status`

Arguments: `{}`. Reads `/api/ps` to report models currently loaded in server memory. Installed models and loaded models are different sets.

### `ollama_show`

Example: `{"model":"YOUR_MODEL"}`. Posts to `/api/show` to inspect metadata, model parameters, and capabilities when provided by the server. No approval is required.

### `ollama_pull`

Example: `{"model":"MODEL_TO_DOWNLOAD"}`. Requires approval before posting a non-streaming download to `/api/pull`. The review identifies the server, model registry, server-side storage destination, and unknown transfer size. Returns the final server response rather than streamed progress. Disabled by `--no-internet`; no automatic installation/download occurs when another model tool finds a missing model.

### `ollama_delete`

Example: `{"model":"YOUR_MODEL"}`. Requires approval, then sends `DELETE /api/delete` for that model. Empty successful responses become `{"ok":true}`. This permanently removes model files from the configured server.

### `ollama_load`

Example: `{"model":"YOUR_MODEL"}`. First checks installed names (accepting an omitted `:latest` suffix), then posts an empty generation request with `keep_alive="5m"`. Loads the model without a user prompt; no download or approval is involved.

### `ollama_unload`

Example: `{"model":"YOUR_MODEL"}`. Checks installation, then uses the same generation endpoint with `keep_alive=0` to release loaded model memory. Does not delete model files.

### `ollama_switch`

Example: `{"model":"YOUR_MODEL"}`. Checks installation and metadata, switches the attached agent's client, and checkpoints without discarding working context. Returns `previous` and `model`. When metadata includes a capability list, sets tool calling and vision according to that list; older servers without it retain configured settings. This affects subsequent model requests. A standalone broker without an attached agent cannot switch. The terminal's explicit `--model` takes precedence when starting it again.

## Memory tools

Source: [memory.py](tools/local_llm_tools/memory.py). The CLI registers these against the current agent identity and state directory. Memories have an owner, content, source references, sharing flag, and timestamps. Shared access permits reading; correction and deletion remain owner-only. None of these records grant tool permissions.

### `remember`

Required: `content` (nonblank string, at most 32,000 characters). Optional: `sources` (default `[]`, at most 30 strings).

```json
{"content":"The user prefers short progress updates.","sources":["User instruction in conversation"]}
```

Creates a record and returns `id`, `content`, `sources`, and `shared`. Records are shared when the caller's memory scope is `shared`; otherwise they are private. Recognized credential-like content is rejected. Source strings are provenance labels supplied by the caller, not automatically verified citations.

### `search_memory`

Optional: `query` (default `""`). Example: `{"query":"progress updates"}`. Performs case-insensitive substring search over accessible memory content and returns up to 20 records ordered by most recently updated. An empty query lists recent accessible memories. This is a SQLite text search, not embedding/vector retrieval.

### `recall_history`

Optional: `after` (nonnegative integer, default `0`). Example: `{"after":0}`. Returns up to 100 archived messages for this identity with `id > after`, ordered by ID. Each row includes the decoded message and timestamps/identity. Use the last returned ID to retrieve the next page. Compacted originals remain accessible; this tool does not expose other agents' histories.

### `update_memory`

Required: `memory_id`, `content` (nonblank, up to 32,000 characters).

```json
{"memory_id":"RETURNED_MEMORY_ID","content":"The user prefers detailed explanations for code reviews."}
```

Updates an owned record's content and timestamp, retaining its ID and sources. Returns `{"id":"…","updated":true}`. Rejects another owner's record and recognized credentials; no approval is needed for correction.

### `forget_memory`

Required: `memory_id`. Example: `{"memory_id":"RETURNED_MEMORY_ID"}`. Requests approval tied to the owned record's current content and revision, then deletes only that revision. If it changed while approval was pending, the old approval cannot delete the updated record. Returns `id`, `forgotten: true`, and a note that original history remains. Deleting a memory does not erase its archived source conversation.

## Task tools

Source: [service.py](tools/local_llm_tools/service.py). These tools create durable queue records; a running `agent-control.sh serve` is needed to execute them. Tasks inherit the caller's model, roots, endpoint, and policy configuration. Each execution gets a distinct `task-…` agent identity.

### `schedule_task`

Required: `prompt` (nonblank, at most 32,000 characters; recognized credentials rejected). Optional: `interval` (finite numeric seconds, at least 1), `scheduled_at` (finite nonnegative Unix timestamp), `watch_path` (path inside a granted root that neither contains nor lies inside private state). Omit optional values rather than passing JSON `null`.

```json
{"prompt":"Review notes.txt and report unfinished items.","interval":300}
```

With no trigger, queues work immediately. A timestamp creates a one-shot scheduled task; interval/watch options create templates. Returns the stored task row, including `id`, `agent_id`, decoded `config`, status, timing fields, result/error fields, and attempt count. Choose one trigger for predictable behavior: watch checks take precedence if mixed with time scheduling. Creating a task grants no approvals for later sensitive actions.

### `list_tasks`

Arguments: `{}`. Returns tasks whose recorded `task_owner` is this agent, filtered from the most recent 1,000 task rows. Operator-created CLI/dashboard tasks need not appear here; use the operator interface for the broader queue. Inspect `status`, `result`, `error`, and `approval_id` to distinguish finished, failed, and waiting work.

### `control_task`

Required: `task_id` and `action` (`pause`, `resume`, or `cancel`). Example: `{"task_id":"RETURNED_TASK_ID","action":"pause"}`. Checks that the task was created by this agent, updates its state, and returns its row. Resume accepts paused, failed, or cancelled tasks; recurring/watch tasks become scheduled again and ordinary tasks become queued. A waiting approval must be decided by the human first. Cancelling a schedule template does not implicitly cancel an execution row it already created; control that row separately.

## Child-agent tools

Source: [sessions.py](tools/local_llm_tools/sessions.py), [worker.py](tools/local_llm_tools/worker.py). Available with `--children` in the terminal and enabled by service configurations. Children run as separate Python processes, share granted host folders, and have independent working contexts. They inherit policy rather than gaining broader permissions. Children cannot recursively spawn grandchildren through these tools. Four active children per parent are allowed by default; idle workers still count.

Session IDs below are 12-character strings returned by `spawn_agent`. Examples use `abc123def456` as a placeholder.

### `spawn_agent`

All arguments are optional; the parent supplies the default model:

| Argument | Default and meaning |
| --- | --- |
| `task` | `""`; at most 32,000 characters; nonempty values must not be whitespace-only |
| `model` | Parent model; choose an installed model |
| `memory_mode` | `fresh`; one of `fresh`, `selected`, `shared` |
| `context` | `""`; explicit background, at most 32,000 characters |
| `memory_ids` | `[]`; at most 100 IDs, all accessible to the parent |
| `enable_tools` | `true`; use `false` for reasoning-only models |
| `vision` | Inherits the parent's value for the same model; defaults false when choosing a different model |

```json
{"task":"Review notes.txt and suggest three improvements.","memory_mode":"fresh","enable_tools":true}
```

Creates a persistent session, launches its worker, and queues the initial task if nonempty. Returns `id`, `pid`, `status`, `created`, and `updated`; this confirms startup, not completion of the task. Fresh mode rejects additional `context`/`memory_ids`. Selected mode provides explicit background and selected memories; shared mode provides shared memories plus the parent's accessible delegated records. New shared-mode memories are shared. Credential-like tasks/background are rejected. Model names are not preflighted against Ollama here, so an unavailable model can fail when the worker makes its request.

### `send_agent`

Required: `session_id`, `message` (nonblank, at most 32,000 characters). Example: `{"session_id":"abc123def456","message":"Expand your second suggestion."}`. Queues a follow-up and immediately returns `session_id`, `message_id`, and `status: "queued"`. The child processes messages in order; pending approval blocks subsequent work. Stopped/stopping/failed workers reject new messages. This tool is a task/follow-up queue, not an interruption mechanism.

### `read_agent`

Required: `session_id`. Optional: `after_id` (0–2^63−1, default 0), `limit` (1–100, default 5), `wait_seconds` (0–30, default 0).

```json
{"session_id":"abc123def456","after_id":0,"limit":5,"wait_seconds":30}
```

Waits for an assistant/error reply after the cursor or an ended/approval-waiting worker, then returns `session_id`, `status`, `messages`, and `next_after_id`. The returned rows include user messages as well as replies, ordered by message ID. Content is capped at 12,000 characters per message and 60,000 per call. Each row reports `content_truncated` and `content_length`; full content stays in the database. Advancing the cursor does not retrieve the truncated tail.

### `agent_status`

Required: `session_id`. Example: `{"session_id":"abc123def456"}`. Returns the worker's `id`, `pid`, `status`, `created`, and `updated`. Process identity is checked to detect exited workers without trusting a reused PID. Use `read_agent` for task output.

### `list_agents`

Arguments: `{}`. Returns accessible child status records, including old stopped sessions. Use it to find IDs and active workers; it does not return message histories.

### `stop_agent`

Required: `session_id`. Example: `{"session_id":"abc123def456"}`. Stops the worker using Linux pidfd process identity checks, escalates from SIGTERM to SIGKILL after a short wait if needed, marks queued/running messages cancelled, and returns status. Stored history and pending approvals remain. An already stopped/failed child simply returns its status. Stop can interrupt an effect, so inspect an uncertain outcome before recovery.

### `resume_agent`

Required: `session_id`. Example: `{"session_id":"abc123def456"}`. Restarts a stopped child with its original configuration and context and returns its status. It preserves pending approvals and does not turn cancelled messages back into runnable work. Send a new follow-up when appropriate after resuming; uncertain action checkpoints still require inspection.

## Dashboard and HTTP API

Source: [dashboard.py](tools/local_llm_tools/dashboard.py), [UI assets](tools/local_llm_tools/static). The service binds to loopback, normally `127.0.0.1:8765`. The HTML/CSS/JavaScript shell is public locally; API calls require `Authorization: Bearer TOKEN` and validate host/origin. The UI keeps the entered token only in page memory. `agent-control.sh token` prints the token as a JSON string; enter its value without the surrounding JSON quotes. It is stored in `dashboard.token` under the private state directory.

The dashboard offers task submission/control, activity, approvals, memory correction/deletion, compaction, and recovery. API POST bodies must be JSON objects with `Content-Type: application/json`, between 1 and 65,536 bytes.

| Method/path | Input and result |
| --- | --- |
| `GET /api/tasks` | Latest task rows |
| `GET /api/events?after=0` | Up to 200 events after an ID |
| `GET /api/approvals` | Pending approvals |
| `GET /api/memories?owner=main&q=text` | Up to 100 accessible shared/owner memories matching text |
| `POST /api/tasks` | `prompt`, optional `at`, `interval`, `watch`; creates a task under service configuration |
| `POST /api/control` | `id`, `action` (`pause`, `resume`, `cancel`) |
| `POST /api/recover` | `id`, `note`; records inspected outcome |
| `POST /api/compact` | Task `id`; compacts that context |
| `POST /api/approval` | Approval `id`, boolean `approved` |
| `POST /api/memory/update` | `owner`, memory `id`, replacement `content` |
| `POST /api/memory/forget` | `owner`, memory `id`; explicit operator deletion |

API clients can choose task text/timing but cannot expand the service's file roots or policy through the request. Errors use JSON and HTTP 400 for bad input, 403 for permission failures, 404 for unknown endpoints, and a generic 500 with details retained in service activity for unexpected failures.

## Embedding and development

The lightweight registry can be used without Ollama. From `tools/` (or with the package installed):

```python
from pathlib import Path
from local_llm_tools import default_tools

registry = default_tools(
    [Path("../workspace").resolve()],
    allow_writes=False,
    allow_internet=False,
)
print(registry.call("list_files", {"path": "."}))
print(registry.definitions())
```

`default_tools` includes file, clock, notification, optional registered-script, and optional web tools. It does **not** construct the full agent's memory, model-management, browser, desktop, generated-code, task, or child facilities. Unlike the agent CLI, it defaults to `allow_writes=False`. A full `Agent` attaches additional providers based on its configuration, including `state_dir`. Sensitive library operations need an approval callback; absence of one does not grant approval.

To add a tool, write a function returning JSON-serializable data and register its explicit object schema with `ToolRegistry.add` or `.register`. Definitions use function names/descriptions/parameter schemas; `.call` accepts either a dictionary or JSON string. Schema validation covers the project's supported subset (including integer ranges, strings, arrays, required keys, and unknown-key rejection); provider functions must validate additional semantic constraints. The model loop passes tool exceptions back as tool errors, while approval exceptions suspend execution. Follow the broker/checkpoint conventions for sensitive effects.

A standalone notification example is provided at [proactive_watch.py](tools/local_llm_tools/examples/proactive_watch.py):

```bash
cd tools
python3 -m local_llm_tools.examples.proactive_watch --folder ../workspace --interval 60
```

It polls direct child names and notifies when new entries appear; it does not use an LLM or the durable task scheduler. Its minimum interval is 5 seconds.

Existing verification commands, run from `tools/`:

```bash
python3 -m unittest discover -s tests -v
python3 tests/browser_smoke.py
```

The suite covers registry/policy boundaries, memory, sessions, scheduling, the authenticated dashboard, and sandbox behavior, with local mock model servers for integration. Socket/namespace restrictions and missing optional helpers affect which tests can run. The separate Chromium smoke script uses `about:blank` and a PNG capture. Neither command substitutes for checking live model quality or desktop interaction in your environment.

Further project records: [architecture](tools/docs/architecture.md), [requirements](tools/docs/requirements.md), [glossary](tools/GLOSSARY.md), and [historical dependency verification](tools/docs/dependency-verification.md). Machine-specific results in the last document are records of earlier checks, not proof of the current machine's setup.

## Troubleshooting

| Symptom | Check or next step |
| --- | --- |
| Cannot reach Ollama | Check the running server, `--base-url`, and selected installed model. A direct `ollama_list` call separates transport from reasoning problems. |
| Model replies but does not use tools | Confirm native tool-calling support and that `--no-tools` is absent. A reasoning-only model can converse but cannot drive actions. |
| Tool is not enabled | Inspect `--tools` with the same flags: writes, internet, scripts, and children change registration. |
| File access denied | Check granted roots, the first-root relative-path rule, protected source/state, symlinks/hardlinks, and configuration approvals. |
| Sandbox cannot start | Check `bwrap`, `prlimit`, and kernel/container namespace permissions. There is no unrestricted host-shell fallback. |
| Sandbox output is missing from the host | It is staged until `commit_sandbox_file` succeeds. Use the returned run ID and a relative output path. |
| Search is unavailable | Configure Brave credentials or a SearXNG JSON endpoint; URL fetching is independent. |
| Browser/desktop tool fails | Check helper paths and the graphical session/daemon. An installed executable alone does not prove it can access the display or input device. |
| Task stays queued/scheduled | Start the service with the same state directory; inspect trigger timing and any older paused/waiting execution. |
| Task/child waits for approval | Review pending approvals and decide in the operator CLI/dashboard. Sending another task does not resolve it. |
| Interrupted action cannot resume | Inspect the external outcome, record a recovery note, then resume. Do not assume the action failed or safely replay it. |
| Context window too small | Increase the configured context budget within the model/hardware limits or reduce enabled tools. Tool schemas also consume context. |
| Cannot start more children | Inspect `list_agents` and stop idle workers; completed answers do not terminate workers. |
| Dashboard rejects requests | Check token value, loopback URL/port, origin, and matching state directory. |
| State grows over time | Inspect retained databases, logs, screenshots, and sandbox artifacts; automatic purging is not implemented. |
