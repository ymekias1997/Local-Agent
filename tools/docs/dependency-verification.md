# Dependency verification — 2026-10-04

Installed the Arch packages `ollama` 0.33.3-1 and `ydotool` 1.0.4-2.
No model files were downloaded. The application uses the Python standard library
and does not require additional runtime Python packages.

## Results

- All 89 automated tests passed, including worker processes, approval recovery,
  local HTTP integration, dashboard security, and Bubblewrap isolation.
- Chromium's private connection, blank-page snapshot, and PNG capture passed.
- Python compilation, Bash syntax, and dashboard JavaScript syntax checks passed.
- The package database reported no errors. Package file checks found no altered
  ydotool files; Ollama's data directory ownership differs from archive metadata
  because its packaged tmpfiles rule assigns it to `ollama:ollama`, as verified.
- No system or user services were marked failed at the time of the health check.
- A temporary Ollama server answered version and model-list requests successfully;
  its model list was empty. The server was stopped after verification.
- The first ydotool startup failed because `uinput` was not loaded. Loading the
  kernel module applied the packaged rule: `/dev/uinput` became `root:input`, mode
  `0660`. The user already belongs to `input`. A second startup successfully
  created a private socket; the daemon was stopped after verification.

## Remaining setup

No persistent Ollama, ydotool, or agent services were enabled. Before running an
agent, start an Ollama endpoint with an installed model. Model download and live
reasoning are deferred at the operator's request. Desktop clicks require a running
ydotool daemon; the startup check sent no clicks or keystrokes. The module was
loaded for the current boot; no boot configuration was changed.

These checks cover the toolkit and its dependencies. They do not establish that
every application on the host is fault-free, or validate live model reasoning and
desktop interaction.
