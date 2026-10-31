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


class NativeMonthlyPublisher:
    """Copy at most a few complete venue months to the EXISTING private Drive.

    Reuses the cold-archive schedule and OAuth, with persisted receipts.
    A later backfill cannot rewrite a published dataset or live prediction.
    """

    def __init__(self, market, drive, output_dir: Path) -> None:
        self.market = market
        self.drive = drive
        self.output_dir = Path(output_dir)

    def initialize(self) -> None:
        self.market.initialize()
        with self.market.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_native_research_receipts (
                   venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                   month TEXT NOT NULL, data_sha256 TEXT NOT NULL,
                   history_file_id TEXT NOT NULL, manifest_file_id TEXT NOT NULL,
                   published_at TEXT NOT NULL,
                   PRIMARY KEY(venue,contract,quote,month)
                )"""
            )

    def _eligible(self, now: datetime) -> list[tuple[str, str, str, str]]:
        from parlay_bot.research.batch import complete_months

        self.initialize()
        months = complete_months(now=now, count=6)
        floor = months[0] + "-01T00:00:00+00:00"
        ceiling = now.astimezone(UTC).strftime("%Y-%m") + "-01T00:00:00+00:00"
        with self.market.store.connect() as conn:
            groups = conn.execute(
                """SELECT c.venue,c.contract,c.quote,substr(c.opened_at,1,7) AS month,
                          COUNT(*) AS n
                   FROM perp_market_candles c
                   LEFT JOIN rival_native_research_receipts r
                     ON r.venue=c.venue AND r.contract=c.contract
                    AND r.quote=c.quote AND r.month=substr(c.opened_at,1,7)
                   WHERE c.interval_minutes=5 AND c.opened_at>=?
                     AND c.opened_at<? AND r.month IS NULL
                   GROUP BY c.venue,c.contract,c.quote,substr(c.opened_at,1,7)
                   ORDER BY month,c.venue,c.contract""",
                (floor, ceiling),
            ).fetchall()
        selected = []
        for row in groups:
            if row["venue"] not in VENUES or row["month"] not in months:
                continue
            year, mon = map(int, row["month"].split("-"))
            first = datetime(year, mon, 1, tzinfo=UTC)
            end = (first.replace(year=year+1, month=1) if mon == 12
                   else first.replace(month=mon+1))
            expected = int((end-first)/STEP)
            if int(row["n"]) == expected:
                selected.append((
                    str(row["venue"]), str(row["contract"]), str(row["quote"]),
                    str(row["month"]),
                ))
        return selected

    async def run_once(self, *, now: datetime | None = None,
                       max_batches: int = 2) -> list[dict]:
        import asyncio

        from parlay_bot.research.publish import _artifact

        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None or not 1 <= max_batches <= 4:
            raise ValueError("invalid native publication clock or batch limit")
        candidates = await asyncio.to_thread(self._eligible, stamp)
        copied = []
        for venue, contract, quote, month in candidates:
            if len(copied) >= max_batches:
                break
            try:
                result = await asyncio.to_thread(
                    export_source_month, self.market,
                    venue=venue, contract=contract, quote=quote,
                    month=month, root=self.output_dir, now=stamp,
                )
            except HistoryImportError:
                # Missing/malformed sources remain unqualified, not silently
                # patched or marked as already published.
                continue
            history_artifact = _artifact(result.data_path, result.records)
            manifest_artifact = _artifact(result.manifest_path, 1)
            source_batch = "native-" + history_artifact.sha256[:24]
            history = await self.drive.ensure_history_file(
                artifact=history_artifact, batch_id=source_batch,
                mime_type="application/x-ndjson",
            )
            await self.drive.verify_downloaded_artifact(
                file_id=history.file_id, parent_id=self.drive.folders.history,
                artifact=history_artifact,
            )
            manifest = await self.drive.ensure_manifest_file(
                artifact=manifest_artifact, batch_id=source_batch,
            )
            await self.drive.verify_downloaded_artifact(
                file_id=manifest.file_id, parent_id=self.drive.folders.manifests,
                artifact=manifest_artifact,
            )
            with self.market.store.connect() as conn:
                conn.execute(
                    """INSERT INTO rival_native_research_receipts
                       (venue,contract,quote,month,data_sha256,history_file_id,
                        manifest_file_id,published_at)
                       VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (venue,contract,quote,month,history_artifact.sha256,
                     history.file_id,manifest.file_id,stamp.isoformat()),
                )
            copied.append({
                "venue": venue, "contract": contract, "quote": quote,
                "month": month, "rows": result.records,
                "sha256": history_artifact.sha256,
            })
        return copied
