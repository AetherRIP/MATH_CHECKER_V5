"""Generate silent Windows auto-start files for the tiny agent (§15).

Kept out of backend code so the agent install stays a copy-pasteable script;
it writes a .vbs silent launcher + instructions into a target folder.
Usage: python scripts/setup_agent_autostart.py --target path\\to\\folder
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.distributed.tiny_agent import autostart_file  # noqa: E402
from agent.agent import AGENT_DIR  # location of agent.py


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True,
                        help="folder to write the autostart files into")
    parser.add_argument("--force", action="store_true",
                        help="write them even though this version disables the "
                             "Tiny Agent (documented re-enable path)")
    args = parser.parse_args()
    try:
        vbs = autostart_file(AGENT_DIR / "agent.py", Path(args.target),
                             allow_disabled=args.force)
    except Exception as exc:  # noqa: BLE001 — the refusal is the expected path
        # AppError when the switch is off: nothing is written, and the message
        # already says exactly how to turn the agent back on.
        print(f"No auto-start files were written. {exc}")
        return 2
    print("Auto-start launcher written:", vbs)
    print("Follow AUTOSTART_INSTRUCTIONS.txt in the same folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
