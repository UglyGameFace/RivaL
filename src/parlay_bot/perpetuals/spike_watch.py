"""Source-exact autonomous anomaly watch and future-only observational scoring.

Uses the existing 5m collector, historical PostgreSQL bars and index; no
new exchange requests, LLM requests, trading orders, or backfill-as-live claims.
"""
from __future__ import annotations

import itertools
import json
import math
import statistics
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

ASSETS = ("BTC", "ETH", "SOL", "XRP")
MIN_SOURCES = 3
MAX_SOURCES = 12
SAMPLE_SIZE = 27
FRESH = timedelta(minutes=12)
SETTLE_LATE = timedelta(hours=2)


def utc(raw: object) -> datetime:
    value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    if value.utcoffset() is None:
        raise ValueError("timezone-aware observation required")
    return value.astimezone(UTC)


def contract_for(venue: str, asset: str) -> tuple[str, str]:
    template, quote = MARKET_SYMBOLS[venue]
    return ("XBTUSDTM" if venue == "kucoin" and asset == "BTC"
            else template.format(asset=asset), quote)


def summarize(rows: list[dict], now: datetime) -> dict | None:
    """Only contiguous, recently *observed* closed candles qualify."""
    if len(rows) != SAMPLE_SIZE:
        return None
    try:
        opened = [utc(r["opened_at"]) for r in rows]
        if any(b-a != timedelta(minutes=5) for a, b in itertools.pairwise(opened)):
            return None
        last = rows[-1]
        close_time = utc(last["closed_at"])
        first_seen = utc(last["first_observed_at"])
        if not (close_time <= first_seen <= now
                and timedelta(0) <= now-close_time <= FRESH
                and first_seen-close_time <= FRESH):
            return None
        closes = [float(r["close_price"]) for r in rows]
        volumes = [float(r["volume_base"]) for r in rows]
        if (any(not math.isfinite(x) or x <= 0 for x in closes)
                or any(not math.isfinite(x) or x < 0 for x in volumes)):
            return None
        historic = [abs(100 * math.log(b/a))
                    for a, b in itertools.pairwise(closes[:-1])]
        known_volumes = [x for x in volumes[:-1] if x > 0]
        ratio = (volumes[-1] / statistics.median(known_volumes)
                 if len(known_volumes) >= 16 else None)
        return {
            "opened_at": opened[-1].isoformat(),
            "close": closes[-1],
            "move": 100 * math.log(closes[-1]/closes[-2]),
            "typical_move": statistics.median(historic),
            "volume_ratio": ratio,
        }
    except (TypeError, KeyError, ValueError, OverflowError, ZeroDivisionError):
        return None


