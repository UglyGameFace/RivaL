"""Build research-only monthly 5m snapshots from RivaL's actual venue ledger.

This never changes the live candle store, retroactively issues a forecast,
or treats a recovered candle as having been observable before recovery.
A native archive is a source-specific, locally verifiable dataset, not
independent exchange execution evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.research.free_history import HistoryImportError, _persist

VENUES = frozenset({
    "hyperliquid", "dydx", "bitget", "gate", "okx", "kucoin",
    "deribit", "lighter", "phemex",
})
SOURCE = "verified_venue_perp_klines"
STEP = timedelta(minutes=5)


def export_source_month(market, *, venue: str, contract: str, quote: str,
                        month: str, root: Path, now: datetime | None = None):
    """Export only complete, continuous, original-source calendar partitions.

    The DB provides the factual venue/contract identity and original
    first-observed-at; it does not make historic source data live forecasts.
    Missing bars or conflicting observations abort publication altogether.
    """
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise HistoryImportError("source-native research requires an aware clock")
    if (venue not in VENUES or not contract or len(contract) > 64
        or not contract.isascii() or not all(c.isalnum() or c in "-_" for c in contract)
        or quote not in {"USDT", "USDC", "USD"}):
        raise HistoryImportError("unsupported native venue/instrument identity")
    try:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise HistoryImportError("native research month must be YYYY-MM") from exc
    if first.strftime("%Y-%m") != month:
        raise HistoryImportError("noncanonical native research month")
    last = (first.replace(year=first.year + 1, month=1)
            if first.month == 12 else first.replace(month=first.month + 1))
    if last > clock.astimezone(UTC):
        raise HistoryImportError("source-native research needs a completed month")
    expected = int((last - first) / STEP)
    market.initialize()
    with market.store.connect() as conn:
        rows = conn.execute(
            """SELECT venue,contract,quote,interval_minutes,opened_at,
                      closed_at,first_observed_at,open_price,high_price,
                      low_price,close_price,volume_base,trades
               FROM perp_market_candles
               WHERE venue=? AND contract=? AND quote=? AND interval_minutes=5
                 AND opened_at>=? AND opened_at<?
               ORDER BY opened_at LIMIT ?""",
            (venue, contract, quote, first.isoformat(), last.isoformat(),
             expected + 1),
        ).fetchall()
    if len(rows) != expected:
        raise HistoryImportError(
            "native candle month incomplete; source-specific research is blocked"
        )
    normalized = []
    for idx, raw in enumerate(rows):
        row = dict(raw)
        opened = datetime.fromisoformat(str(row["opened_at"]))
        closed = datetime.fromisoformat(str(row["closed_at"]))
        observed = datetime.fromisoformat(str(row["first_observed_at"]))
        canonical_open = first + idx * STEP
        if (opened.utcoffset() is None or closed.utcoffset() is None
            or observed.utcoffset() is None
            or opened.astimezone(UTC) != canonical_open
            or closed.astimezone(UTC) != canonical_open + STEP - timedelta(milliseconds=1)
            or observed < closed or observed > clock
            or any(row[k] != expected_value for k, expected_value in (
                ("venue", venue), ("contract", contract), ("quote", quote),
                ("interval_minutes", 5)
            ))):
            raise HistoryImportError("native history has a gap, wrong source or impossible clock")
        prices = [float(row[k]) for k in
                  ("open_price", "high_price", "low_price", "close_price")]
        volume = float(row["volume_base"])
        trades = int(row["trades"])
        if (not all(math.isfinite(p) and p > 0 for p in prices)
            or prices[2] > min(prices[0], prices[3])
            or prices[1] < max(prices[0], prices[3])
            or not math.isfinite(volume) or volume < 0
            or type(row["trades"]) is not int or trades < 0):
            raise HistoryImportError("invalid saved source-native OHLCV")
        normalized.append({
            "venue": venue, "contract": contract, "quote": quote,
            "interval_minutes": 5, "opened_at": opened.isoformat(),
            "closed_at": closed.isoformat(),
            "first_observed_at": observed.isoformat(),
            "open": prices[0], "high": prices[1], "low": prices[2],
            "close": prices[3], "volume_base": volume, "trades": trades,
        })
    # Hash the exact source record identity including actual observation time.
    # No claim of an independently signed provider checksum.
    digest = hashlib.sha256(json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    return _persist(
        source=SOURCE, key=f"{venue}-{contract}-{quote}-5m-{month}",
        records=normalized, raw_sha=digest,
        url="RivaL verified private venue ledger",
        out_dir=Path(root),
        extra={
            "venue": venue, "contract": contract, "quote": quote,
            "interval_minutes": 5, "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_integrity": "postgres-first-observed-at-v1",
            "provider_signed_checksum": False, "actual_fills_verified": False,
        },
    )
