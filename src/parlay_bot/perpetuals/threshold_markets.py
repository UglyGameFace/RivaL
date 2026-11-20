"""Polymarket threshold/barrier market snapshots for the N1 forward collection.

The market side of the options-implied pre-test (design + forward
amendment, 2026-11-19): the BTC/ETH Above, Range, Reach and Dip series
at daily/weekly/monthly horizons, recorded hourly from Gamma so the
forward sample carries, for every contract-hour, the market's own
price, its terms as Gamma states them, and -- critically -- the fee
schedule in force at that hour. Grading under the amendment reads the
fee from the recorded snapshot, never a flat constant: these series
were fee-free in the pre-fee era, carried the 0.07 crypto coefficient
into November 2026, and take the 0.01 coefficient from 2026-11-20
23:59 UTC, and Gamma displays the schedule per market.

Discovery is by series, verified against the live Gamma API on
2026-11-20 (ids and slugs in ``SERIES``): each in-scope series is
queried for its open events, and every market in those events is
classified from its own question -- Above from the multi-strikes
series, brackets from the neg-risk (Range) series, and Reach vs Dip
from the hit series' question verb ("reach" / "dip to"). Terms are
persisted verbatim (question, group item title) alongside the parsed
strike bounds; a market whose terms cannot be parsed is skipped and
counted, never guessed. Gamma carries no per-market source clock, so
the receipt time is the snapshot time -- the same convention the
convergence collector uses for Kalshi books.

Collection only: no wedge is computed here, no model reads this table,
no order is placed. The single future consumer is the read-only N1
evaluator, run once when the amendment's frozen floors are met. The
Kalshi exploratory arm has NO recorder by design: step 0 verified
Kalshi's own free historical candlesticks API serves its analogous
series, so that arm pulls from the venue at evaluation time.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
ASSETS = ("BTC", "ETH")


@dataclass(frozen=True)
class SeriesSpec:
    """One in-scope Gamma series, as verified live on 2026-11-20."""

    asset: str
    family: str  # "above" | "range" | "hit"
    series_id: int
    series_slug: str
    horizon: str  # the series' own recurrence: daily | weekly | monthly


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("BTC", "above", 45, "btc-multi-strikes-weekly", "daily"),
    SeriesSpec("ETH", "above", 42, "ethereum-multi-strikes-weekly", "daily"),
    SeriesSpec("BTC", "range", 10041, "bitcoin-neg-risk-weekly", "daily"),
    SeriesSpec("ETH", "range", 10065, "ethereum-neg-risk-weekly", "daily"),
    SeriesSpec("BTC", "hit", 10200, "bitcoin-hit-price-daily", "daily"),
    SeriesSpec("ETH", "hit", 11297, "ethereum-hit-price-daily", "daily"),
    SeriesSpec("BTC", "hit", 10151, "bitcoin-hit-price-weekly", "weekly"),
    SeriesSpec("ETH", "hit", 10152, "ethereum-hit-price-weekly", "weekly"),
    SeriesSpec("BTC", "hit", 10016, "bitcoin-hit-price-monthly", "monthly"),
    SeriesSpec("ETH", "hit", 10017, "ethereum-hit-price-monthly", "monthly"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_threshold_market_snapshots (
    snapshot_at TEXT NOT NULL,
    asset TEXT NOT NULL, contract_type TEXT NOT NULL, horizon TEXT NOT NULL,
    series_id INTEGER NOT NULL, series_slug TEXT NOT NULL,
    event_id TEXT, event_slug TEXT,
    market_id TEXT, condition_id TEXT NOT NULL, market_slug TEXT,
    question TEXT, group_item_title TEXT,
    strike_low DOUBLE PRECISION, strike_high DOUBLE PRECISION,
    end_date TEXT,
    yes_price DOUBLE PRECISION, best_bid DOUBLE PRECISION,
    best_ask DOUBLE PRECISION,
    fee_rate DOUBLE PRECISION, fee_schedule_json TEXT,
    PRIMARY KEY (condition_id, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_threshold_market_series
    ON rival_threshold_market_snapshots (asset, contract_type, end_date);
CREATE INDEX IF NOT EXISTS idx_threshold_market_snapshot
    ON rival_threshold_market_snapshots (snapshot_at);
"""

_MONEY = r"\$?([0-9][0-9,]*\.?[0-9]*)"
_ABOVE_QUESTION = re.compile(r"above " + _MONEY, re.IGNORECASE)
_HIT_QUESTION = re.compile(r"(reach|dip to) " + _MONEY, re.IGNORECASE)
_RANGE_QUESTION = re.compile(
    r"between " + _MONEY + r" and " + _MONEY, re.IGNORECASE)
_LESS_QUESTION = re.compile(r"less than " + _MONEY, re.IGNORECASE)