class SpikeWatch:
    """Crash-safe small read/write ledger for proven observations."""

    def __init__(self, market: PerpetualMarketStore) -> None:
        self.market = market

    def initialize(self) -> None:
        with self.market.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_spike_status (
                  asset TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
                  status TEXT NOT NULL, fresh_venues INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS perp_spike_events (
                  asset TEXT NOT NULL, opened_at TEXT NOT NULL,
                  detected_at TEXT NOT NULL, kind TEXT NOT NULL,
                  direction TEXT NOT NULL, source_count INTEGER NOT NULL,
                  move_pct DOUBLE PRECISION NOT NULL,
                  volume_ratio DOUBLE PRECISION, sources_json TEXT NOT NULL,
                  evaluated_at TEXT, outcome TEXT, next_move_pct DOUBLE PRECISION,
                  evaluated_sources INTEGER,
                  PRIMARY KEY (asset,opened_at)
                );
                CREATE INDEX IF NOT EXISTS idx_spike_events_recent
                  ON perp_spike_events (asset,detected_at);
                """
            )

    def scan(self, *, asset: str, venues: tuple[str, ...],
             now: datetime | None = None) -> dict:
        if asset not in ASSETS or len(venues) > MAX_SOURCES:
            raise ValueError("unsupported asset or source count")
        clock = utc(now or datetime.now(UTC))
        feeds = {(str(r["venue"]), str(r["asset"])): r
                 for r in self.market.feed_health()}
        grouped: dict[str, list[dict]] = {}
        for venue in dict.fromkeys(venues):
            if venue not in MARKET_SYMBOLS:
                continue
            health = feeds.get((venue, asset))
            if not health or health["status"] != "ok" or not health.get("last_success_at"):
                continue
            try:
                if not timedelta(0) <= clock-utc(health["last_success_at"]) <= FRESH:
                    continue
            except (ValueError, TypeError):
                continue
            contract, quote = contract_for(venue, asset)
            item = summarize(self.market.recent_candles(
                venue=venue, contract=contract, quote=quote,
                interval_minutes=5, count=SAMPLE_SIZE,
            ), clock)
            if item is not None:
                item.update(venue=venue, contract=contract, quote=quote)
                grouped.setdefault(item["opened_at"], []).append(item)
        # Align actual UTC bar open, never mix different venues' prices.
        opened, rows = (max(grouped.items(), key=lambda x: (len(x[1]), x[0]))
                        if grouped else (None, []))
        event = None
        if len(rows) >= MIN_SOURCES:
            movement = statistics.median(x["move"] for x in rows)
            typical = statistics.median(x["typical_move"] for x in rows)
            volume_ratios = [x["volume_ratio"] for x in rows
                             if x["volume_ratio"] is not None]
            volume = (statistics.median(volume_ratios)
                      if len(volume_ratios) >= MIN_SOURCES else None)
            same_direction = sum(x["move"] * movement > 0 for x in rows)
            price = (same_direction >= MIN_SOURCES
                     and abs(movement) >= max(0.8, 3.5 * typical))
            vol = volume is not None and volume >= 2.5
            if price or vol:
                event = {
                    "asset": asset, "opened_at": opened,
                    "detected_at": clock.isoformat(),
                    "kind": "price_and_volume" if price and vol else
                            "price" if price else "volume",
                    "direction": "up" if movement > 0 else
                                 "down" if movement < 0 else "mixed",
                    "source_count": len(rows),
                    "move_pct": round(movement, 6),
                    "volume_ratio": round(volume, 5) if volume is not None else None,
                    "sources_json": json.dumps([
                        {k: x[k] for k in ("venue", "contract", "quote", "close")}
                        for x in rows
                    ], separators=(",", ":"), sort_keys=True),
                }
        state = "watching" if len(rows) >= MIN_SOURCES else "insufficient_fresh_sources"
        with self.market.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_spike_status
                    (asset,checked_at,status,fresh_venues) VALUES (?,?,?,?)
                    ON CONFLICT(asset) DO UPDATE SET
                    checked_at=excluded.checked_at,status=excluded.status,
                    fresh_venues=excluded.fresh_venues""",
                (asset, clock.isoformat(), state, len(rows)),
            )
            if event is not None:
                conn.execute(
                    """INSERT INTO perp_spike_events
                       (asset,opened_at,detected_at,kind,direction,source_count,
                        move_pct,volume_ratio,sources_json)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(asset,opened_at) DO NOTHING""",
                    tuple(event[key] for key in (
                        "asset", "opened_at", "detected_at", "kind",
                        "direction", "source_count", "move_pct",
                        "volume_ratio", "sources_json",
                    )),
                )
        return {"status": state, "sources": len(rows), "event": event}

    def settle(self, *, asset: str, now: datetime | None = None) -> int:
        """Score only market outcomes first observed after the saved detection."""
        if asset not in ASSETS:
            raise ValueError("unsupported asset")
        clock = utc(now or datetime.now(UTC))
        with self.market.store.connect() as conn:
            events = conn.execute(
                """SELECT * FROM perp_spike_events
                   WHERE asset=? AND evaluated_at IS NULL
                   ORDER BY detected_at DESC LIMIT 48""",
                (asset,),
            ).fetchall()
        updated = 0
        for raw in events:
            event = dict(raw)
            detected = utc(event["detected_at"])
            target = utc(event["opened_at"]) + timedelta(minutes=5)
            if clock < target + timedelta(minutes=5):
                continue
            moves = []
            for source in json.loads(event["sources_json"]):
                with self.market.store.connect() as conn:
                    bar = conn.execute(
                        """SELECT closed_at,first_observed_at,close_price
                           FROM perp_market_candles WHERE venue=? AND contract=?
                           AND quote=? AND interval_minutes=5 AND opened_at=?""",
                        (source["venue"], source["contract"], source["quote"],
                         target.isoformat()),
                    ).fetchone()
                if bar is None:
                    continue
                try:
                    observed = utc(bar["first_observed_at"])
                    closed = utc(bar["closed_at"])
                    price, previous = float(bar["close_price"]), float(source["close"])
                    if not (detected < closed <= observed <= clock
                            and observed - closed <= FRESH
                            and observed - detected <= SETTLE_LATE
                            and price > 0 and previous > 0
                            and math.isfinite(price) and math.isfinite(previous)):
                        continue
                    moves.append(100 * math.log(price / previous))
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
            if len(moves) >= MIN_SOURCES:
                next_move = statistics.median(moves)
                direction = str(event["direction"])
                outcome = ("mixed_no_direction" if direction == "mixed" else
                           "flat" if next_move == 0 else
                           "continuation" if next_move * (
                               1 if direction == "up" else -1) > 0 else "reversal")
            elif clock - detected >= SETTLE_LATE:
                next_move = None
                outcome = "unverified_future_coverage"
            else:
                continue
            with self.market.store.connect() as conn:
                conn.execute(
                    """UPDATE perp_spike_events SET evaluated_at=?,outcome=?,
                       next_move_pct=?,evaluated_sources=?
                       WHERE asset=? AND opened_at=? AND evaluated_at IS NULL""",
                    (clock.isoformat(), outcome,
                     round(next_move, 6) if next_move is not None else None,
                     len(moves), asset, event["opened_at"]),
                )
            updated += 1
        return updated

    def snapshot(self, *, asset: str, now: datetime | None = None) -> dict:
        if asset not in ASSETS:
            raise ValueError("unsupported asset")
        clock = utc(now or datetime.now(UTC))
        try:
            with self.market.store.connect() as conn:
                status = conn.execute(
                    "SELECT * FROM perp_spike_status WHERE asset=?", (asset,),
                ).fetchone()
                rows = conn.execute(
                    """SELECT opened_at,kind,direction,source_count,move_pct,
                              volume_ratio,outcome,next_move_pct
                       FROM perp_spike_events
                       WHERE asset=? AND detected_at>=? AND detected_at<=?
                       ORDER BY detected_at DESC LIMIT 3""",
                    (asset, (clock-timedelta(hours=24)).isoformat(),
                     clock.isoformat()),
                ).fetchall()
                count = conn.execute(
                    """SELECT COUNT(*) AS n,
                       SUM(CASE WHEN outcome='continuation' THEN 1 ELSE 0 END)
                       AS continued,
                       SUM(CASE WHEN outcome='reversal' THEN 1 ELSE 0 END)
                       AS reversed,
                       SUM(CASE WHEN outcome='unverified_future_coverage' THEN 1 ELSE 0 END)
                       AS unverified
                       FROM perp_spike_events WHERE asset=? AND detected_at>=?
                       AND detected_at<=?""",
                    (asset, (clock-timedelta(hours=24)).isoformat(),
                     clock.isoformat()),
                ).fetchone()
        except Exception:  # noqa: BLE001 - legacy PostgreSQL schema may be absent
            return {"status": "unavailable", "events_24h": None, "recent": [],
                    "scored_continuations_24h": None}
        result = "not_checked"
        checked = None
        if status is not None:
            checked = str(status["checked_at"])
            result = str(status["status"])
            try:
                if not timedelta(0) <= clock-utc(checked) <= FRESH:
                    result = "stale"
            except ValueError:
                result = "unavailable"
        return {
            "status": result, "checked_at": checked,
            "fresh_venues": int(status["fresh_venues"]) if status else 0,
            "events_24h": int(count["n"]),
            "scored_continuations_24h": int(count["continued"] or 0),
            "scored_reversals_24h": int(count["reversed"] or 0),
            "unverified_24h": int(count["unverified"] or 0),
            "recent": [dict(row) for row in rows],
            "self_learning": "future_only_observational;not_trained_promotion",
            "model_edge_verified": False,
        }
