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
        # Aggregate the large candle table before looking up archive receipts.
        # Joining every single candle to the receipt table forced millions
        # of repeated joins and held the small production PG pool too long.
        with self.market.store.connect() as conn:
            groups = conn.execute(
                """WITH counted_months AS (
                     SELECT venue,contract,quote,substr(opened_at,1,7) AS month,
                            COUNT(*) AS n
                     FROM perp_market_candles
                     WHERE interval_minutes=5 AND opened_at>=?
                       AND opened_at<?
                     GROUP BY venue,contract,quote,substr(opened_at,1,7)
                   )
                   SELECT m.venue,m.contract,m.quote,m.month,m.n
                   FROM counted_months m
                   LEFT JOIN rival_native_research_receipts r
                     ON r.venue=m.venue AND r.contract=m.contract
                    AND r.quote=m.quote AND r.month=m.month
                   WHERE r.month IS NULL
                   ORDER BY m.month,m.venue,m.contract""",
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


async def restore_verified_source_months(
    drive, *, venue: str, months: list[str], root: Path,
    contract: str | None = None,
) -> list[Path]:
    """Restore only actual source/month files from our private Drive archive.

    The importer verifies BOTH the signed-in account's folder boundary and
    SHA-256 for the manifest and the exact corresponding history bytes.
    Unknown, duplicate, malformed, oversized and cross-source files fail
    closed. Source archives are not historical live-trading forecasts.
    """
    if venue not in VENUES or not 1 <= len(months) <= 6:
        raise HistoryImportError("invalid or oversized native research request")
    if len(months) != len(set(months)):
        raise HistoryImportError("duplicate source research month")
    if contract is not None and (
        venue != "okx"
        or contract not in {"BTC-USDT-SWAP", "ETH-USDT-SWAP",
                            "SOL-USDT-SWAP", "XRP-USDT-SWAP"}
    ):
        raise HistoryImportError("invalid OKX source-native contract filter")
    await drive._verify_folder_boundary()
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        f"and name contains '{drive._escape_query(venue + '-')}' "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    # A long backfill produces >100 native manifests. Exhaust bounded Drive
    # pagination instead of treating the 101st verified month as an error.
    found = []
    page_token = None
    for _ in range(20):
        params = {
            "q": query, "spaces": "drive", "pageSize": 100,
            "fields": "nextPageToken,files(id,name,size,md5Checksum,parents,"
                      "appProperties,trashed)",
        }
        if page_token is not None:
            params["pageToken"] = page_token
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files",
            params=params, headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("private native manifest listing unavailable")
        result = response.json()
        page = result.get("files") if isinstance(result, dict) else None
        if not isinstance(page, list) or len(page) > 100:
            raise HistoryImportError("invalid native manifest Drive page")
        found.extend(page)
        page_token = result.get("nextPageToken")
        if page_token is None:
            break
        if not isinstance(page_token, str) or not page_token:
            raise HistoryImportError("invalid native Drive pagination token")
    else:
        raise HistoryImportError("native research manifest listing exceeds bounded pages")
    target = Path(root) / SOURCE
    target.mkdir(parents=True, exist_ok=True)
    imported = []
    unique = set()
    for entry in found:
        if (not isinstance(entry, dict)
            or not str(entry.get("name", "")).startswith(venue + "-")):
            raise HistoryImportError("unexpected native manifest listing record")
        # OKX month snapshots have exact, immutable -5m-YYYY-MM names.
        # Filter the Drive metadata *before* downloading each manifest.
        if venue == "okx" and not any(
            f"-5m-{month}--" in entry["name"] for month in months
        ):
            continue
        if contract is not None and not entry["name"].startswith(
            f"okx-{contract}-"
        ):
            continue
        item = drive._object(entry)
        props = entry.get("appProperties") or {}
        expected_sha = props.get("rival_sha256")
        if (folder not in item.parents or props.get("rival_kind") != "manifest"
            or not isinstance(expected_sha, str) or len(expected_sha) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha)
            or not isinstance(item.size_bytes, int)
            or not 0 < item.size_bytes <= 8192):
            raise HistoryImportError("native manifest metadata invalid")
        blob = await drive._client.get(
            f"{drive.DRIVE_URL}/files/{item.file_id}",
            params={"alt": "media"}, headers=await drive._headers(),
            follow_redirects=False,
        )
        if (blob.status_code != 200 or len(blob.content) != item.size_bytes
            or hashlib.sha256(blob.content).hexdigest() != expected_sha):
            raise HistoryImportError("native research manifest byte identity mismatch")
        try:
            info = json.loads(blob.content)
            if (info.get("source") != SOURCE or info.get("venue") != venue
                or not (
                    info.get("source_integrity") == "postgres-first-observed-at-v1"
                    or (
                        venue == "okx"
                        and info.get("source_integrity") == "okx-v5-public-history-5m-v1"
                        and info.get("source_url") ==
                            "https://www.okx.com/api/v5/market/history-candles"
                        and info.get("source_transport") == "official_public_https"
                        and info.get("provider_signed_checksum") is False
                    )
                    or (
                        venue == "okx"
                        and info.get("source_integrity") ==
                            "okx-v5-official-1m-zip-derived-5m-v1"
                        and isinstance(info.get("source_url"), str)
                        and info["source_url"].startswith(
                            "https://static.okx.com/cdn/okex/"
                        )
                        and info.get("source_transport") == "official_public_https"
                        and info.get("provider_signed_checksum") is False
                        and info.get("source_archive_type") ==
                            "one_minute_monthly_zip"
                        and isinstance(info.get("source_archive_files"), list)
                        and len(info["source_archive_files"]) == 2
                        and info["source_archive_files"][0].get("url") ==
                            info["source_url"]
                        and info.get("source_minutes_verified") ==
                            info.get("records", -1) * 5
                    )
                )
                or info.get("prospective_forecast") is not False
                or info.get("live_trading_authorized") is not False
                or info.get("interval_minutes") != 5):
                raise HistoryImportError("unverified native month provenance")
            month = info["file"].split("-5m-")[1][:7]
            identity = (info["venue"], info["contract"], info["quote"], month)
            if month not in months:
                continue
            if identity in unique:
                raise HistoryImportError("conflicting native month snapshots")
            unique.add(identity)
            name = info["file"]
            if (not isinstance(name, str) or Path(name).name != name
                or not name.endswith(".ndjson")
                or item.name != name.removesuffix(".ndjson") + ".manifest.json"):
                raise HistoryImportError("native source filename mismatch")
            history = await drive._find_batch_object(
                parent_id=drive.folders.history,
                batch_id="native-" + info["data_sha256"][:24],
                kind="history",
            )
            if (history is None or history.name != name
                or not isinstance(history.size_bytes, int)
                or not 0 < history.size_bytes <= 12_000_000):
                raise HistoryImportError("native source history missing or oversized")
            response = await drive._client.get(
                f"{drive.DRIVE_URL}/files/{history.file_id}",
                params={"alt": "media"}, headers=await drive._headers(),
                follow_redirects=False,
            )
            if (response.status_code != 200
                or len(response.content) != history.size_bytes
                or hashlib.sha256(response.content).hexdigest() != info["data_sha256"]):
                raise HistoryImportError("source-native history failed byte restore")
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise HistoryImportError("invalid source-native archive metadata") from exc
        path = target / item.name
        datapath = target / name
        for output, payload in ((path, blob.content), (datapath, response.content)):
            if output.exists() and output.read_bytes() != payload:
                raise HistoryImportError("existing native research dataset changed")
            if not output.exists():
                output.write_bytes(payload)
        imported.append(path)
    return imported

