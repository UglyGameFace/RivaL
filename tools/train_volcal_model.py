"""Train one volcal_v1 artifact (standalone; see volcal_training.main).

The weekly job runs volcal_v1 through train_weekly_archive instead;
this wrapper is the non-weekly entry point (dry runs, fixtures).
"""
from parlay_bot.research.volcal_training import main

if __name__ == "__main__":
    raise SystemExit(main())
