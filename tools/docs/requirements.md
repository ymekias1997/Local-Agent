# Requirements from the grilling interview

These requirements record the user's answers across the initial interview and
follow-up round. The user confirmed the combined scope and authorized its build.
See the README and architecture document for implementation details and setup limits.

## Confirmed requirements

- Use Python for the agent and tool library, with Ollama as the model runtime.
- Target Omarchy on Arch Linux, including its Hyprland/Wayland desktop. This
  supersedes the earlier Debian target.
- Provide file creation, reading, writing, and deletion, internet access, and the
  ability to run the user's scripts.
- Support scheduled work, reactions to events, and continued pursuit of assigned
  goals without a new prompt for every step.
- Support terminal commands, interactive browser use, and graphical desktop
  interaction including seeing the screen, clicking, and typing.
- Require explicit approval each time an action spends money, sends a message to
  another person, changes account/security settings, or permanently deletes data.
  Before approval, explain the purpose, exact action, affected resources,
  expected effects, and reversibility in detail. Report the actual outcome afterward.
- Require permission for every download to the machine, including files, model
  weights, packages, and updates. Explain source, purpose, destination, and size
  when known. Download approval does not by itself authorize installation or execution.
- Retain goals and progress across restarts, resume unfinished work, and support
  scheduled and event-triggered tasks after reboot. Provide inspect, pause,
  cancel, and resume controls.
- Maintain long-term memory and short-term working context as distinct concepts.
- Automatically retain useful facts, preferences, and lessons from completed
  work, with source references and controls to inspect, correct, and forget
  entries. Exclude passwords and access tokens from agent memory.
- Compact context automatically as it approaches its limit; also provide an
  explicit `/compact` command. Preserve original conversation/action history,
  save a linked summary, and allow original details to be retrieved later.
  Display when compaction happens. Never convert a pending approval into approval
  during summarization.
- Provide Bash controls and agent tools to create another agent instance and
  exchange tasks and replies with it.
- Let the top agent choose the model for each child and choose between a fresh
  child with only task instructions, selected context/memories, or access to
  shared long-term memory. Each child has its own working context. A fresh child
  still inherits permission rules. Model choices refer to models served by
  Ollama; another provider was not requested.
- Let the agent operate Ollama: discover, inspect, download, load, unload, and
  select models, including assigning different models to children. Downloads
  require approval and permanent model deletion falls under deletion approval.
- Allow agents to generate and execute Python and Bash scripts for ordinary
  work within granted access. Downloads and sensitive actions still require
  approval when initiated inside a script or command.
- Provide both a terminal interface and a local browser dashboard, sharing task
  controls, memory inspection, detailed activity, and approval requests.
- Provide verbose, understandable progress output across all agent activity,
  not only sensitive-action approvals. Show what is happening, its purpose,
  which agent/model is responsible, relevant tool actions and affected resources,
  results, failures, retries, and whether work is waiting for approval or input.
  Include visible task lifecycle, delegation, model changes, memory operations,
  and compaction events. Progress descriptions explain actions and decisions;
  they do not require exposing private model reasoning.
- Make activity available in both interfaces and retain an inspectable history.
  Redact credentials and avoid dumping private file contents into activity logs
  merely to make output verbose.
- Thoroughly comment all newly written or materially changed code, including
  Python, Bash, and dashboard code. Document module responsibilities, public
  interfaces, important data structures, control flow, permission checks,
  persistence/recovery behavior, and non-obvious decisions or edge cases.
  Comments should explain purpose and reasoning as well as behavior, remain
  accurate as code changes, and make the code approachable to someone learning
  or extending the project.

## Required enforcement and recovery properties

- Enforce permission boundaries outside the model's instructions. Unrestricted
  scripts, browser control, or desktop control cannot be assumed to obey them
  merely because an agent prompt requests approval.
- Approval must describe the specific proposed action. Blanket `--yes` approval
  does not meet the user's requirement to ask on every sensitive action or download.
- Persist pending approvals as pending during compaction and restart. Resuming
  work must not silently bypass approval or replay a completed external action.
- Memory sharing, fresh contexts, and model selection must not expand a child's
  permissions beyond those granted to it.
- Any implementation limitation that prevents these properties must be surfaced
  before presenting the relevant capability as ready for unattended use.

## Original audit before this build

This table records the starting point, before implementation of the expanded
requirements. It is retained as the audit trail; it is not the current feature list.

| Area | Implemented | Still required or unresolved |
| --- | --- | --- |
| Runtime | Ollama tool-calling loop | Direct Ollama management, model selection, live validation |
| Files | UTF-8 CRUD, directory creation, search within allowed roots | Broader file handling; current limits need review for desktop workflows |
| Internet | HTTP text fetch, downloads, configurable Brave/SearXNG search | Interactive browser, authenticated sessions, JavaScript sites |
| System actions | Registered Python scripts, notifications | General terminal and desktop controls |
| Proactivity | Repeating fixed task; separate notification-only file watcher | Event-to-agent triggers, persistent schedules, ongoing goals and recovery |
| Memory | Process-lifetime conversation; stored child task/reply records | Durable memory, automatic learning, retrieval, compaction, original-history archive |
| Approval | Delete/script prompt, optional blanket `--yes` | Every-occurrence sensitive-action approval with detailed explanation; blanket approval cannot satisfy this requirement |
| Child agents | Separate processes, queues, Bash controls, up to four workers | Top-agent model/context/memory choices and restart recovery |
| Interfaces | CLI and Bash commands | Dashboard, shared task/memory/approval controls |
| Observability | Basic CLI tool progress and worker records | Verbose activity across all components, consistent live views and retained history |
| Code documentation | Existing docstrings and selected comments | Thorough comments throughout new or materially changed code |
| Platform | Linux implementation, Python >=3.10 requirement | Omarchy desktop integration and validation |

## Status and implementation choices

All twelve planned interview questions have answers. The user subsequently added
verbose output and thorough code comments, then authorized implementation with
"go build it". The current implementation covers the agreed areas with conservative
approval gates, isolated generated-code execution, and explicit dependency checks.
Live model and desktop-input validation require the corresponding local services.

The default model, resource/concurrency budgets, granted directories, search
backend, memory retention limits, and detailed isolation mechanism remain
configuration or engineering choices. The earlier hard-coded four-child limit
and no-grandchildren policy are existing behavior, not newly confirmed requirements.
Changes to the scope or permission rules require an explicit decision rather
than an assumption made during implementation.
