# Local-Agent

Local-Agent lets a local Ollama model act through Python tools: work with files, fetch and search the web, control a browser or Wayland desktop, and run Python or Bash in an isolated sandbox. It also provides persistent memory, scheduled tasks, child agents, and a local dashboard for activity and approvals.

The runtime uses Python 3.10+ and the standard library. Computer interaction targets Omarchy/Arch Linux; optional capabilities need installed helpers such as Chromium, Bubblewrap, and Wayland utilities. Ollama must be running with an installed model that supports tool calling.

## Quick start

From the repository root, replace `YOUR_MODEL` with an installed model name:

```bash
mkdir -p workspace
./tools/scripts/run-agent.sh --model YOUR_MODEL --root "$PWD/workspace" --children
```

For the dashboard and background scheduler, run this in another terminal:

```bash
./tools/scripts/agent-control.sh serve --model YOUR_MODEL --root "$PWD/workspace"
```

Open `http://127.0.0.1:8765` and enter the token returned by `./tools/scripts/agent-control.sh token`. Both interfaces use `~/.local/state/local-llm-tools` by default. The Ollama endpoint defaults to `http://localhost:11434`.

File tools stay within granted folders. Generated scripts use disposable, network-isolated workspaces with explicit output commits. Downloads, permanent deletion, recognized host-configuration changes, browser interactions, and desktop input require individual approvals. The project does not automatically install dependencies or download models.

See **[DOCS.md](DOCS.md)** for setup, command-line options, every tool's arguments and examples, permissions, memory, scheduling, recovery, and troubleshooting. For implementation details, see the [architecture map](tools/docs/architecture.md).
