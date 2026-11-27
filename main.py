from __future__ import annotations

import importlib
import sys
from pathlib import Path


def main() -> None:
    source = Path(__file__).resolve().parent / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    # Cap glibc malloc arenas BEFORE the bot spawns its worker threads.
    # Measured 2026-11-26: freed blocks stranding in dozens of per-thread
    # arenas (642 MB of anonymous mappings vs a 150 MB main heap) are what
    # climbs the bot's post-trim memory floor ~20 MB/h to the container
    # kill; malloc_trim alone cannot drain non-main arenas. Guarded: a
    # non-glibc host simply skips it.
    try:
        from parlay_bot.discord_app.memory_receipt import limit_allocator_arenas

        limit_allocator_arenas()
    except Exception as exc:  # noqa: BLE001 - allocator tuning must never block boot
        print(f"RivaL arena cap unavailable: {exc}", file=sys.stderr)

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
