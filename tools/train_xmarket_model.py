"""Train one xmarket_v1 artifact (standalone; see xmarket_training.main).

The weekly job runs xmarket_v1 through train_weekly_archive instead;
this wrapper is the non-weekly entry point (dry runs, fixtures).
"""
from parlay_bot.research.xmarket_training import main

if __name__ == "__main__":
    raise SystemExit(main())
