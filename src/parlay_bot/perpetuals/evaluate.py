"""Inspect shadow-learning quality on already captured PostgreSQL candles.

Never turns an offline backtest into a live trading or Discord prediction.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from parlay_bot.perpetuals.features import live_examples, persisted_candles
from parlay_bot.perpetuals.learning import walk_forward


def main() -> None:
    parser = argparse.ArgumentParser(description="RivaL shadow online learning evaluation")
    parser.add_argument("--provider", choices=("hyperliquid", "dydx"), required=True)
    parser.add_argument("--asset", choices=("BTC", "ETH", "SOL"), default="BTC")
    parser.add_argument("--interval", choices=(5, 15, 60, 240), type=int, default=5)
    parser.add_argument("--horizon", choices=(5, 15, 60, 240), type=int, default=15)
    parser.add_argument("--count", type=int, default=5000)
    args = parser.parse_args()
    if args.horizon % args.interval or not 1 <= args.count <= 5000:
        parser.error("horizon must be a multiple of interval; count 1..5000")

    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    if settings.rival_storage_backend != "postgres" or settings.rival_database_url is None:
        raise RuntimeError("shadow evaluation requires configured PostgreSQL")
    contract, quote = (
        (args.asset, "USDC") if args.provider == "hyperliquid"
        else (f"{args.asset}-USD", "USD")
    )
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    try:
        records = PerpetualMarketStore(store).recent_candles(
            venue=args.provider, contract=contract, quote=quote,
            interval_minutes=args.interval, count=args.count,
        )
    finally:
        store.close()
    samples = live_examples(persisted_candles(records), horizon_minutes=args.horizon)
    result = walk_forward(samples, as_of=datetime.now(UTC))
    print(json.dumps({
        "provider": args.provider, "contract": contract, "quote": quote,
        "model": "shadow-online-logistic-v1", "training_mode": "shadow_only",
        "observed_candles": len(records), "causal_examples": len(samples),
        "matured_updates": result.updates,
        "evaluated": result.evaluated, "brier": result.brier,
        "baseline_brier": result.baseline_brier,
        "directional_accuracy": result.accuracy,
        "candidate_beats_baseline": result.candidate_beats_baseline,
        "deployed_model_changed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
