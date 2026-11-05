"""TEMP read-only probe (do not merge): capture one real review evidence payload."""
from __future__ import annotations

import json
from datetime import UTC, datetime

MARKER = "RIVAL_PERFORMANCE_PROBE="


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.analysis_evidence import build_evidence
    from parlay_bot.perpetuals.intelligence import market_context, model_calls
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore

    import importlib
    for name in ("parlay_bot.perpetuals.market_store", "parlay_bot.perpetuals.forward_audit",
                 "parlay_bot.perpetuals.model_competition", "parlay_bot.perpetuals.intelligence",
                 "parlay_bot.perpetuals.analysis_evidence"):
        module = importlib.import_module(name)
        for obj in vars(module).values():
            if isinstance(obj, type) and "initialize" in vars(obj):
                obj.initialize = lambda self, *a, **k: None  # read-only probe: schema exists
    s = Settings()
    store = PostgresHotStore(s.rival_database_url.get_secret_value(), read_only=True)
    market = PerpetualMarketStore(store)
    out: dict = {"now": datetime.now(UTC).isoformat()}
    try:
        venues = ("hyperliquid", "dydx", "bitget", "gate", "okx", "kucoin", "deribit",
                  "lighter", "phemex")
        evidence, _ = build_evidence(market, asset="BTC", venues=venues, horizon_minutes=5)
        evidence = {**evidence, "model_calls": model_calls(market, asset="BTC")}
        ctx = market_context(market, asset="BTC")
        if ctx:
            evidence["market_context"] = {**ctx, "source": "hyperliquid_okx"}
        out["evidence"] = evidence
    finally:
        store.close()
    return out


def main() -> int:
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001
        report = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    print(MARKER + json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
