"""Bounded, restart-safe KuCoin USDTM 5m historical futures recovery.

One window <=200 bars/coin/cycle from KuCoin's public UTA V2 market API.
A recovered old bar records *today's first observation*, never a fake live
prediction from months ago. Separate venue cursor shares Gate's audited table.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.history_inventory import six_complete_months_start
from parlay_bot.perpetuals.history_recovery import (
    _ALLOWED,
    _STEP,
    GateHistoryRecovery,
    _num,
    _utc,
    align_forward_month_cursor,
    contiguous_forward_next,
)
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)


class KucoinHistoryRecovery(GateHistoryRecovery):
    """KuCoin UTA public trade klines, isolated from live KuCoin polling."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 200,
    ) -> None:
        if not 12 <= page_bars <= 200:
            raise ValueError("KuCoin Futures history page cannot exceed 200 bars")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        # A short metadata cache saves repeat contract-lookups; never cache
        # candles or successful source statuses here.
        self._metadata: dict[str, tuple[float, datetime]] = {}

    async def _public(self, url: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(
                url, params=params, follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("KuCoin public futures history unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()  # Same durable schema used by Gate, different venue key.
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='kucoin' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                start = six_complete_months_start(now).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('kucoin',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='kucoin' AND asset=?""",
                    (asset,),
                ).fetchone()
            assert row is not None
            return align_forward_month_cursor(
                conn, venue="kucoin", asset=asset, now=now, row=dict(row),
            )

    def _advance(
        self, asset: str, *, now: datetime, next_open: datetime | None,
        inserted: int = 0, empty: bool = False,
        status: str = "progress", retry_after: datetime | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE perp_history_recovery SET
                   next_opened_at=CASE WHEN ? > next_opened_at THEN ?
                                       ELSE next_opened_at END,
                   attempted_at=?,inserted_bars=inserted_bars+?,
                   empty_windows=empty_windows+?,status=?,retry_after=?
                   WHERE venue='kucoin' AND asset=?""",
                (next_open.isoformat() if next_open else None,
                 next_open.isoformat() if next_open else None,
                 now.isoformat(), inserted, int(empty), status,
                 retry_after.isoformat() if retry_after else None, asset),
            )

    def _pending_gaps(self, asset: str, *, now: datetime) -> int:
        floor = six_complete_months_start(now).isoformat()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM perp_history_gap_repair
                   WHERE venue='kucoin' AND asset=? AND opened_at>=?""",
                (asset, floor),
            ).fetchone()
        return int(row["n"])

    def _claim_due_gap(
        self, asset: str, *, now: datetime, end: datetime,
    ) -> datetime | None:
        """Atomically claim one audit-proven KuCoin gap with six-hour backoff."""
        floor = six_complete_months_start(now).isoformat()
        with self.store.connect() as conn:
            row = conn.execute(
                """UPDATE perp_history_gap_repair SET
                     attempted_at=?, retry_after=?
                   WHERE venue='kucoin' AND asset=?
                     AND opened_at=(
                       SELECT opened_at FROM perp_history_gap_repair
                       WHERE venue='kucoin' AND asset=? AND opened_at>=?
                         AND opened_at<? AND retry_after<=?
                       ORDER BY opened_at LIMIT 1
                     ) AND retry_after<=?
                   RETURNING opened_at""",
                (now.isoformat(), (now + timedelta(hours=6)).isoformat(),
                 asset, asset, floor, end.isoformat(), now.isoformat(),
                 now.isoformat()),
            ).fetchone()
        return _utc(datetime.fromisoformat(str(row["opened_at"]))) if row else None

    def _complete_gap(self, asset: str, opened: datetime) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """DELETE FROM perp_history_gap_repair
                   WHERE venue='kucoin' AND asset=? AND opened_at=?""",
                (asset, opened.isoformat()),
            )

    def _queue_due_path_gaps(self, asset: str, *, now: datetime, end: datetime) -> int:
        """Queue only provable isolated KuCoin holes blocking immutable paper paths.

        The six-month audit intentionally refuses to queue a massive missing
        historical prefix. That can leave a single, source-identifiable 5m
        hole inside a due forward path unrepairable while the older bulk
        cursor remains active. Sample a bounded 14-day oldest/newest set.
        Only a missing slot strictly bracketed by authentic stored neighbors
        may join the existing six-hour-backoff gap queue. A source request
        still occurs only through the existing one-page fetch_asset budget.
        """
        floor = max(six_complete_months_start(now), now - timedelta(days=14))
        base = "XBT" if asset == "BTC" else asset
        symbol = f"{base}USDTM"
        with self.store.connect() as conn:
            if self.store.database.backend == "postgres":
                exists = conn.execute(
                    "SELECT to_regclass(?) AS name", ("rival_forward_paths",),
                ).fetchone()["name"]
            else:
                present = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    ("rival_forward_paths",),
                ).fetchone()
                exists = present["name"] if present else None
            if not exists:
                return 0

            outstanding = conn.execute(
                """SELECT COUNT(*) AS n FROM perp_history_gap_repair
                   WHERE venue='kucoin' AND asset=? AND opened_at>=?""",
                (asset, floor.isoformat()),
            ).fetchone()
            budget = min(4, max(0, 16 - int(outstanding["n"])))
            if not budget:
                return 0
            candidates = []
            for order in ("ASC", "DESC"):
                candidates.extend(conn.execute(
                    f"""SELECT reference_opened_at,issued_at,target_opened_at,
                               horizon_minutes
                        FROM rival_forward_paths
                        WHERE venue='kucoin' AND contract=? AND quote='USDT'
                          AND asset=? AND state='pending' AND side='long'
                          AND horizon_minutes IN (15,60)
                          AND target_opened_at>=? AND target_opened_at<?
                        ORDER BY target_opened_at {order} LIMIT 12""",
                    (symbol, asset, floor.isoformat(), end.isoformat()),
                ).fetchall())

            processed = set()
            queued = 0
            for path in candidates:
                key = (path["reference_opened_at"], path["target_opened_at"],
                       path["horizon_minutes"])
                if key in processed:
                    continue
                processed.add(key)
                try:
                    reference = _utc(datetime.fromisoformat(
                        str(path["reference_opened_at"])))
                    issued = _utc(datetime.fromisoformat(str(path["issued_at"])))
                    target = _utc(datetime.fromisoformat(
                        str(path["target_opened_at"])))
                    horizon = int(path["horizon_minutes"])
                    steps = horizon // 5
                    if not (
                        horizon in (15, 60)
                        and issued < reference + timedelta(minutes=10)
                        and issued <= now
                        and target == reference + (steps + 1) * _STEP
                    ):
                        continue
                    slots = tuple(
                        reference + (index + 2) * _STEP
                        for index in range(steps)
                    )
                    found = conn.execute(
                        """SELECT opened_at,closed_at,first_observed_at
                           FROM perp_market_candles
                           WHERE venue='kucoin' AND contract=? AND quote='USDT'
                             AND interval_minutes=5
                             AND opened_at>=? AND opened_at<=?
                           ORDER BY opened_at LIMIT 15""",
                        (symbol, (slots[0] - _STEP).isoformat(),
                         (slots[-1] + _STEP).isoformat()),
                    ).fetchall()
                    valid = set()
                    for bar in found:
                        opened = _utc(datetime.fromisoformat(str(bar["opened_at"])))
                        closed = _utc(datetime.fromisoformat(str(bar["closed_at"])))
                        observed = _utc(datetime.fromisoformat(
                            str(bar["first_observed_at"])))
                        if (closed != opened + _STEP - timedelta(milliseconds=1)
                            or not closed <= observed <= now):
                            # Neighbor evidence is malformed, not safe proof
                            # that a provider-original candle is missing.
                            valid.clear()
                            break
                        valid.add(opened)
                    else:
                        missing = [slot for slot in slots if slot not in valid]
                        if len(missing) != 1:
                            continue
                        hole = missing[0]
                        if not (floor <= hole < end
                                and hole - _STEP in valid
                                and hole + _STEP in valid):
                            continue
                        result = conn.execute(
                            """INSERT INTO perp_history_gap_repair
                               (venue,asset,opened_at,retry_after)
                               VALUES ('kucoin',?,?,?)
                               ON CONFLICT(venue,asset,opened_at) DO NOTHING""",
                            (asset, hole.isoformat(), now.isoformat()),
                        )
                        queued += int(result.rowcount == 1)
                        if queued >= budget:
                            break
                except (ValueError, TypeError, OverflowError):
                    # A corrupt forecast clock cannot authorize API recovery.
                    continue
            return queued

    async def _multiplier(self, asset: str, symbol: str, now: datetime) -> float:
        cached = self._metadata.get(symbol)
        if cached and timedelta(0) <= now - cached[1] < timedelta(hours=1):
            return cached[0]
        payload = await self._public(
            f"https://api-futures.kucoin.com/api/v1/contracts/{symbol}", {}
        )
        meta = payload.get("data") if isinstance(payload, dict) else None
        base = "XBT" if asset == "BTC" else asset
        if (
            not isinstance(payload, dict) or payload.get("code") != "200000"
            or not isinstance(meta, dict) or meta.get("symbol") != symbol
            or meta.get("baseCurrency") != base
            or meta.get("quoteCurrency") != "USDT"
            or meta.get("settleCurrency") != "USDT"
            or meta.get("isInverse") is not False
            or meta.get("status") != "Open"
            or meta.get("expireDate") is not None
        ):
            raise MarketDataError("KuCoin active linear perpetual unavailable")
        factor = _num(meta.get("multiplier"), positive=True)
        if cached is not None and cached[0] != factor:
            # Metadata may have changed since older history was recorded.
            # Don't silently rewrite quantities with a new scale.
            self._metadata.clear()
            raise MarketDataError("KuCoin contract multiplier changed; audit required")
        self._metadata[symbol] = (factor, now)
        return factor

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured KuCoin recovery asset")
        clock = _utc(now or datetime.now(UTC))
        position = await asyncio.to_thread(self._position, asset, clock)
        bulk_retry = (
            _utc(datetime.fromisoformat(str(position["retry_after"])))
            if position["retry_after"] else None
        )
        bulk_paused = bulk_retry is not None and bulk_retry > clock
        # A missing bulk-history page is independent of an EXACT, already
        # witnessed path gap. Let a due gap use its own durable retry clock,
        # but never bypass provider-wide error/rate-limit cooldowns.
        if bulk_paused and position["status"] != "upstream_empty":
            return 0
        cursor = _utc(datetime.fromisoformat(str(position["next_opened_at"])))
        end = clock.replace(
            minute=clock.minute // 5 * 5, second=0, microsecond=0,
        ) - timedelta(hours=4)
        # Queue source-authentic singleton holes from a small sample of
        # immutable, already-due paper paths even while the six-month
        # historical cursor is still filling older months. One provider
        # request per cycle remains the hard limit.
        await asyncio.to_thread(
            self._queue_due_path_gaps, asset, now=clock, end=end,
        )
        gap = await asyncio.to_thread(
            self._claim_due_gap, asset, now=clock, end=end,
        )
        repairing = gap is not None
        if repairing:
            cursor = gap
        elif bulk_paused:
            # The bulk page is still cooling down, and there is no eligible
            # independently backed-off source gap to fetch this cycle.
            return 0
        elif cursor >= end:
            pending = await asyncio.to_thread(self._pending_gaps, asset, now=clock)
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status="gaps_pending" if pending else "caught_up",
            )
            return 0
        # A repair request covers ONLY the exact missing five-minute slot.
        # A provider returning a neighbor cannot be counted as recovery.
        until = min(cursor + (1 if repairing else self.page_bars) * _STEP, end)
        base = "XBT" if asset == "BTC" else asset
        symbol = f"{base}USDTM"
        try:
            factor = await self._multiplier(asset, symbol, clock)
            data = await self._public(
                "https://api.kucoin.com/api/ua/v2/market/kline",
                {
                    "tradeType": "FUTURES", "symbol": symbol,
                    "klineType": "TRADE", "interval": "5min",
                    "startAt": int(cursor.timestamp()),
                    "endAt": int((until - _STEP).timestamp()),
                },
            )
            result = data.get("data") if isinstance(data, dict) else None
            if (
                not isinstance(data, dict) or data.get("code") != "200000"
                or not isinstance(result, dict)
                or result.get("symbol") != symbol
                or result.get("tradeType") != "FUTURES"
                or not isinstance(result.get("list"), list)
                or len(result["list"]) > self.page_bars
            ):
                raise MarketDataError("invalid KuCoin futures history response")
            candles = []
            seen: set[int] = set()
            observed = datetime.now(UTC)
            for row in result["list"]:
                if not isinstance(row, list) or len(row) < 7:
                    raise MarketDataError("invalid KuCoin historical candle")
                raw = row[0]
                if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                    raise MarketDataError("invalid KuCoin timestamp")
                value = str(raw)
                if not value.isascii() or not value.isdecimal():
                    raise MarketDataError("invalid KuCoin timestamp")
                timestamp = int(value)
                if len(value) == 13:
                    if timestamp % 1000:
                        raise MarketDataError("non-second historical timestamp")
                    timestamp //= 1000
                elif len(value) != 10:
                    raise MarketDataError("unexpected KuCoin timestamp unit")
                opened = datetime.fromtimestamp(timestamp, UTC)
                if (
                    timestamp in seen or not cursor <= opened < until
                    or timestamp % 300
                ):
                    raise MarketDataError("out-of-window or duplicate KuCoin bar")
                seen.add(timestamp)
                op, high, low, close = (
                    _num(v, positive=True) for v in row[1:5]
                )
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("inconsistent KuCoin historical OHLC")
                candles.append(MarketCandle(
                    "kucoin", symbol, "USDT", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(row[5]) * factor, -1,
                ))
            if not candles and repairing:
                # The claimed slot stays queued for another source request
                # after the per-gap backoff, without rewinding forward data.
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    status="upstream_empty" if bulk_paused else "gaps_pending",
                    retry_after=bulk_retry if bulk_paused else None,
                )
                return 0
            if not candles:
                # Never advance past an empty window: an upstream page
                # with no bars is an API gap, not coverage. Retrying
                # later preserves the cursor (mirrors OKX).
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL KuCoin historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            added = 0
            if candles:
                saved = await asyncio.to_thread(
                    self.market.ingest, MarketBatch((), tuple(candles), 5, observed)
                )
                added = saved.inserted_candles
            if repairing:
                # Only accepted source bars clear a persisted hole. Immutable
                # conflicting market candles must remain visible for audit.
                if saved.inserted_candles + saved.unchanged_candles == len(candles):
                    await asyncio.to_thread(self._complete_gap, asset, cursor)
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    inserted=added,
                    status="upstream_empty" if bulk_paused else "gaps_pending",
                    retry_after=bulk_retry if bulk_paused else None,
                )
            else:
                next_open = contiguous_forward_next(
                    cursor, until, [candle.opened_at for candle in candles],
                )
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=next_open,
                    inserted=added, empty=False, status="progress",
                )
            _LOG.info(
                "RivaL KuCoin history asset=%s rows=%d inserted=%d",
                asset, len(candles), added,
            )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one provider never stalls live data
            blocked = (
                isinstance(exc, MarketDataError)
                and isinstance(exc.__cause__, httpx.HTTPStatusError)
                and exc.__cause__.response.status_code in (401, 403, 429, 451)
            )
            reason = "blocked" if blocked else "retry"
            pause = timedelta(hours=6) if blocked else timedelta(minutes=20)
            retry_until = clock + pause
            if bulk_paused and bulk_retry is not None:
                retry_until = max(retry_until, bulk_retry)
            await asyncio.to_thread(
                self._advance, asset, now=clock,
                next_open=None, status=reason, retry_after=retry_until,
            )
            _LOG.warning(
                "RivaL KuCoin history asset=%s unavailable status=%s",
                asset, reason,
            )
            return 0

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='kucoin' ORDER BY asset"""
            ).fetchall()
        return [dict(r) for r in rows]
