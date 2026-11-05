"""Kalshi 15-minute crypto Up/Down contracts: the six-year model vs Kalshi.

Kalshi's KX<COIN>15M markets resolve YES when CF Benchmarks' 60-second
average at the close is at least the average at the open. For each newly
opened contract the pooled model gives P(up) from bars that closed at or
before the open; the real order-book asks seen at that moment are frozen
next to it. Grading uses Kalshi's own published result. Value bets are
scored at the real ask PLUS Kalshi's taker fee. Read-only: no orders.
These are the same contracts Robinhood and Coinbase route through Kalshi.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.discord_app.kalshi_public import _ROOT, parse_orderbook

_LOG = logging.getLogger(__name__)
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M"}
ISSUE_WINDOW = timedelta(minutes=5)  # the forecast only uses bars up to the open
# Kalshi's prices are frozen at the first look within this time of the open,
# so the model (start-time inputs) is compared with the market at the start.
START_PRICE_WINDOW = timedelta(seconds=90)
START_PRICE_FROZEN_FROM = "2026-11-04T03:15:00+00:00"  # earlier rows: late asks
EDGE = 0.05


def kalshi_fee(price: float) -> float:
    """Kalshi taker fee for one contract: 7% x P x (1 - P), rounded up to 1 cent."""
    return math.ceil(round(0.07 * price * (1 - price) * 100, 6)) / 100


def score_vs_market(rows, *, days: int, edge: float = EDGE) -> dict:
    """(P(up), yes_ask, no_ask, result) rows -> Brier vs market and value bets."""
    n = len(rows)
    if not n:
        return {"contracts": 0, "score_window_days": days}
    model_err = market_err = 0.0
    stakes = profit = 0.0
    bets = wins = 0
    for p, ya, na, y in rows:
        mid = ya / (ya + na)  # market's implied P(up), spread removed
        model_err += (p - y) ** 2
        market_err += (mid - y) ** 2
        for side_p, ask, win in ((p, ya, y == 1), (1 - p, na, y == 0)):
            cost = ask + kalshi_fee(ask)
            if side_p - cost >= edge:
                bets += 1
                wins += win
                stakes += cost
                profit += (1.0 if win else 0.0) - cost
    return {
        "contracts": n, "score_window_days": days,
        "model_brier": model_err / n, "market_brier": market_err / n,
        "value_bets": bets, "value_bet_wins": wins,
        "return_per_dollar_after_fees": profit / stakes if stakes else None,
    }


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class KalshiUpDown:
    def __init__(self, store, forecaster, client: httpx.AsyncClient | None = None) -> None:
        self.store = store
        self.forecaster = forecaster
        self._client = client
        self._ready = False
        self._start_book: dict[str, tuple] = {}

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_kalshi_updown (
                    ticker TEXT PRIMARY KEY, asset TEXT NOT NULL,
                    opens_at TEXT NOT NULL, closes_at TEXT NOT NULL,
                    open_reference DOUBLE PRECISION, issued_at TEXT NOT NULL,
                    probability_up DOUBLE PRECISION, family TEXT, model_sha256 TEXT,
                    venues INTEGER, yes_ask DOUBLE PRECISION, no_ask DOUBLE PRECISION,
                    yes_size DOUBLE PRECISION, no_size DOUBLE PRECISION,
                    result_yes INTEGER, expiration_value DOUBLE PRECISION,
                    settled_at TEXT)"""
            )
        self._ready = True

    async def _get(self, path: str, params: dict | None = None):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        response = await self._client.get(_ROOT + path, params=params)
        response.raise_for_status()
        return response.json()

    def _known(self, tickers) -> set[str]:
        if not tickers:
            return set()
        marks = ",".join("?" for _ in tickers)
        with self.store.connect() as conn:
            return {str(r["ticker"]) for r in conn.execute(
                f"SELECT ticker FROM rival_kalshi_updown WHERE ticker IN ({marks})",
                tuple(tickers)).fetchall()}

    def _record(self, values: tuple) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_kalshi_updown
                   (ticker,asset,opens_at,closes_at,open_reference,issued_at,
                    probability_up,family,model_sha256,venues,yes_ask,no_ask,
                    yes_size,no_size)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                values,
            )

    def _apply_results(self, results: list[tuple]) -> int:
        """One connection for every published result (was one per row)."""
        if not results:
            return 0
        self.initialize()
        updated = 0
        stamp = datetime.now(UTC).isoformat()
        with self.store.connect() as conn:
            for result_yes, value, ticker in results:
                updated += conn.execute(
                    """UPDATE rival_kalshi_updown SET result_yes=?, expiration_value=?,
                       settled_at=? WHERE ticker=? AND result_yes IS NULL""",
                    (result_yes, value, stamp, ticker),
                ).rowcount
        return updated

    # Every database call below runs in a worker thread. These coroutines
    # run on the Discord event loop; a synchronous PostgreSQL round trip
    # here blocked it (stall watch, Nov 5: 64 stalls >1 s in settle()).
    async def issue(self, now: datetime | None = None) -> int:
        """Record every Kalshi 15-minute contract that opened in the last 5 minutes."""
        import asyncio

        await asyncio.to_thread(self.initialize)
        clock = now or datetime.now(UTC)
        saved = 0
        for asset, series in SERIES.items():
            try:
                rows = (await self._get("/markets", {"series_ticker": series,
                                                     "status": "open", "limit": 5}))["markets"]
            except (httpx.HTTPError, KeyError, ValueError):
                _LOG.info("Kalshi up/down listing unavailable series=%s", series)
                continue
            fresh = []
            for row in rows:
                opens, closes = _time(row.get("open_time")), _time(row.get("close_time"))
                ticker = str(row.get("ticker") or "")
                if (opens and closes and re.fullmatch(r"[A-Z0-9-]{5,80}", ticker)
                        and ticker.startswith(series + "-")
                        and opens <= clock < opens + ISSUE_WINDOW
                        and closes - opens == timedelta(minutes=15)):
                    fresh.append((ticker, opens, closes, row.get("floor_strike")))
            known = await asyncio.to_thread(self._known, [f[0] for f in fresh])
            fresh = [f for f in fresh if f[0] not in known]
            for ticker, opens, closes, reference in fresh:
                if ticker not in self._start_book and clock - opens <= START_PRICE_WINDOW:
                    try:
                        self._start_book[ticker] = parse_orderbook(
                            await self._get(f"/markets/{ticker}/orderbook"))
                    except (httpx.HTTPError, ValueError, TypeError):
                        _LOG.info("Kalshi book unavailable ticker=%s", ticker)
                # No start-time book: the contract is kept but never compared.
                yes_ask, no_ask, yes_size, no_size = self._start_book.get(
                    ticker, (None, None, None, None))
                forecast = None
                try:
                    forecast = await asyncio.to_thread(
                        self.forecaster.forecast, asset=asset, horizon=15,
                        opens_at=opens, now=clock)
                except Exception:  # noqa: BLE001 - the market record is still kept
                    _LOG.warning("Kalshi up/down forecast failed ticker=%s", ticker)
                if forecast is None and clock < opens + ISSUE_WINDOW - timedelta(seconds=75):
                    # The bar that closed at the open may not be collected yet;
                    # retry next minute rather than record a contract unforecast.
                    continue
                forecast = forecast or {}
                await asyncio.to_thread(self._record, (
                    ticker, asset, opens.isoformat(), closes.isoformat(),
                    float(reference) if isinstance(reference, (int, float)) else None,
                    clock.isoformat(), forecast.get("probability"),
                    forecast.get("family"), forecast.get("model_sha256"),
                    forecast.get("venues"), yes_ask, no_ask, yes_size, no_size,
                ))
                self._start_book.pop(ticker, None)
                saved += 1
        if len(self._start_book) > 500:
            self._start_book.clear()
        return saved

    async def settle(self) -> int:
        """Copy Kalshi's own published results onto recorded contracts."""
        import asyncio

        results: list[tuple] = []
        for series in SERIES.values():
            try:
                rows = (await self._get("/markets", {"series_ticker": series,
                                                     "status": "settled", "limit": 40}))["markets"]
            except (httpx.HTTPError, KeyError, ValueError):
                continue
            for row in rows:
                result = row.get("result")
                if result not in ("yes", "no"):
                    continue
                try:
                    value = float(row.get("expiration_value"))
                except (TypeError, ValueError):
                    value = None
                results.append((int(result == "yes"), value, str(row.get("ticker"))))
        return await asyncio.to_thread(self._apply_results, results)

    def summary(self, *, asset: str | None = None, days: int = 7, edge: float = EDGE) -> dict:
        """Model vs Kalshi on settled contracts; value bets at real ask + fee."""
        self.initialize()
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        query = ("SELECT probability_up,yes_ask,no_ask,result_yes FROM rival_kalshi_updown "
                 "WHERE result_yes IS NOT NULL AND probability_up IS NOT NULL "
                 "AND yes_ask IS NOT NULL AND no_ask IS NOT NULL AND issued_at>=?")
        params: tuple = (max(since, START_PRICE_FROZEN_FROM),)
        if asset:
            query += " AND asset=?"
            params += (asset,)
        with self.store.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return score_vs_market(
            [(float(r["probability_up"]), float(r["yes_ask"]), float(r["no_ask"]),
              int(r["result_yes"])) for r in rows], days=days, edge=edge)

    def challenger_summary(self, family: str, *, asset: str | None = None,
                           days: int = 7, edge: float = EDGE) -> dict:
        """A shadow challenger scored against Kalshi exactly like the main model.

        A RivaL 15-minute forecast whose reference candle closes at a contract's
        open predicts the same event (price at open+15 vs at open). Its
        probability (averaged across exchanges) meets the start-time asks, real
        fees and the official result. Read-only: nothing new is issued.
        """
        self.initialize()
        since = max((datetime.now(UTC) - timedelta(days=days)).isoformat(),
                    START_PRICE_FROZEN_FROM)
        with self.store.connect() as conn:
            contracts = conn.execute(
                "SELECT asset,opens_at,yes_ask,no_ask,result_yes FROM rival_kalshi_updown "
                "WHERE result_yes IS NOT NULL AND yes_ask IS NOT NULL AND no_ask IS NOT NULL "
                "AND issued_at>=?" + (" AND asset=?" if asset else ""),
                (since, asset) if asset else (since,)).fetchall()
            scored = []
            for row in contracts:
                opens = _time(row["opens_at"])
                if opens is None:
                    continue
                call = conn.execute(
                    """SELECT AVG(probability_up) AS p, COUNT(*) AS n
                       FROM rival_model_shadow_forecasts
                       WHERE family=? AND asset=? AND horizon_minutes=15
                         AND target_opened_at=? AND issued_at<=?""",
                    (family, row["asset"], (opens + timedelta(minutes=10)).isoformat(),
                     (opens + timedelta(minutes=5)).isoformat())).fetchone()
                if call is not None and int(call["n"] or 0) > 0:
                    scored.append((float(call["p"]), float(row["yes_ask"]),
                                   float(row["no_ask"]), int(row["result_yes"])))
        return score_vs_market(scored, days=days, edge=edge)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
