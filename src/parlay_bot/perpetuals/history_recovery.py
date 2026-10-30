"""Bounded, restart-safe Gate USDT perpetual history recovery.

One 600-bar request per coin per cycle; never replaces the live collector.
Source history is tagged with its ACTUAL retrieval time. Backfilled bars
are never reclassified as contemporaneous trading predictions.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.history_inventory import six_months_prior
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS as _ALLOWED

_LOG = logging.getLogger(__name__)
_STEP = timedelta(minutes=5)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("aware clock required")
    return value.astimezone(UTC)


def _num(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        raise MarketDataError("bad historical numeric value")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise MarketDataError("bad historical numeric value") from exc
    if not math.isfinite(number) or number < 0 or positive and number == 0:
        raise MarketDataError("bad historical numeric value")
    return number


def contiguous_forward_next(
    cursor: datetime, until: datetime, opened: list[datetime],
) -> datetime:
    """Next forward cursor after a page: only past contiguous bars.

    A page that returns fewer bars than its window must not skip the
    missing bars, or the six-month auditor can never report the stream
    complete: the cursor advances past the contiguous prefix only. A
    leading gap (no bar at the cursor) jumps to the first received bar
    so a genuinely absent region cannot stall the cursor forever; the
    auditor still counts those bars as missing, honestly. Callers must
    handle an empty page before calling this (upstream_empty, no
    advance); an empty stamp list here still returns the window end
    for the callers that already filtered empties.
    """
    stamps = sorted({stamp for stamp in opened if cursor <= stamp < until})
    if not stamps:
        return until
    expected = int((until - cursor) / _STEP)
    prefix = 0
    for stamp in stamps:
        if stamp == cursor + prefix * _STEP:
            prefix += 1
        elif stamp > cursor + prefix * _STEP:
            break
    if prefix >= expected:
        return until
    if prefix > 0:
        return cursor + prefix * _STEP
    return stamps[0]


def contiguous_backward_next(
    cursor: datetime, opened: list[datetime],
) -> datetime:
    """Next backward cursor after a page: only past the top-contiguous run.

    The cursor is an exclusive upper bound. Advance to the oldest bar of
    the contiguous run ending just below the cursor; if the run is empty
    (a gap at the top), jump to the oldest fetched bar so a genuine gap
    cannot stall the cursor. Skipped bars stay missing in the audit.
    """
    stamps = sorted({stamp for stamp in opened if stamp < cursor}, reverse=True)
    if not stamps:
        return cursor
    run_start: datetime | None = None
    expect = cursor - _STEP
    for stamp in stamps:
        if stamp == expect:
            run_start = stamp
            expect -= _STEP
        elif stamp < expect:
            break
    return run_start if run_start is not None else stamps[-1]


class GateHistoryRecovery:
    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 600,
    ) -> None:
        if (
            not assets or len(set(assets)) != len(assets)
            or any(x not in _ALLOWED for x in assets)
            or not 12 <= page_bars <= 600
        ):
            raise ValueError("invalid Gate history recovery configuration")
        self.market = market
        self.store = market.store
        self.assets = assets
        self.page_bars = page_bars
        self._client = client or httpx.AsyncClient(
            timeout=20, follow_redirects=False,
        )
        self._owned = client is None
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        self.market.initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_history_recovery (
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    started_from TEXT NOT NULL,
                    next_opened_at TEXT NOT NULL,
                    attempted_at TEXT,
                    inserted_bars INTEGER NOT NULL DEFAULT 0,
                    empty_windows INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_after TEXT,
                    PRIMARY KEY(venue,asset)
                );
                """
            )

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(
                f"https://api.gateio.ws/api/v4/futures/usdt/{path}",
                params=params, follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("Gate historical read unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            existing = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='gate' AND asset=?""",
                (asset,),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                if (
                    row.get("status") == "caught_up"
                    and not row.get("inserted_bars")
                    and (row.get("empty_windows") or 0) > 0
                ):
                    # Vacuous backfill: the old advance-on-empty logic
                    # marched this cursor to caught_up through empty
                    # windows without ever inserting a bar, so the audit
                    # can never fill. Reset to the original seed; empty
                    # pages no longer advance, so a reset row cannot
                    # re-march to a vacuous caught_up, and legitimately
                    # caught-up rows (empty_windows == 0) are untouched.
                    conn.execute(
                        """UPDATE perp_history_recovery SET
                           next_opened_at=started_from, status='progress',
                           retry_after=NULL, empty_windows=0
                           WHERE venue='gate' AND asset=?""",
                        (asset,),
                    )
                    _LOG.warning(
                        "RivaL Gate vacuous backfill reset asset=%s seed=%s",
                        asset, row.get("started_from"),
                    )
                    row = dict(conn.execute(
                        """SELECT * FROM perp_history_recovery
                           WHERE venue='gate' AND asset=?""",
                        (asset,),
                    ).fetchone())
                return row
            initial = six_months_prior(now).replace(
                minute=six_months_prior(now).minute // 5 * 5,
                second=0, microsecond=0,
            ).isoformat()
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at)
                   VALUES ('gate',?,?,?)
                   ON CONFLICT(venue,asset) DO NOTHING""",
                (asset, initial, initial),
            )
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='gate' AND asset=?""",
                (asset,),
            ).fetchone()
        return dict(row)

    def _advance(
        self, asset: str, *, now: datetime, next_open: datetime | None,
        inserted: int = 0, empty: bool = False,
        status: str = "progress", retry_after: datetime | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE perp_history_recovery SET
                   next_opened_at=CASE
                     WHEN ? > next_opened_at THEN ?
                     ELSE next_opened_at END,
                   attempted_at=?,inserted_bars=inserted_bars+?,
                   empty_windows=empty_windows+?,status=?,retry_after=?
                   WHERE venue='gate' AND asset=?""",
                (next_open.isoformat() if next_open else None,
                 next_open.isoformat() if next_open else None,
                 now.isoformat(), inserted, int(empty), status,
                 retry_after.isoformat() if retry_after else None, asset),
            )

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("invalid Gate recovery asset")
        clock = _utc(now or datetime.now(UTC))
        position = await asyncio.to_thread(self._position, asset, clock)
        if position["retry_after"] and _utc(datetime.fromisoformat(
            str(position["retry_after"])
        )) > clock:
            return 0
        cursor = _utc(datetime.fromisoformat(str(position["next_opened_at"])))
        end = clock.replace(minute=clock.minute // 5 * 5,
                            second=0, microsecond=0) - timedelta(hours=4)
        if cursor >= end:
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None, status="caught_up",
            )
            return 0
        until = min(cursor + self.page_bars * _STEP, end)
        symbol = f"{asset}_USDT"
        try:
            contract = await self._get(f"contracts/{symbol}", {})
            if (
                not isinstance(contract, dict) or contract.get("name") != symbol
                or contract.get("type") != "direct"
                or contract.get("status") != "trading"
                or contract.get("in_delisting", False)
            ):
                raise MarketDataError("inactive Gate direct perpetual")
            multiplier = _num(contract.get("quanto_multiplier"), positive=True)
            rows = await self._get(
                "candlesticks", {
                    "contract": symbol, "interval": "5m",
                    "from": int(cursor.timestamp()),
                    "to": int((until - _STEP).timestamp()),
                },
            )
            if not isinstance(rows, list) or len(rows) > self.page_bars:
                raise MarketDataError("oversized or invalid Gate historical page")
            candles = []
            seen: set[int] = set()
            observed = datetime.now(UTC)
            for row in rows:
                if not isinstance(row, dict):
                    raise MarketDataError("invalid historical Gate bar")
                raw = row.get("t")
                if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                    raise MarketDataError("invalid historical Gate timestamp")
                try:
                    stamp = int(raw)
                except ValueError as exc:
                    raise MarketDataError("invalid historical Gate timestamp") from exc
                opened = datetime.fromtimestamp(stamp, UTC)
                if (
                    stamp in seen or not cursor <= opened < until
                    or stamp % 300
                ):
                    raise MarketDataError("unaligned or out-of-window Gate candle")
                seen.add(stamp)
                op, high, low, close = (
                    _num(row.get(key), positive=True)
                    for key in ("o", "h", "l", "c")
                )
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("invalid historical Gate OHLC")
                candles.append(MarketCandle(
                    "gate", symbol, "USDT", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(row.get("v")) * multiplier, -1,
                ))
            if not rows:
                # Never advance past an empty window: an upstream page
                # with no bars is an API gap, not coverage. Retrying
                # later preserves the cursor (mirrors OKX).
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL Gate historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            added = 0
            if candles:
                batch = MarketBatch((), tuple(candles), 5, observed)
                result = await asyncio.to_thread(self.market.ingest, batch)
                added = result.inserted_candles
            next_open = contiguous_forward_next(
                cursor, until, [candle.opened_at for candle in candles],
            )
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=next_open,
                inserted=added, empty=not bool(rows),
                status="progress",
            )
            _LOG.info(
                "RivaL Gate historical recovery asset=%s received=%d "
                "inserted=%d through=%s",
                asset, len(rows), added, next_open.date(),
            )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = (
                "blocked" if isinstance(exc, MarketDataError)
                and isinstance(exc.__cause__, httpx.HTTPStatusError)
                and exc.__cause__.response.status_code in (401, 403, 451)
                else "retry"
            )
            pause = timedelta(hours=6) if reason == "blocked" else timedelta(minutes=20)
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status=reason, retry_after=clock + pause,
            )
            _LOG.warning("RivaL Gate historical recovery failed asset=%s reason=%s "
                         "error=%s",
                         asset, reason, type(exc).__name__)
            return 0

    async def run_once(self, *, now: datetime | None = None) -> int:
        async with self._lock:
            clock = _utc(now or datetime.now(UTC))
            total = 0
            for asset in self.assets:
                total += await self.fetch_asset(asset, now=clock)
            return total

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,asset,started_from,next_opened_at,attempted_at,
                          inserted_bars,empty_windows,status,retry_after
                   FROM perp_history_recovery ORDER BY asset""",
            ).fetchall()
        return [dict(x) for x in rows]
