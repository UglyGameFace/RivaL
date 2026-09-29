from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from parlay_bot.discord_app.bot import run_discord_bot


if __name__ == "__main__":
    run_discord_bot()
