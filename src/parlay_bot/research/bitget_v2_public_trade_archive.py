"""Capture Bitget's *public* v2 90-day historical futures trades as original bytes.

The official endpoint does not serve pre-90-day trades or historical depth.
Never infer a complete historical trade tape from a capped number of pages.
Immutable Drive source bytes + independent SHA-256 readback, research-only.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.bitget_v3_archive import ASSETS, Pacer
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

ENDPOINT = "https://api.bitget.com/api/v2/mix/market/fills-history"
SCHEMA = "rival-bitget-public-v2-90d-trades-v1"
PAGE_LIMIT = 1000
PAGE_MAX_BYTES = 500_000
MAX_PAGES = 32
MAX_DAYS_AGO = 89
ONE_DAY_MS = 86_400_000


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _millis(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HistoryImportError("Bitget public-trade timestamp type invalid")
    try:
        stamp = int(value)
    except (TypeError, ValueError) as exc:
        raise HistoryImportError("Bitget public-trade timestamp invalid") from exc
    if str(value) != str(stamp) or not 1_500_000_000_000 <= stamp:
        raise HistoryImportError("Bitget public-trade timestamp malformed")
    return stamp


def _positive(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise HistoryImportError("Bitget public-trade numeric type invalid")
    try:
        v = Decimal(str(value))
    except InvalidOperation as exc:
        raise HistoryImportError("Bitget public-trade numeric invalid") from exc
    if not v.is_finite() or v <= 0:
        raise HistoryImportError("Bitget public-trade quantity or price invalid")


def day_window(now: datetime, days_ago: int) -> tuple[int, int]:
    if now.utcoffset() is None or not 1 <= days_ago <= MAX_DAYS_AGO:
        raise HistoryImportError("Bitget historical trade day outside 90-day bound")
    now = now.astimezone(UTC)
    boundary = now.replace(hour=0, minute=0, second=0, microsecond=0)
    first = boundary - timedelta(days=days_ago)
    # Reject wholly future/unreleased periods and any day that begins outside
    # the public API's rolling 90-day availability.
    if first < now - timedelta(days=90):
        raise HistoryImportError("Bitget public API cannot fetch this historical day")
    start = int(first.timestamp() * 1000)
    return start, start + ONE_DAY_MS


def validate_page(
    raw: bytes, *, asset: str, start: int, end: int,
    cursor: str | None, now_ms: int,
) -> tuple[list[dict], str | None]:
    if (asset not in ASSETS or not 0 < end - start <= 7 * ONE_DAY_MS
        or not 0 < len(raw) <= PAGE_MAX_BYTES):
        raise HistoryImportError("Bitget public trade page outside source contract")
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HistoryImportError("Bitget public trade response invalid JSON") from exc
    if not isinstance(obj, dict) or obj.get("code") != "00000":
        raise HistoryImportError("Bitget public trade API rejected request")
    response_clock = _millis(obj.get("requestTime"))
    # Captured now_ms precedes the HTTP request. Bitget's genuine
    # requestTime is typically milliseconds *after* that reference clock.
    # Permit bounded request/transit skew, but never arbitrary stale/future.
    if abs(now_ms - response_clock) > 120_000:
        raise HistoryImportError("Bitget public trade API response clock invalid")
    rows = obj.get("data")
    if not isinstance(rows, list) or len(rows) > PAGE_LIMIT:
        raise HistoryImportError("Bitget public-trade page unbounded")
    previous: int | None = int(cursor) if cursor is not None else None
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") != asset + "USDT":
            raise HistoryImportError("Bitget historical trade wrong contract")
        ident = row.get("tradeId")
        if (not isinstance(ident, str) or not ident.isascii()
            or not ident.isdecimal() or not ident or len(ident) > 60):
            raise HistoryImportError("Bitget historical trade ID invalid")
        uid = int(ident)
        if uid <= 0 or (previous is not None and uid >= previous):
            raise HistoryImportError("Bitget historical trade cursor not descending")
        if uid in seen:
            raise HistoryImportError("Bitget historical trade duplicate ID")
        seen.add(uid)
        previous = uid
        if not start <= _millis(row.get("ts")) < end:
            raise HistoryImportError("Bitget historical trade outside requested UTC day")
        if str(row.get("side", "")).lower() not in ("buy", "sell"):
            raise HistoryImportError("Bitget historical trade side invalid")
        _positive(row.get("price"))
        _positive(row.get("size"))
    # Need a full page to know that an additional page *may* exist; a short
    # response ends this API pagination but cannot prove an exchange-wide
    # complete historical tape. Do not fabricate a cursor on empty pages.
    next_cursor = str(previous) if len(rows) == PAGE_LIMIT else None
    return rows, next_cursor


async def collect_day(
    client: httpx.AsyncClient, *, asset: str, start: int, end: int,
    now: datetime, max_pages: int, pacer: Pacer,
) -> tuple[list[dict], dict]:
    if (asset not in ASSETS or not 1 <= max_pages <= MAX_PAGES
        or now.utcoffset() is None):
        raise HistoryImportError("Bitget public trade scan budget invalid")
    clock = int(now.timestamp() * 1000)
    pages = []
    cursor: str | None = None
    known: set[str] = set()
    state = "unknown"
    for page in range(1, max_pages + 1):
        params = {
            "symbol": asset + "USDT", "productType": "USDT-FUTURES",
            "startTime": str(start), "endTime": str(end - 1),
            "limit": str(PAGE_LIMIT),
        }
        if cursor is not None:
            params["idLessThan"] = cursor
        raw: bytes | None = None
        for attempt in range(3):
            await pacer.wait()
            try:
                response = await client.get(
                    ENDPOINT, params=params, follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise HistoryImportError(
                        "Bitget public trade API transport failure"
                    ) from exc
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 200:
                raw = response.content
                break
            if response.status_code not in (429, 500, 502, 503, 504):
                raise HistoryImportError(
                    f"Bitget public trade history HTTP {response.status_code}"
                )
            if attempt == 2:
                raise HistoryImportError("Bitget public trade API retries exhausted")
            await asyncio.sleep(2 ** attempt)
        if raw is None:
            raise HistoryImportError("Bitget public trade response absent")
        trades, next_cursor = validate_page(
            raw, asset=asset, start=start, end=end,
            cursor=cursor, now_ms=clock,
        )
        if next_cursor is not None and next_cursor in known:
            raise HistoryImportError("Bitget public trade cursor repeated")
        pages.append({
            "page": page, "request_url": ENDPOINT, "params": params,
            "response_sha256": sha(raw),
            "response_base64": base64.b64encode(raw).decode("ascii"),
            "records": len(trades),
            "oldest_trade_id": trades[-1]["tradeId"] if trades else None,
            "newest_trade_id": trades[0]["tradeId"] if trades else None,
        })
        if next_cursor is None:
            state = "source_page_short_or_empty"
            break
        known.add(next_cursor)
        cursor = next_cursor
    else:
        state = "page_budget_exhausted"
    report = {
        "asset": asset, "contract": asset + "USDT", "utc_date":
            datetime.fromtimestamp(start / 1000, UTC).date().isoformat(),
        "utc_day_start": start, "utc_day_end_exclusive": end,
        "source_url": ENDPOINT, "page_limit": PAGE_LIMIT,
        "source_pages": len(pages),
        "source_trades": sum(x["records"] for x in pages),
        "pagination_stop_reason": state,
        "pagination_continuation_required": state == "page_budget_exhausted",
        "last_id_cursor": cursor if state == "page_budget_exhausted" else None,
        "full_trade_tape_verified": False,
        "full_utc_day_verified": False,
        "historical_depth_available_from_this_api": False,
        "provider_signed_checksum": False, "research_only": True,
        "model_training_eligible": False,
        "prospective_forecast": False, "live_trading_authorized": False,
    }
    return pages, report


async def publish(drive, *, root: Path, pages: list[dict], receipt: dict) -> dict:
    if not pages or receipt["source_pages"] != len(pages):
        raise HistoryImportError("Bitget public trades no verifiable source pages")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".part", dir=root, delete=False,
    ) as temporary_file:
        temp = Path(temporary_file.name)
    try:
        with gzip.open(temp, "wb") as z:
            for item in pages:
                z.write((json.dumps(item, sort_keys=True) + "\n").encode())
        digest = sha(temp.read_bytes())
        ident = f"bitget-v2-trades-{receipt['asset']}-{receipt['utc_date']}--{digest[:16]}"
        original = root / (ident + ".responses.jsonl.gz")
        temp.replace(original)
        manifest = root / (ident + ".manifest.json")
        proof = {
            **receipt, "schema_version": SCHEMA,
            "raw_file": original.name, "raw_sha256": digest,
            "raw_source_observed_at": datetime.now(UTC).isoformat(),
            "original_source_sha256s": [p["response_sha256"] for p in pages],
        }
        manifest.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
        raw_artifact = _artifact(original, receipt["source_trades"])
        manifest_artifact = _artifact(manifest, 1)
        batch = "bitget-v2-trades-" + digest[:24]
        raw_saved = await drive.ensure_history_file(
            artifact=raw_artifact, batch_id=batch, mime_type="application/gzip",
        )
        await drive.verify_downloaded_artifact(
            file_id=raw_saved.file_id, parent_id=drive.folders.history,
            artifact=raw_artifact,
        )
        manifest_saved = await drive.ensure_manifest_file(
            artifact=manifest_artifact, batch_id=batch,
        )
        await drive.verify_downloaded_artifact(
            file_id=manifest_saved.file_id, parent_id=drive.folders.manifests,
            artifact=manifest_artifact,
        )
        return {
            "status": "source_pages_published_and_readback_verified",
            **receipt, "source_sha256": raw_artifact.sha256,
            "manifest_sha256": manifest_artifact.sha256,
            "drive_sha256_readback_verified": True,
        }
    finally:
        temp.unlink(missing_ok=True)


async def run(
    *, days_ago: int, max_pages: int, asset: str | None,
    root: Path, report_dir: Path, probe_only: bool,
) -> dict:
    clock = datetime.now(UTC)
    start, end = day_window(clock, days_ago)
    assets = (asset,) if asset else ASSETS
    status = {"schema_version": SCHEMA, "target_utc_day":
              datetime.fromtimestamp(start / 1000, UTC).date().isoformat(),
              "days_ago": days_ago, "asset_reports": [], "failed_assets": [],
              "all_days_complete": False,
              "historical_2021_depth_trades_retrieved": False}
    pacer = Pacer(0.5)
    async with httpx.AsyncClient(timeout=httpx.Timeout(25)) as client:
        for coin in assets:
            try:
                pages, info = await collect_day(
                    client, asset=coin, start=start, end=end,
                    now=clock, max_pages=max_pages, pacer=pacer,
                )
                if probe_only:
                    report = {"status": "source_probe_only", **info}
                else:
                    drive = _drive(Settings())
                    try:
                        report = await publish(
                            drive, root=root, pages=pages, receipt=info,
                        )
                    finally:
                        await drive.aclose()
                status["asset_reports"].append(report)
            except (HistoryImportError, httpx.HTTPError, OSError) as exc:
                status["failed_assets"].append({
                    "asset": coin, "reason": str(exc)[:180],
                })
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"bitget-v2-trades-{days_ago:02d}d-report.json"
    path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Source-original 90d Bitget public trades")
    parser.add_argument("--days-ago", type=int, default=89)
    parser.add_argument("--asset", choices=ASSETS)
    parser.add_argument("--max-pages", type=int, default=32)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("data/bitget-v2-public-trades"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)
    if not 1 <= args.max_pages <= MAX_PAGES:
        parser.error("max_pages exceeds safe source API budget")
    result = asyncio.run(run(
        days_ago=args.days_ago, max_pages=args.max_pages, asset=args.asset,
        root=args.root, report_dir=args.report_dir, probe_only=args.probe_only,
    ))
    print(json.dumps(result, sort_keys=True))
    if result["failed_assets"] or not result["asset_reports"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
