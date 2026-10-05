#!/usr/bin/env python3
"""Install the reviewed user service after the operator supplies model and roots.

No packages or model files are downloaded. This command is an explicit system
configuration action: it writes one user unit and enables it for user sessions.
Desktop automation needs the graphical session's environment in the user manager;
the installer imports only the relevant display variables, never all secrets.
"""
import argparse
from pathlib import Path
import os
import subprocess
import sys


def quote(value):
    """Quote a literal systemd ExecStart argument, including percent specifiers."""
    text = str(value)
    if any(char in text for char in "\n\r\x00"):
        raise ValueError("Service arguments cannot contain newlines or NUL")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", required=True, action="append", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/local-llm-tools")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--replace", action="store_true", help="explicitly replace an existing local-agent unit")
    parser.add_argument("--print-only", action="store_true", help="review the exact unit without changing anything")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be 1024–65535")
    repository = Path(__file__).resolve().parent.parent
    command = [sys.executable, "-m", "local_llm_tools.service", "--state-dir", args.state_dir.expanduser().resolve(),
               "serve", "--model", args.model, "--port", str(args.port)]
    for root in args.root:
        path = root.expanduser().resolve(strict=True)
        if not path.is_dir():
            parser.error("Each --root must name a directory")
        command.extend(["--root", path])
    if args.vision:
        command.append("--vision")
    unit = "\n".join([
        "[Unit]", "Description=Local agent dashboard and scheduler", "After=graphical-session.target", "PartOf=graphical-session.target",
        # WorkingDirectory consumes the whole value as a path, unlike ExecStart's
        # argument parser: enclosing it in quotes makes it a non-absolute path.
        "", "[Service]", "Type=simple", "WorkingDirectory=" + str(repository).replace("%", "%%"),
        "ExecStart=" + " ".join(quote(part) for part in command),
        "Restart=on-failure", "RestartSec=5", "UMask=0077", "", "[Install]", "WantedBy=graphical-session.target", "",
    ])
    if args.print_only:
        print(unit)
        return
    destination = Path.home() / ".config/systemd/user/local-agent.service"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids silently replacing a service the user owns.
    with destination.open("w" if args.replace else "x") as stream:
        stream.write(unit)
    display = [name for name in ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_CURRENT_DESKTOP") if name in os.environ]
    if display:
        subprocess.run(["systemctl", "--user", "import-environment", *display], check=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "local-agent.service"], check=True)
    print(f"Installed {destination}. Starts with your user session; dashboard http://127.0.0.1:{args.port}.")


if __name__ == "__main__":
    main()
