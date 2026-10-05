"""Notify when new files appear in a watched directory."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from local_llm_tools import default_tools


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=60, help="check interval in seconds")
    args = parser.parse_args()
    folder = args.folder.expanduser().resolve(strict=True)
    if not folder.is_dir():
        parser.error("--folder must be a directory")
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")

    tools = default_tools([folder])
    known = {p.name for p in folder.iterdir()}
    print(f"Watching {folder} every {args.interval}s. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(args.interval)
            current = {p.name for p in folder.iterdir()}
            for name in sorted(current - known):
                result = tools.call("notify_user", {"title": "New file", "message": f"{name} appeared in {folder}"})
                if not result.get("delivered"):
                    print(f"New file: {name} appeared in {folder}", flush=True)
            known = current
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
