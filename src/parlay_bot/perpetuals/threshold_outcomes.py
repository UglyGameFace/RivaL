"""Settled outcomes for the threshold-market estate (sweep fix,
2026-11-24).

The G-C candidates (options-implied BTC weekly; PM-vs-Kalshi
threshold gap) were registered NOT_MEASURABLE with the cause
``no_settled_threshold_outcomes``: the snapshot recorders capture
threshold markets all day, but nothing anywhere recorded how those
markets actually settled, so no candidate on this estate could ever
be graded. This module is the missing recorder. On the same hourly
cycle as the snapshot collectors it resolves expired threshold
markets against the venues' own published resolutions -- Polymarket
Gamma (closed flag + final outcome prices) and the Kalshi market
record (result field) -- and stores one row per market in
``rival_threshold_outcomes``.

Recording only: no grading, no criteria. A market whose venue record
is not final, or whose final prices are not decisive (a void /
50-50 market), stays unresolved -- never guessed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx

from parlay_bot.perpetuals.kalshi_thresholds import KALSHI_BASE
from parlay_bot.perpetuals.threshold_markets import GAMMA_BASE

_LOG = logging.getLogger(__name__)

OUTCOMES_TABLE = "rival_threshold_outcomes"
OUTCOMES_DDL = f"""
CREATE TABLE IF NOT EXISTS {OUTCOMES_TABLE} (
    venue TEXT NOT NULL,
    market_key TEXT NOT NULL,
    asset TEXT,
    contract_type TEXT,
    strike_low DOUBLE PRECISION,
    strike_high DOUBLE PRECISION,
    expiry_at TEXT,
    resolved_yes INTEGER,
    final_yes_price DOUBLE PRECISION,
    resolved_at TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (venue, market_key)
);
"""

# Per-cycle bounds: the weekly estate is small; these caps keep a
# backfill burst from crowding out the snapshot collectors.
MAX_PM_PER_CYCLE = 40
MAX_KALSHI_PER_CYCLE = 40


def _parse_time(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if at.utcoffset() is None:
        return None
    return at.astimezone(UTC)


class ThresholdOutcomeSettler:
    """Resolves expired threshold markets from venue records."""

    def __init__(self, store):
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.execute(OUTCOMES_DDL, ())

    # -- due-market discovery -------------------------------------
    def _due(self, now: datetime) -> dict[str, list[dict]]:
        """Latest snapshot per expired market with no resolved row."""
        resolved = set()
        with self.store.connect() as conn:
            try:
                rows = conn.execute(
                    f"SELECT venue, market_key FROM {OUTCOMES_TABLE}"
                    " WHERE resolved_yes IS NOT NULL", ()).fetchall()
            except Exception:  # noqa: BLE001 - table not created yet
                rows = []
            for row in rows:
                item = dict(row) if isinstance(row, dict) else {
                    "venue": row[0], "market_key": row[1]}
                resolved.add((item["venue"], item["market_key"]))
        due: dict[str, list[dict]] = {"polymarket": [], "kalshi": []}
        seen: set[tuple[str, str]] = set()
        with self.store.connect() as conn:
            # Two load-bearing details:
            # - Column aliases: Postgres rows arrive as dicts keyed by
            #   the SELECT's output names, SQLite rows as tuples read
            #   by position. The normalizer below expects
            #   "key"/"expiry" -- without the aliases the dict shape
            #   has neither and _due dies with KeyError before
            #   recording anything (the production failure this fixes:
            #   the settler ran hourly from 2026-11-24 and recorded
            #   zero outcomes).
            # - Latest snapshot PER MARKET (the snapshot tables' keys
            #   make (market, snapshot_at) unique, so the MAX join
            #   yields one row per market). The previous flat
            #   latest-4000-rows scan went blind to any market whose
            #   last snapshot aged past the window -- at ~1,400 Kalshi
            #   snapshot rows an hour that window is under three
            #   hours, so the markets that expired while the settler
            #   was broken (and any future gap of the same length)
            #   could never be recorded at all.
            pm_rows = conn.execute(
                "SELECT s.condition_id AS key, s.asset, s.contract_type,"
                " s.strike_low, s.strike_high, s.end_date AS expiry"
                " FROM rival_threshold_market_snapshots s"
                " JOIN (SELECT condition_id, MAX(snapshot_at) AS latest"
                " FROM rival_threshold_market_snapshots"
                " GROUP BY condition_id) m"
                " ON m.condition_id = s.condition_id"
                " AND m.latest = s.snapshot_at"
                " WHERE s.end_date IS NOT NULL", ()
            ).fetchall()
            k_rows = conn.execute(
                "SELECT s.market_ticker AS key, s.asset, s.contract_type,"
                " s.strike_low, s.strike_high, s.close_time AS expiry"
                " FROM rival_kalshi_threshold_snapshots s"
                " JOIN (SELECT market_ticker, MAX(snapshot_at) AS latest"
                " FROM rival_kalshi_threshold_snapshots"
                " GROUP BY market_ticker) m"
                " ON m.market_ticker = s.market_ticker"
                " AND m.latest = s.snapshot_at"
                " WHERE s.close_time IS NOT NULL", ()
            ).fetchall()
        for raw, venue, key_idx in ((pm_rows, "polymarket", 0),
                                    (k_rows, "kalshi", 0)):
            for raw_row in raw:
                item = dict(raw_row) if isinstance(raw_row, dict) else {
                    "key": raw_row[0], "asset": raw_row[1],
                    "contract_type": raw_row[2], "strike_low": raw_row[3],
                    "strike_high": raw_row[4], "expiry": raw_row[5]}
                key = (venue, str(item["key"]))
                if key in seen or key in resolved:
                    continue
                seen.add(key)
                expiry = _parse_time(item["expiry"])
                if expiry is None or expiry >= now:
                    continue
                due[venue].append({
                    "market_key": str(item["key"]),
                    "asset": item["asset"],
                    "contract_type": item["contract_type"],
                    "strike_low": item["strike_low"],
                    "strike_high": item["strike_high"],
                    "expiry_at": item["expiry"],
                })
        return due

    # -- venue resolution -----------------------------------------
    async def _resolve_polymarket(self, client: httpx.AsyncClient,
                                  market_key: str) -> tuple[int, float] | None:
        # Gamma's default /markets listing omits closed markets, and a
        # settler only ever asks about closed ones (verified live
        # 2026-11-25: the same condition id returns n=0 without the
        # flag and the settled market with it). Ask for closed markets
        # explicitly; fall back to the plain query if that returns
        # nothing, so an open market's answer shape is unchanged.
        payload = None
        for params in ({"condition_ids": market_key, "closed": "true"},
                       {"condition_ids": market_key}):
            response = await client.get(GAMMA_BASE + "/markets",
                                        params=params)
            response.raise_for_status()
            candidate = response.json()
            if isinstance(candidate, list) and candidate:
                payload = candidate
                break
        if not payload:
            return None
        market = payload[0]
        if not isinstance(market, dict) or not market.get("closed"):
            return None
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            import json
            try:
                prices = json.loads(prices)
            except ValueError:
                return None
        if not isinstance(prices, list) or not prices:
            return None
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            return None
        if yes_price >= 0.999:
            return 1, yes_price
        if yes_price <= 0.001:
            return 0, yes_price
        return None  # not decisive (void / split): never guessed

    async def _resolve_kalshi(self, client: httpx.AsyncClient,
                              market_key: str) -> tuple[int, float] | None:
        response = await client.get(
            KALSHI_BASE + f"/markets/{market_key}")
        response.raise_for_status()
        payload = response.json()
        market = payload.get("market") if isinstance(payload, dict) else None
        if not isinstance(market, dict):
            return None
        result = str(market.get("result") or "").lower()
        if result == "yes":
            return 1, 1.0
        if result == "no":
            return 0, 0.0
        return None

    # -- recording --------------------------------------------------
    def _record(self, venue: str, market: dict,
                resolution: tuple[int, float] | None, *,
                now: datetime) -> None:
        resolved_yes, final_price = resolution if resolution else (None, None)
        with self.store.connect() as conn:
            existing = conn.execute(
                f"SELECT market_key FROM {OUTCOMES_TABLE}"
                " WHERE venue=? AND market_key=?",
                (venue, market["market_key"])).fetchone()
            if existing is not None:
                if resolution is not None:
                    conn.execute(
                        f"UPDATE {OUTCOMES_TABLE} SET resolved_yes=?,"
                        " final_yes_price=?, resolved_at=?"
                        " WHERE venue=? AND market_key=?",
                        (resolved_yes, final_price, now.isoformat(),
                         venue, market["market_key"]))
                return
            conn.execute(
                f"INSERT INTO {OUTCOMES_TABLE} (venue, market_key, asset,"
                " contract_type, strike_low, strike_high, expiry_at,"
                " resolved_yes, final_yes_price, resolved_at, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (venue, market["market_key"], market["asset"],
                 market["contract_type"], market["strike_low"],
                 market["strike_high"], market["expiry_at"],
                 resolved_yes, final_price,
                 now.isoformat() if resolution is not None else None,
                 now.isoformat()))

    async def settle(self, client: httpx.AsyncClient, *,
                     now: datetime) -> dict:
        """One cycle: resolve due markets per venue, failure-isolated."""
        self.initialize()
        due = self._due(now)
        receipt: dict[str, dict] = {}
        for venue, resolver, cap in (
                ("polymarket", self._resolve_polymarket, MAX_PM_PER_CYCLE),
                ("kalshi", self._resolve_kalshi, MAX_KALSHI_PER_CYCLE)):
            checked = resolved = 0
            for market in due[venue][:cap]:
                try:
                    resolution = await resolver(client, market["market_key"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one market's outage is local
                    _LOG.warning(
                        "threshold outcome resolve failed venue=%s "
                        "market=%s error=%s",
                        venue, market["market_key"], type(exc).__name__)
                    continue
                checked += 1
                if resolution is not None:
                    resolved += 1
                await asyncio.to_thread(
                    self._record, venue, market, resolution, now=now)
            receipt[venue] = {"due": len(due[venue]), "checked": checked,
                              "resolved": resolved}
        return receipt


def settled_count(store) -> int:
    """Resolved threshold outcomes on record (0 when the table is
    absent -- true: nothing has been recorded yet)."""
    backend = store.database.backend
    if backend == "sqlite":
        sql = ("SELECT name FROM sqlite_master"
               " WHERE type='table' AND name=?")
    else:
        sql = ("SELECT table_name AS name FROM information_schema.tables"
               " WHERE table_schema = current_schema() AND table_name=?")
    with store.connect() as conn:
        if conn.execute(sql, (OUTCOMES_TABLE,)).fetchone() is None:
            return 0
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {OUTCOMES_TABLE}"
            " WHERE resolved_yes IS NOT NULL", ()).fetchone()
    item = dict(row) if isinstance(row, dict) else {"n": row[0]}
    return int(item["n"])
