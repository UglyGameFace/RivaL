from __future__ import annotations

import importlib
import sys
from pathlib import Path


def main() -> None:
    source = Path(__file__).resolve().parent / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    module = importlib.import_module("parlay_bot.discord_app.bot")
    module.run_discord_bot()


if __name__ == "__main__":
    main()