def _num(value) -> float | None:
    """A finite numeric field, or None (bools and strings are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    level = float(value)
    return level if math.isfinite(level) else None


def _price(value) -> float | None:
    """A 0..1 probability price, or None."""
    level = _num(value)
    return level if level is not None and 0.0 <= level <= 1.0 else None


def _money(text) -> float | None:
    """A money figure from a Gamma label like ``$72,500`` / ``52,000``."""
    if not isinstance(text, str):
        return None
    try:
        level = float(text.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return level if math.isfinite(level) and level > 0 else None


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("threshold snapshot clock requires timezone")
    return value.astimezone(UTC).isoformat()


def _event_horizon(spec: SeriesSpec, event: dict) -> str:
    """The event's horizon: the series recurrence, except annual specials.

    The monthly hit series also carries the annual event (``...-before-
    2027``, ``...-in-2026``); those slugs are classified annual so the
    evaluator's daily/weekly/monthly sample never silently absorbs them.
    """
    slug = str(event.get("slug") or "")
    if "before-" in slug or re.search(r"-in-\d{4}$", slug):
        return "annual"
    return spec.horizon


def _terms(spec: SeriesSpec, market: dict) -> tuple[str, float | None, float | None] | None:
    """(contract_type, strike_low, strike_high) for one market, or None.

    Bounds read as the interval the settlement price is compared
    against: Above and Reach carry their level as the low bound, Dip as
    the high bound, Range as both (an open bracket leaves one side
    NULL). The verbatim labels are persisted alongside by the caller;
    these parsed bounds are a convenience, never a replacement.
    """
    question = str(market.get("question") or "")
    title = str(market.get("groupItemTitle") or "")
    if spec.family == "above":
        level = _money(title)
        if level is None:
            match = _ABOVE_QUESTION.search(question)
            level = _money(match.group(1)) if match else None
        return ("above", level, None) if level is not None else None
    if spec.family == "range":
        low = high = None
        text = title.strip()
        if text.startswith("<"):
            high = _money(text[1:])
        elif text.startswith(">"):
            low = _money(text[1:])
        elif "-" in text:
            left, _, right = text.partition("-")
            low, high = _money(left), _money(right)
        if low is None and high is None:
            match = _RANGE_QUESTION.search(question)
            if match:
                low, high = _money(match.group(1)), _money(match.group(2))
            else:
                match = _LESS_QUESTION.search(question)
                if match:
                    high = _money(match.group(1))
        if low is None and high is None:
            return None
        return ("range", low, high)
    # Hit family: the question's verb is the direction.
    lowered = question.lower()
    if "reach" in lowered:
        kind = "reach"
    elif "dip" in lowered:
        kind = "dip"
    else:
        return None
    level = _money(title)
    if level is None:
        match = _HIT_QUESTION.search(question)
        level = _money(match.group(2)) if match else None
    if level is None:
        return None
    return (kind, level, None) if kind == "reach" else (kind, None, level)


def _yes_price(market: dict) -> float | None:
    """The first outcome's price from outcomePrices (list or JSON text)."""
    raw = market.get("outcomePrices")
    prices = raw
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(prices, list) or not prices:
        return None
    first = prices[0]
    if isinstance(first, str):
        try:
            first = float(first)
        except ValueError:
            return None
    return _price(first)


def _fee(market: dict) -> tuple[float | None, str | None]:
    """(fee_rate, schedule JSON) exactly as the market states them.

    feesEnabled false is a positive statement -- the rate is 0.0. A
    missing schedule is unknown and stays NULL; the two are never
    conflated, because grading reads this rate per observation date.
    """
    schedule = market.get("feeSchedule")
    schedule_json = None
    if isinstance(schedule, dict):
        schedule_json = json.dumps(schedule, sort_keys=True,
                                   separators=(",", ":"))
    if market.get("feesEnabled") is False:
        return 0.0, schedule_json
    if isinstance(schedule, dict):
        return _num(schedule.get("rate")), schedule_json
    return None, None


