"""Low-cost live market momentum changes, distinct from candle-model predictions.

Samples already-open shared Hyperliquid and Binance WebSocket books in RAM.
A trend change requires both sources to show the SAME 60-150 second direction
over a meaningful threshold, two confirming samples, an established prior
opposite trend, and sufficient quote freshness. No model probability is
fabricated from price changes. No polling or orders are created.
"""
from __future__ import annotations

import hashlib
import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ASSETS = ("BTC", "ETH", "SOL", "XRP")
VENUES = ("hyperliquid", "binance")
MIN_BPS = 15.0
MAX_CROSS_VENUE_BPS = 35.0
WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 150
MIN_CONFIRM_SECONDS = 10
MAX_FRESH_SECONDS = 12


@dataclass(frozen=True)
class LiveShiftNotice:
    asset: str
    issued: datetime
    expires: datetime
    direction: str
    move_bps: float
    lookback_seconds: int
    venue: str = "live_trend"
    horizon: int = 5

    @property
    def key(self) -> str:
        raw = f"live-trend|{self.asset}|{self.direction}|{self.issued.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()


class LiveDirectionScanner:
    """Single shared scanner; never emit an initial trend as a reversal."""

    def __init__(self, hub, *, threshold_bps: float = MIN_BPS) -> None:
        if not math.isfinite(threshold_bps) or not 10 <= threshold_bps <= 100:
            raise ValueError("invalid live trend threshold")
        self.hub = hub
        self.threshold_bps = threshold_bps
        self._observations: dict[str, deque[tuple[datetime, float, float]]] = {
            asset: deque(maxlen=240) for asset in ASSETS
        }
        self._confirmed: dict[str, str] = {}
        self._pending: dict[str, tuple[str, datetime]] = {}

    def observe(self, *, now: datetime | None = None) -> list[LiveShiftNotice]:
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("aware timestamp required")
        stamp = stamp.astimezone(UTC)
        emitted = []
        for asset in ASSETS:
            books = [self.hub.snapshot(venue, asset, now=stamp)
                     for venue in VENUES]
            if any(book is None for book in books):
                self._pending.pop(asset, None)
                continue
            a, b = books
            if any(
                book.exchange_at > stamp + timedelta(seconds=2)
                or stamp - book.exchange_at > timedelta(seconds=MAX_FRESH_SECONDS)
                or not all(math.isfinite(x) and x > 0
                           for x in (book.bid, book.ask))
                or book.bid >= book.ask
                or (book.ask - book.bid) / ((book.ask + book.bid) / 2)
                   * 10000 > 25
                for book in (a, b)
            ):
                self._pending.pop(asset, None)
                continue
            mid_a = (a.bid + a.ask) / 2
            mid_b = (b.bid + b.ask) / 2
            cross = abs(mid_a - mid_b) / ((mid_a + mid_b) / 2) * 10000
            if cross > MAX_CROSS_VENUE_BPS:
                self._pending.pop(asset, None)
                continue
            history = self._observations[asset]
            if history and stamp <= history[-1][0]:
                continue
            history.append((stamp, mid_a, mid_b))
            while history and (stamp-history[0][0]).total_seconds() > 180:
                history.popleft()
            anchor = next(
                (row for row in history
                 if WINDOW_SECONDS <= (stamp - row[0]).total_seconds()
                 <= MAX_WINDOW_SECONDS),
                None,
            )
            if anchor is None:
                continue
            elapsed = int((stamp - anchor[0]).total_seconds())
            changes = (
                (mid_a / anchor[1]-1) * 10000,
                (mid_b / anchor[2]-1) * 10000,
            )
            direction = (
                "UP" if all(x >= self.threshold_bps for x in changes)
                else "DOWN" if all(x <= -self.threshold_bps for x in changes)
                else None
            )
            if direction is None:
                self._pending.pop(asset, None)
                continue
            if self._confirmed.get(asset) == direction:
                self._pending.pop(asset, None)
                continue
            pending = self._pending.get(asset)
            if pending is None or pending[0] != direction:
                self._pending[asset] = (direction, stamp)
                continue
            if (stamp - pending[1]).total_seconds() < MIN_CONFIRM_SECONDS:
                continue
            previous = self._confirmed.get(asset)
            self._confirmed[asset] = direction
            self._pending.pop(asset, None)
            if previous is None:
                continue  # baseline established; NOT an alert
            emitted.append(LiveShiftNotice(
                asset=asset, issued=stamp, expires=stamp+timedelta(minutes=3),
                direction=direction, move_bps=sum(changes)/2,
                lookback_seconds=elapsed,
            ))
        return emitted
