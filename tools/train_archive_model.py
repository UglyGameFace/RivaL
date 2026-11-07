"""Run the archive workflow on verified manifests or train standalone logistic fixtures."""

from __future__ import annotations

from parlay_bot.research.archive_training import main

if __name__ == "__main__":
    raise SystemExit(main(weekly=True))