def market_row(spec: SeriesSpec, event: dict, market: dict) -> dict | None:
    """One snapshot row from a Gamma market, or None when it is not evidence.

    A row needs a condition id (its identity) and parseable terms (its
    contract). Prices and fees persist as stated, NULL when absent.
    """
    if not isinstance(market, dict):
        return None
    condition_id = market.get("conditionId")
    if not isinstance(condition_id, str) or not condition_id:
        return None
    terms = _terms(spec, market)
    if terms is None:
        return None
    contract_type, strike_low, strike_high = terms
    fee_rate, fee_schedule_json = _fee(market)
    end_date = market.get("endDate") or event.get("endDate")
    return {
        "asset": spec.asset,
        "contract_type": contract_type,
        "horizon": _event_horizon(spec, event),
        "series_id": spec.series_id,
        "series_slug": spec.series_slug,
        "event_id": str(event.get("id")) if event.get("id") is not None else None,
        "event_slug": event.get("slug"),
        "market_id": str(market.get("id")) if market.get("id") is not None else None,
        "condition_id": condition_id,
        "market_slug": market.get("slug"),
        "question": market.get("question"),
        "group_item_title": market.get("groupItemTitle"),
        "strike_low": strike_low,
        "strike_high": strike_high,
        "end_date": end_date if isinstance(end_date, str) else None,
        "yes_price": _yes_price(market),
        "best_bid": _price(market.get("bestBid")),
        "best_ask": _price(market.get("bestAsk")),
        "fee_rate": fee_rate,
        "fee_schedule_json": fee_schedule_json,
    }


class ThresholdMarketsRecorder:
    """Idempotent storage + Gamma capture for threshold market snapshots.

    ``capture`` is the bot loop's entry point: each series is fetched
    and stored independently, so a Gamma failure for one series degrades
    that family for the hour and nothing else. The receipt counts rows
    per series; it carries no payload data.
    """

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.store.connect() as conn:
                conn.executescript(_SCHEMA)
            self._ready = True

    def record_event(self, spec: SeriesSpec, event: dict, *,
                     snapshot_at: datetime) -> tuple[int, int]:
        """Persist one event's markets; returns (written, skipped)."""
        self.initialize()
        if not isinstance(event, dict):
            return 0, 0
        markets = event.get("markets")
        if not isinstance(markets, list):
            return 0, 0
        stamp = _iso(snapshot_at)
        written = 0
        skipped = 0
        with self._lock, self.store.connect() as conn:
            for market in markets:
                row = market_row(spec, event, market)
                if row is None:
                    skipped += 1
                    continue
                cursor = conn.execute(
                    """INSERT INTO rival_threshold_market_snapshots
                       (snapshot_at, asset, contract_type, horizon,
                        series_id, series_slug, event_id, event_slug,
                        market_id, condition_id, market_slug, question,
                        group_item_title, strike_low, strike_high, end_date,
                        yes_price, best_bid, best_ask, fee_rate,
                        fee_schedule_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (stamp, row["asset"], row["contract_type"], row["horizon"],
                     row["series_id"], row["series_slug"], row["event_id"],
                     row["event_slug"], row["market_id"], row["condition_id"],
                     row["market_slug"], row["question"],
                     row["group_item_title"], row["strike_low"],
                     row["strike_high"], row["end_date"], row["yes_price"],
                     row["best_bid"], row["best_ask"], row["fee_rate"],
                     row["fee_schedule_json"]),
                )
                written += cursor.rowcount
        return written, skipped

    async def _fetch_series(self, client: httpx.AsyncClient,
                            spec: SeriesSpec) -> list[dict]:
        """Open events for one series, paged to a bounded depth."""
        events: list[dict] = []
        for page in range(5):
            response = await client.get(
                GAMMA_BASE + "/events",
                params={"series_id": spec.series_id, "closed": "false",
                        "limit": 100, "offset": page * 100})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError("Gamma events payload is not a list")
            events.extend(e for e in payload if isinstance(e, dict))
            if len(payload) < 100:
                break
        return events

    async def capture_series(self, client: httpx.AsyncClient,
                             spec: SeriesSpec, *, now: datetime) -> dict:
        """Fetch + store one series; never raises for venue faults."""
        try:
            events = await self._fetch_series(client, spec)
            written = 0
            skipped = 0
            for event in events:
                rows, missed = await asyncio.to_thread(
                    self.record_event, spec, event, snapshot_at=now)
                written += rows
                skipped += missed
            return {"series": spec.series_slug, "events": len(events),
                    "rows": written, "skipped": skipped}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one series' outage is local
            return {"series": spec.series_slug, "error": type(exc).__name__}

    async def capture(self, client: httpx.AsyncClient, *,
                      now: datetime) -> dict:
        """One hourly cycle over every in-scope series, failure-isolated."""
        receipt = {}
        for spec in SERIES:
            result = await self.capture_series(client, spec, now=now)
            receipt[spec.series_slug] = result
        return receipt

    def snapshots(self, *, asset: str | None = None,
                  contract_type: str | None = None) -> list[dict]:
        """Stored rows, optionally filtered, in snapshot order."""
        self.initialize()
        query = "SELECT * FROM rival_threshold_market_snapshots"
        clauses = []
        params: list = []
        if asset is not None:
            clauses.append("asset=?")
            params.append(asset)
        if contract_type is not None:
            clauses.append("contract_type=?")
            params.append(contract_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY snapshot_at, condition_id"
        with self.store.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]
