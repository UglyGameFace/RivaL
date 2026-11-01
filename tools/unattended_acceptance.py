"""Backward-compatible source-tree wrapper for the canonical main.py CLI.

Discloud may omit tools/ during upload. From the hosted app console prefer:
    python main.py --acceptance
    python main.py --health
    python main.py --mark-restart
    python main.py --verify-restart
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    source = Path(__file__).resolve().parents[1] / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from parlay_bot.perpetuals.acceptance_cli import main as run_acceptance

    return run_acceptance()


if __name__ == "__main__":
    sys.exit(main())
