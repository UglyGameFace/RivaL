"""Owner-invoked no-key public feed probe; --persist opts in to PostgreSQL writes.

Use on the Discloud host to establish actual regional access. Not scheduled.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from parlay_bot.perpetuals.hyperliquid import HyperliquidMarketClient


def main() -> None:
    parser = argparse.ArgumentParser(description="RivaL public perpetual feed probe")
    parser.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--interval", type=int, choices=[5, 15, 60, 240], default=5)
    parser.add_argument("--bars", type=int, default=48)
    parser.add_argument("--persist", action="store_true", help="write to configured PostgreSQL")
    args = parser.parse_args()

    async def read():
        client = HyperliquidMarketClient()
        try:
            return await client.collect(
                assets=tuple(args.assets), interval_minutes=args.interval, bars=args.bars
            )
        finally:
            await client.aclose()

    batch = asyncio.run(read())
    summary = {
        "venue": "hyperliquid", "status": "validated_public_response",
        "contracts": sorted(x.contract for x in batch.contexts),
        "complete_candles": len(batch.candles),
        "observed_at": batch.observed_at.isoformat(),
        "persisted": False,
    }
    if args.persist:
        from parlay_bot.config import Settings
        from parlay_bot.perpetuals.market_store import PerpetualMarketStore
        from parlay_bot.storage.hot import PostgresHotStore

        settings = Settings()
        if settings.rival_storage_backend != "postgres" or settings.rival_database_url is None:
            raise RuntimeError("--persist requires configured PostgreSQL")
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
        try:
            result = PerpetualMarketStore(store).ingest(batch)
            summary["persisted"] = True
            summary["inserted_candles"] = result.inserted_candles
            summary["unchanged_candles"] = result.unchanged_candles
        finally:
            store.close()
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
