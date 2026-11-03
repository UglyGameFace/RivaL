"""Live 5-minute liquidity and order-flow record (issue #295).

From the existing public websocket feeds, keep one immutable row per
venue x asset x UTC 5-minute bucket: spread, depth near the mid, and
aggressor buy/sell trade flow. Raw ticks stay in RAM; only the finished
bucket summary is stored. Rows describe what was observable by the end of
their bucket (``recorded_at``), so they are causal inputs for later
forecasts, never labels or proof of edge.

Sources (public, read-only):
- Binance USD-M: ``<symbol>@depth20@500ms`` (top 20 levels) and
  ``<symbol>@aggTrade`` (``m`` true = buyer is maker = aggressive sell).
- Hyperliquid: ``l2Book`` (top levels per side) and ``trades``
  (``side`` "B" = aggressive buy, "A" = aggressive sell).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from parlay_bot.perpetuals.live_quotes import ASSETS

BUCKET = timedelta(minutes=5)
# A bucket is flushed once its end is this far in the past, so late frames
# for the same bucket can still be counted.
FLUSH_GRACE = timedelta(seconds=15)
# A bucket counts as complete when the feed covered it within this slack.
COVERAGE_SLACK = timedelta(seconds=30)
DEPTH_BANDS_BPS = (10, 25)
VENUES = ("binance", "hyperliquid")
SYMBOL_TO_ASSET = {symbol: asset for asset, symbol in ASSETS.items()}

SCHEMA = """CREATE TABLE IF NOT EXISTS perp_liquidity_5m (
    venue TEXT NOT NULL,
    asset TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    complete INTEGER NOT NULL,
    book_samples INTEGER NOT NULL,
    spread_bps_mean DOUBLE PRECISION,
    spread_bps_max DOUBLE PRECISION,
    mid_last DOUBLE PRECISION,
    bid_depth_10bps_mean DOUBLE PRECISION,
    ask_depth_10bps_mean DOUBLE PRECISION,
    bid_depth_25bps_mean DOUBLE PRECISION,
    ask_depth_25bps_mean DOUBLE PRECISION,
    depth_25bps_covered_fraction DOUBLE PRECISION,
    buy_trades INTEGER NOT NULL,
    sell_trades INTEGER NOT NULL,
    buy_volume DOUBLE PRECISION NOT NULL,
    sell_volume DOUBLE PRECISION NOT NULL,
    first_event_at TEXT,
    last_event_at TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (venue, asset, bucket_start)
)"""
COLUMNS = (
    "venue", "asset", "bucket_start", "complete", "book_samples",
    "spread_bps_mean", "spread_bps_max", "mid_last",
    "bid_depth_10bps_mean", "ask_depth_10bps_mean",
    "bid_depth_25bps_mean", "ask_depth_25bps_mean",
    "depth_25bps_covered_fraction", "buy_trades", "sell_trades",
    "buy_volume", "sell_volume", "first_event_at", "last_event_at",
    "recorded_at",
)


def bucket_start(instant: datetime) -> datetime:
    if instant.utcoffset() is None:
        raise ValueError("liquidity clock must be timezone-aware")
    utc = instant.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 5, second=0, microsecond=0)


def _levels(raw: Any, *, limit: int = 50) -> list[tuple[float, float]]:
    """[(price, size)] from Binance [[p, q], ...] or Hyperliquid [{px, sz}]."""
    if not isinstance(raw, list):
        raise TypeError("book side must be a list")
    out = []
    for level in raw[:limit]:
        if isinstance(level, dict):
            price, size = float(level["px"]), float(level["sz"])
        else:
            price, size = float(level[0]), float(level[1])
        if not (math.isfinite(price) and math.isfinite(size)) or price <= 0 or size < 0:
            raise ValueError("invalid book level")
        out.append((price, size))
    return out


@dataclass
class _Bucket:
    start: datetime
    book_samples: int = 0
    spread_sum: float = 0.0
    spread_max: float = 0.0
    mid_last: float | None = None
    depth_sums: dict[str, float] = field(default_factory=lambda: {
        f"{side}_{band}": 0.0 for side in ("bid", "ask") for band in DEPTH_BANDS_BPS
    })
    depth_25_covered: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    first_event: datetime | None = None
    last_event: datetime | None = None

    def touch(self, at: datetime) -> None:
        if self.first_event is None or at < self.first_event:
            self.first_event = at
        if self.last_event is None or at > self.last_event:
            self.last_event = at


class LiquidityRecorder:
    """In-RAM 5-minute accumulators fed by the stream hub; drained to rows."""

    def __init__(self, *, assets: tuple[str, ...] = tuple(ASSETS)) -> None:
        if not assets or any(asset not in ASSETS for asset in assets):
            raise ValueError("invalid liquidity assets")
        self.assets = assets
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._ready: list[dict] = []
        self.rejected = 0

    # ---- feeding -------------------------------------------------------
    def _bucket(self, venue: str, asset: str, at: datetime) -> _Bucket | None:
        start = bucket_start(at)
        key = (venue, asset)
        current = self._buckets.get(key)
        if current is not None and start < current.start:
            return None  # A frame for an already-closed bucket is dropped.
        if current is not None and start > current.start:
            self._ready.append(self._row(venue, asset, current))
            current = None
        if current is None:
            current = _Bucket(start=start)
            self._buckets[key] = current
        return current

    def observe_book(self, venue: str, asset: str, at: datetime,
                     bids: list[tuple[float, float]],
                     asks: list[tuple[float, float]]) -> None:
        if not bids or not asks or asset not in self.assets:
            return
        best_bid, best_ask = bids[0][0], asks[0][0]
        if best_ask <= best_bid:
            self.rejected += 1
            return
        bucket = self._bucket(venue, asset, at)
        if bucket is None:
            return
        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid * 10_000
        bucket.book_samples += 1
        bucket.spread_sum += spread
        bucket.spread_max = max(bucket.spread_max, spread)
        bucket.mid_last = mid
        for band in DEPTH_BANDS_BPS:
            low = mid * (1 - band / 10_000)
            high = mid * (1 + band / 10_000)
            bucket.depth_sums[f"bid_{band}"] += sum(s for p, s in bids if p >= low)
            bucket.depth_sums[f"ask_{band}"] += sum(s for p, s in asks if p <= high)
        if bids[-1][0] <= mid * (1 - 25 / 10_000) and asks[-1][0] >= mid * (1 + 25 / 10_000):
            bucket.depth_25_covered += 1
        bucket.touch(at)

    def observe_trade(self, venue: str, asset: str, at: datetime, *,
                      size: float, aggressive_buy: bool) -> None:
        if asset not in self.assets or not math.isfinite(size) or size <= 0:
            return
        bucket = self._bucket(venue, asset, at)
        if bucket is None:
            return
        if aggressive_buy:
            bucket.buy_trades += 1
            bucket.buy_volume += size
        else:
            bucket.sell_trades += 1
            bucket.sell_volume += size
        bucket.touch(at)

    def ingest(self, venue: str, message: str | bytes) -> None:
        """Route one raw stream frame; anything else is ignored."""
        if isinstance(message, bytes):
            return
        try:
            wrapper = json.loads(message)
            if venue == "binance":
                self._binance(wrapper.get("data", wrapper))
            elif venue == "hyperliquid":
                self._hyperliquid(wrapper)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError):
            self.rejected += 1

    def _binance(self, raw: dict) -> None:
        event = raw.get("e")
        if event not in ("depthUpdate", "aggTrade"):
            return
        asset = SYMBOL_TO_ASSET.get(raw.get("s"))
        if asset is None:
            return
        stamp = raw["T"] if event == "aggTrade" else raw.get("T", raw["E"])
        if type(stamp) is not int or stamp <= 0:
            raise ValueError("missing Binance event time")
        at = datetime.fromtimestamp(stamp / 1000, tz=UTC)
        if event == "depthUpdate":
            self.observe_book("binance", asset, at, _levels(raw["b"]), _levels(raw["a"]))
        else:
            if type(raw.get("m")) is not bool:
                raise ValueError("missing Binance aggressor flag")
            self.observe_trade("binance", asset, at, size=float(raw["q"]),
                               aggressive_buy=not raw["m"])

    def _hyperliquid(self, wrapper: dict) -> None:
        channel = wrapper.get("channel")
        data = wrapper.get("data")
        if channel == "l2Book":
            asset = data["coin"]
            if asset not in ASSETS or type(data["time"]) is not int:
                return
            at = datetime.fromtimestamp(data["time"] / 1000, tz=UTC)
            bids, asks = data["levels"]
            self.observe_book("hyperliquid", asset, at, _levels(bids), _levels(asks))
        elif channel == "trades":
            for trade in data:
                asset = trade["coin"]
                side = trade["side"]
                if asset not in ASSETS or side not in ("B", "A") or type(trade["time"]) is not int:
                    continue
                at = datetime.fromtimestamp(trade["time"] / 1000, tz=UTC)
                self.observe_trade("hyperliquid", asset, at, size=float(trade["sz"]),
                                   aggressive_buy=side == "B")

    # ---- draining ------------------------------------------------------
    def _row(self, venue: str, asset: str, bucket: _Bucket) -> dict:
        end = bucket.start + BUCKET
        samples = bucket.book_samples
        complete = (
            bucket.first_event is not None and bucket.last_event is not None
            and bucket.first_event - bucket.start <= COVERAGE_SLACK
            and end - bucket.last_event <= COVERAGE_SLACK
            and samples > 0
        )

        def mean(key: str) -> float | None:
            return bucket.depth_sums[key] / samples if samples else None

        return {
            "venue": venue, "asset": asset,
            "bucket_start": bucket.start.isoformat(),
            "complete": int(complete), "book_samples": samples,
            "spread_bps_mean": bucket.spread_sum / samples if samples else None,
            "spread_bps_max": bucket.spread_max if samples else None,
            "mid_last": bucket.mid_last,
            "bid_depth_10bps_mean": mean("bid_10"),
            "ask_depth_10bps_mean": mean("ask_10"),
            "bid_depth_25bps_mean": mean("bid_25"),
            "ask_depth_25bps_mean": mean("ask_25"),
            "depth_25bps_covered_fraction": (
                bucket.depth_25_covered / samples if samples else None),
            "buy_trades": bucket.buy_trades, "sell_trades": bucket.sell_trades,
            "buy_volume": bucket.buy_volume, "sell_volume": bucket.sell_volume,
            "first_event_at": bucket.first_event.isoformat() if bucket.first_event else None,
            "last_event_at": bucket.last_event.isoformat() if bucket.last_event else None,
            "recorded_at": "",  # stamped when drained
        }

    def drain(self, *, now: datetime) -> list[dict]:
        """Return finished buckets (end + grace <= now), oldest first."""
        if now.utcoffset() is None:
            raise ValueError("liquidity clock must be timezone-aware")
        for key, bucket in list(self._buckets.items()):
            if bucket.start + BUCKET + FLUSH_GRACE <= now:
                self._ready.append(self._row(*key, bucket))
                del self._buckets[key]
        rows, self._ready = self._ready, []
        stamp = now.astimezone(UTC).isoformat()
        for row in rows:
            row["recorded_at"] = stamp
        return sorted(rows, key=lambda r: (r["bucket_start"], r["venue"], r["asset"]))


class LiquidityStore:
    """Immutable bucket rows in PostgreSQL (or SQLite in tests)."""

    def __init__(self, store) -> None:
        self.store = store
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.executescript(
                SCHEMA + ";\nCREATE INDEX IF NOT EXISTS idx_perp_liquidity_5m_asset_time "
                "ON perp_liquidity_5m (asset, bucket_start)"
            )
        self._ready = True

    def insert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        self.initialize()
        placeholders = ",".join("?" for _ in COLUMNS)
        written = 0
        with self.store.connect() as conn:
            for row in rows:
                cursor = conn.execute(
                    f"INSERT INTO perp_liquidity_5m ({','.join(COLUMNS)}) "
                    f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    tuple(row[c] for c in COLUMNS),
                )
                written += max(cursor.rowcount, 0)
        return written

    def recent(self, *, asset: str, limit: int = 12) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM perp_liquidity_5m WHERE asset=? "
                "ORDER BY bucket_start DESC LIMIT ?",
                (asset, limit),
            ).fetchall()
        return [dict(row) for row in rows]
