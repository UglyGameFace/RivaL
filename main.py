from __future__ import annotations

import importlib
import sys
from pathlib import Path


def main() -> None:
    source = Path(__file__).resolve().parent / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    # RivaL's shipped main.py is the supported operator entry point.
    # tools/ may be absent from Discloud's app package even after a successful
    # deploy upload, so acceptance diagnostics run from the packaged src tree.
    if len(sys.argv) > 1:
        module = importlib.import_module("parlay_bot.perpetuals.acceptance_cli")
        raise SystemExit(module.main(sys.argv[1:]))

    module = importlib.import_module("parlay_bot.discord_app.bot")
    module.run_discord_bot()


if __name__ == "__main__":
    main()
