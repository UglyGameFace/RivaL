"""The pooled multi-year model's forecast for a Polymarket Up/Down contract.

A contract asks: will the price at the end of [opens_at, expires) be at or
above the price at opens_at? The pooled model answers exactly that from bars
that closed AT OR BEFORE opens_at, averaged over liquid exchanges. It is then
compared with the market's own price seen a little later (a head start for
the market, so the comparison is conservative for RivaL).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import persisted_candles
from parlay_bot.perpetuals.markets import asset_for_contract
from parlay_bot.perpetuals.pooled_features import WINDOW, bar_close, model_vector
from parlay_bot.perpetuals.pooled_model import probability

_LOG = logging.getLogger(__name__)
# dYdX often has no trades in a 5-minute bar (stale price): never used here.
EXCLUDED_VENUES = frozenset({"dydx"})
MIN_VENUES = 3


class PolymarketPooledForecaster:
    def __init__(self, market, models) -> None:
        self.market = market
        self.models = models

    def _streams(self, asset: str, since: datetime) -> tuple[dict, dict]:
        import time

        cached = getattr(self, "_stream_cache", None)
        if cached and time.monotonic() - cached[0] < 600:
            rows = cached[1]
        else:
            # Uses idx_perp_market_candles_observed (interval_minutes,
            # first_observed_at); refreshed every 10 minutes, not per contract.
            with self.market.store.connect() as conn:
                rows = [dict(r) for r in conn.execute(
                    """SELECT DISTINCT venue, contract, quote FROM perp_market_candles
                       WHERE interval_minutes=5 AND first_observed_at>=?""",
                    (since.isoformat(),),
                ).fetchall()]
            self._stream_cache = (time.monotonic(), rows)
        out: dict[str, dict[str, tuple[str, str]]] = {}
        for row in rows:
            coin = asset_for_contract(str(row["contract"]))
            if coin and str(row["venue"]) not in EXCLUDED_VENUES:
                out.setdefault(coin, {})[str(row["venue"])] = (str(row["contract"]),
                                                              str(row["quote"]))
        return out.get(asset, {}), out.get("BTC", {})

    def _bars(self, venue, source, start, now, count):
        rows = self.market.recent_candles(venue=venue, contract=source[0], quote=source[1],
                                          interval_minutes=5, count=count, as_of=now)
        bars = [b for b in persisted_candles(rows) if bar_close(b) <= start]
        return bars if bars and bar_close(bars[-1]) == start else None

    def forecast(self, *, asset: str, horizon: int, opens_at: datetime,
                 now: datetime | None = None) -> dict | None:
        """{'probability', 'family', 'venues'} or None when not computable."""
        if horizon not in (5, 15, 60):
            return None
        feed = getattr(self.market, "binance_context", None)
        context = feed.bundle(asset) if feed else None
        doc = self.models.active(horizon, context=context is not None)
        if doc is None:
            return None
        clock = now or datetime.now(UTC)
        start = opens_at.astimezone(UTC)
        # Only before the first post-start bar would add information.
        if not start <= clock < start + timedelta(minutes=5):
            return None
        streams, btc = self._streams(asset, start - timedelta(minutes=15))
        probabilities = []
        for venue, source in sorted(streams.items()):
            try:
                bars = self._bars(venue, source, start, clock, WINDOW + 6)
                if bars is None:
                    continue
                btc_bars = None
                if asset != "BTC" and venue in btc:
                    btc_bars = self._bars(venue, btc[venue], start, clock, 30)
                vector = model_vector(doc, bars, asset=asset, btc_candles=btc_bars,
                                      context=context)
                if vector is not None:
                    probabilities.append(probability(doc, vector))
            except Exception:  # noqa: BLE001 - one venue never blocks the others
                _LOG.warning("Pooled Polymarket input unavailable venue=%s", venue)
        if len(probabilities) < MIN_VENUES:
            return None
        return {"probability": sum(probabilities) / len(probabilities),
                "family": doc["_family"], "model_sha256": doc["_sha256"],
                "venues": len(probabilities)}
