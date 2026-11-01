"""Conservative OKX archived L2 replay.

A snapshot can provide a point-in-time quote. Incremental deltas are only
replayed when the prior snapshot and *every* sequence link are available.
No synthetic liquidity or provider checksum is inferred.
"""
from __future__ import annotations

import math

from parlay_bot.research.free_history import HistoryImportError


class ArchivedBook:
    def __init__(self, max_levels: int = 5000) -> None:
        if max_levels not in (400, 5000):
            raise ValueError("unsupported OKX L2 depth")
        self.max_levels = max_levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.seq: int | None = None
        self.last_ts: int | None = None
        self.valid = False
        self.gaps = 0
        self.unlinked = 0
        self.applied_updates = 0

    @staticmethod
    def _levels(values: object, *, snapshot: bool) -> dict[float, float]:
        if not isinstance(values, list):
            raise HistoryImportError("invalid L2 price levels")
        if len(values) > 5000:
            raise HistoryImportError("oversized archived book depth")
        parsed = {}
        for level in values:
            if not isinstance(level, list) or len(level) < 2:
                raise HistoryImportError("malformed archived L2 level")
            try:
                px, sz = float(level[0]), float(level[1])
            except (TypeError, ValueError, OverflowError) as exc:
                raise HistoryImportError("invalid L2 price or size") from exc
            if (not math.isfinite(px) or not math.isfinite(sz)
                or px <= 0 or sz < 0 or (snapshot and sz == 0) or px in parsed):
                raise HistoryImportError("invalid/duplicate L2 price level")
            parsed[px] = sz
        return parsed

    def _invalidate(self) -> None:
        self.valid = False
        self.bids.clear()
        self.asks.clear()
        self.seq = None

    @staticmethod
    def _seq(event: dict, key: str) -> int | None:
        value = event.get(key)
        if value is None:
            return None
        # Strictly integer sequence identifiers, not booleans or floats.
        if isinstance(value, bool):
            raise HistoryImportError("invalid OKX L2 sequence")
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        if isinstance(value, int) and value >= 0:
            return value
        raise HistoryImportError("invalid OKX L2 sequence")

    def _metrics(self) -> dict:
        if not self.bids or not self.asks:
            raise HistoryImportError("empty book after L2 replay")
        best_bid, best_ask = max(self.bids), min(self.asks)
        if best_bid >= best_ask:
            raise HistoryImportError("crossed L2 book after replay")
        top_bids = sorted(self.bids.items(), reverse=True)[:5]
        top_asks = sorted(self.asks.items())[:5]
        b = sum(size for _, size in top_bids)
        a = sum(size for _, size in top_asks)
        if b <= 0 or a <= 0:
            raise HistoryImportError("zero depth in archived L2")
        return {
            "last_snapshot_spread_bps": 20_000 * (best_ask - best_bid) / (best_ask + best_bid),
            "l2_top5_imbalance": (b - a) / (b + a),
        }

    def apply(self, event: dict, ts: int) -> dict | None:
        """Return reconstructed book metrics only when evidence is continuous."""
        if not isinstance(event, dict) or event.get("action") not in ("snapshot", "update"):
            raise HistoryImportError("unrecognized OKX L2 action")
        if self.last_ts is not None and ts < self.last_ts:
            self._invalidate()
            raise HistoryImportError("nonmonotonic archived order book events")
        self.last_ts = ts
        action = event["action"]
        seq = self._seq(event, "seqId")
        prev = self._seq(event, "prevSeqId")
        if action == "snapshot":
            bids = self._levels(event.get("bids"), snapshot=True)
            asks = self._levels(event.get("asks"), snapshot=True)
            if not bids or not asks:
                raise HistoryImportError("incomplete archived snapshot")
            self.bids, self.asks = bids, asks
            self.valid = True
            self.seq = seq
            try:
                metrics = self._metrics()
            except HistoryImportError:
                self._invalidate()
                raise
            return {**metrics, "snapshot": True, "sequence_verified": False}
        if not self.valid:
            self.unlinked += 1
            return None
        if seq is None or prev is None or self.seq is None:
            self.unlinked += 1
            self._invalidate()
            return None
        if prev != self.seq or seq <= prev:
            self.gaps += 1
            self._invalidate()
            return None
        bids = self._levels(event.get("bids", []), snapshot=False)
        asks = self._levels(event.get("asks", []), snapshot=False)
        for book, changes in ((self.bids, bids), (self.asks, asks)):
            for px, sz in changes.items():
                if sz == 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
        self.seq = seq
        if len(self.bids) > self.max_levels or len(self.asks) > self.max_levels:
            self._invalidate()
            raise HistoryImportError("L2 replay exceeds advertised depth")
        try:
            metrics = self._metrics()
        except HistoryImportError:
            self._invalidate()
            raise
        self.applied_updates += 1
        return {**metrics, "snapshot": False, "sequence_verified": True}