def source_month_readiness(
    market, *, venue: str, contract: str, quote: str,
    now: datetime | None = None,
) -> dict:
    """Bounded host-DB evidence for six *complete calendar* training months.

    Row counts alone are NOT continuity or byte proof. Only the existing
    publisher's persisted receipt identifies a month that completed exact
    source-grid checks and remote SHA-256 readback. Drive restoration still
    independently verifies stored bytes before any research model is trained.
    This function neither retrieves historical candles nor promotes models.
    """
    from parlay_bot.research.batch import complete_months

    if (venue not in VENUES or not contract or len(contract) > 64
        or quote not in {"USDT", "USDC", "USD"}):
        raise ValueError("unsupported source-native evidence identity")
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("source-native evidence requires a timezone-aware clock")
    months = complete_months(now=clock, count=6)
    start = months[0] + "-01T00:00:00+00:00"
    end = clock.astimezone(UTC).strftime("%Y-%m") + "-01T00:00:00+00:00"
    market.initialize()
    with market.store.connect() as conn:
        counted = conn.execute(
            """SELECT substr(opened_at,1,7) AS month, COUNT(*) AS bars,
                      MIN(opened_at) AS first_open, MAX(opened_at) AS last_open
               FROM perp_market_candles
               WHERE venue=? AND contract=? AND quote=? AND interval_minutes=5
                 AND opened_at>=? AND opened_at<?
               GROUP BY substr(opened_at,1,7)""",
            (venue, contract, quote, start, end),
        ).fetchall()
        # A new or partially migrated database can lack the receipt table.
        # That is UNKNOWN, never zero verified publications.
        try:
            receipts = conn.execute(
                """SELECT month,published_at FROM rival_native_research_receipts
                   WHERE venue=? AND contract=? AND quote=? AND month>=?
                     AND month<=?""",
                (venue, contract, quote, months[0], months[-1]),
            ).fetchall()
        except Exception as exc:
            message = str(exc).lower()
            if "no such table" not in message and "does not exist" not in message:
                raise
            receipts = None

    by_month = {str(row["month"]): row for row in counted}
    published = (
        {str(row["month"]): str(row["published_at"]) for row in receipts}
        if receipts is not None else None
    )
    details = []
    for month in months:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        last = (
            first.replace(year=first.year + 1, month=1)
            if first.month == 12 else first.replace(month=first.month + 1)
        )
        expected = int((last - first) / STEP)
        row = by_month.get(month)
        bars = int(row["bars"]) if row is not None else 0
        first_seen = str(row["first_open"]) if row is not None else None
        last_seen = str(row["last_open"]) if row is not None else None
        count_possible = (
            bars == expected and first_seen == first.isoformat()
            and last_seen == (last - STEP).isoformat()
        )
        has_receipt = published is not None and month in published
        details.append({
            "month": month, "bars": bars, "expected_bars": expected,
            "count_looks_complete": count_possible,
            "publisher_receipt": (
                "present" if has_receipt else
                "absent" if published is not None else "unknown"
            ),
            "status": (
                "published_receipt" if has_receipt and count_possible
                else "receipt_source_mismatch" if has_receipt
                else "count_only_not_certified" if count_possible
                else "incomplete"
            ),
        })
    verified = sum(x["status"] == "published_receipt" for x in details)
    return {
        "venue": venue, "contract": contract, "quote": quote,
        "months": details, "months_with_receipts": verified,
        "receipt_read_available": published is not None,
        "six_month_receipts_ready": verified == 6,
        "training_approved": False,
        "note": "Host receipts are not Drive restore, model import, or future proof",
    }
