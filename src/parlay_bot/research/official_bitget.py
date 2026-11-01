"""Recover real source-native Bitget perpetual 5m history directly from its public API.

Runs off-host in GitHub Actions, not in the Discloud collector. The output is
strictly historical research, with actual API retrieval timestamps. Do not
label historical requests as pre-outcome observations, place orders, or
manufacture absent provider candles.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.batch import complete_months
from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.source_native import (
    SOURCE, restore_verified_source_months,
)

URL = "https://api.bitget.com/api/v3/market/history-candles"
INSTRUMENTS = "https://api.bitget.com/api/v3/market/instruments"
INTEGRITY = "official-bitget-rest-observed-at-v1"
STEP = timedelta(minutes=5)
BARS_PER_PAGE = 100
ASSETS = frozenset(("BTC", "ETH", "SOL", "XRP"))


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid source month") from exc
    if start.strftime("%Y-%m") != month:
        raise HistoryImportError("invalid source month")
    end = (start.replace(year=start.year + 1, month=1)
           if start.month == 12 else start.replace(month=start.month + 1))
    return start, end


def parse_history_page(payload: object, *, asset: str, start: datetime,
                       end: datetime, observed: datetime) -> list[dict]:
    """Validate the exact bounded page; never fill gaps with other venues."""
    if asset not in ASSETS or start.utcoffset() is None or end.utcoffset() is None:
        raise HistoryImportError("invalid official source identity/time window")
    if end <= start or end - start > BARS_PER_PAGE * STEP:
        raise HistoryImportError("unbounded official source page")
    data = payload.get("data") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("code") != "00000"
        or not isinstance(data, list) or len(data) > BARS_PER_PAGE + 1):
        raise HistoryImportError("official Bitget historical page unavailable")
    opened_by_time: dict[datetime, dict] = {}
    for raw in data:
        if not isinstance(raw, list) or len(raw) < 7:
            raise HistoryImportError("invalid Bitget futures candle shape")
        epoch = raw[0]
        if (isinstance(epoch, bool) or not isinstance(epoch, (str, int))
            or not str(epoch).isascii() or not str(epoch).isdecimal()
            or len(str(epoch)) != 13):
            raise HistoryImportError("invalid Bitget candle timestamp")
        stamp = int(epoch)
        if stamp % 300_000:
            raise HistoryImportError("Bitget candle off five-minute grid")
        opened = datetime.fromtimestamp(stamp / 1000, UTC)
        # The public API explicitly allows one older boundary-rounded bar.
        if start - STEP <= opened < start:
            continue
        if not start <= opened < end or opened in opened_by_time:
            raise HistoryImportError("wrong-window or duplicate Bitget candle")
        try:
            op, hi, lo, close, volume, turnover = map(float, raw[1:7])
        except (TypeError, ValueError, OverflowError) as exc:
            raise HistoryImportError("invalid Bitget OHLCV numbers") from exc
        if (not all(math.isfinite(v) for v in
                    (op, hi, lo, close, volume, turnover))
            or min(op, hi, lo, close) <= 0
            or lo > min(op, close) or hi < max(op, close)
            or volume < 0 or turnover < 0):
            raise HistoryImportError("invalid Bitget future OHLC envelope/volume")
        closed = opened + STEP - timedelta(milliseconds=1)
        if observed <= closed:
            raise HistoryImportError("historical API response observed before candle close")
        opened_by_time[opened] = {
            "venue": "bitget", "contract": asset+"USDT", "quote": "USDT",
            "interval_minutes": 5, "opened_at": opened.isoformat(),
            "closed_at": closed.isoformat(),
            "first_observed_at": observed.isoformat(),
            "open": op, "high": hi, "low": lo, "close": close,
            "volume_base": volume, "trades": -1,
        }
    expected = int((end - start) / STEP)
    if len(opened_by_time) != expected:
        raise HistoryImportError(
            f"official Bitget missing exact 5m candles: {expected-len(opened_by_time)}"
        )
    return [opened_by_time[start + i*STEP] for i in range(expected)]


async def _api(client: httpx.AsyncClient, url: str, params: dict) -> object:
    try:
        resp = await client.get(url, params=params, follow_redirects=False)
        resp.raise_for_status()
        # Do not follow redirects to third-party or web pages.
        if resp.status_code != 200 or len(resp.content) > 300_000:
            raise HistoryImportError("unverified or oversized official API response")
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HistoryImportError("official Bitget public API request failed") from exc


async def verify_instrument(client: httpx.AsyncClient, asset: str) -> None:
    if asset not in ASSETS:
        raise HistoryImportError("unsupported native Bitget perpetual symbol")
    result = await _api(client, INSTRUMENTS, {
        "category": "USDT-FUTURES", "symbol": asset+"USDT",
    })
    data = result.get("data") if isinstance(result, dict) else None
    good = [
        row for row in data if isinstance(row, dict)
        and row.get("symbol") == asset+"USDT"
        and row.get("baseCoin") == asset and row.get("quoteCoin") == "USDT"
        and row.get("category") == "USDT-FUTURES"
        and row.get("type") == "perpetual"
        and row.get("status") == "online"
    ] if isinstance(data, list) and result.get("code") == "00000" else []
    if len(good) != 1:
        raise HistoryImportError("official Bitget futures instrument unverified")


async def fetch_month(client: httpx.AsyncClient, *, asset: str,
                      month: str, root: Path, now: datetime | None = None):
    if asset not in ASSETS:
        raise HistoryImportError("unsupported historical symbol")
    start, end = _month_bounds(month)
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None or end > clock.astimezone(UTC):
        raise HistoryImportError("native source month has not completed")
    records = []
    raw_hash = hashlib.sha256()
    for page_start_idx in range(0, int((end-start)/STEP), BARS_PER_PAGE):
        page_start = start + page_start_idx * STEP
        page_end = min(page_start + BARS_PER_PAGE * STEP, end)
        params = {
            "category": "USDT-FUTURES", "symbol": asset+"USDT",
            "interval": "5m", "type": "market",
            # Bitget specifies strictly-after start / before end and may
            # return one preceding 5m candle when rounding boundaries.
            "startTime": str(int(page_start.timestamp()*1000) - 1),
            "endTime": str(int(page_end.timestamp()*1000) - 1),
            "limit": str(BARS_PER_PAGE),
        }
        result = await _api(client, URL, params)
        observed = datetime.now(UTC)
        page = parse_history_page(
            result, asset=asset, start=page_start, end=page_end,
            observed=observed,
        )
        records.extend(page)
        raw_hash.update(json.dumps(
            {"params": params, "result": result},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
        # One bounded request every >= 0.15s, comfortably under 20/s per IP.
        await asyncio.sleep(0.15)
    expected = int((end-start)/STEP)
    if len(records) != expected:
        raise HistoryImportError("source-native Bitget month incomplete")
    artifact = _persist(
        source=SOURCE, key=f"bitget-{asset}USDT-USDT-5m-{month}",
        records=records, raw_sha=raw_hash.hexdigest(), url=URL, out_dir=root,
        extra={
            "venue": "bitget", "contract": asset+"USDT", "quote": "USDT",
            "interval_minutes": 5, "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_integrity": INTEGRITY,
            "provider_signed_checksum": False,
            "actual_fills_verified": False,
            "exchange_response_signature_verified": False,
            "historical_first_observed_at_is_fetch_time": True,
            "source_api_category": "USDT-FUTURES",
            "source_api_interval": "5m", "source_api_type": "market",
        },
    )
    audited = audit_partition(artifact.manifest_path)
    if (not audited["full_calendar_month_verified"]
        or audited["rows"] != expected or audited["venue"] != "bitget"):
        raise HistoryImportError("verified official Bitget month continuity failed")
    return artifact


async def bootstrap(drive, *, asset: str, root: Path,
                    months: list[str], client: httpx.AsyncClient) -> dict:
    if asset not in ASSETS or len(months) != 6:
        raise HistoryImportError("unsupported bootstrap request")
    await verify_instrument(client, asset)
    existing = await restore_verified_source_months(
        drive, venue="bitget", months=months, root=root,
    )
    verified = set()
    for path in existing:
        meta = json.loads(path.read_text("utf-8"))
        if (meta.get("contract") == asset+"USDT"
            and meta.get("quote") == "USDT"
            and audit_partition(path)["full_calendar_month_verified"]):
            verified.add(meta["file"].split("-5m-")[1][:7])
    published = []
    from parlay_bot.research.publish import _artifact
    for month in months:
        if month in verified:
            continue
        archive = await fetch_month(
            client, asset=asset, month=month, root=root,
        )
        hist = _artifact(archive.data_path, archive.records)
        manifest = _artifact(archive.manifest_path, 1)
        batch = "native-" + hist.sha256[:24]
        uploaded_hist = await drive.ensure_history_file(
            artifact=hist, batch_id=batch, mime_type="application/x-ndjson",
        )
        await drive.verify_downloaded_artifact(
            file_id=uploaded_hist.file_id,
            parent_id=drive.folders.history, artifact=hist,
        )
        uploaded_manifest = await drive.ensure_manifest_file(
            artifact=manifest, batch_id=batch,
        )
        await drive.verify_downloaded_artifact(
            file_id=uploaded_manifest.file_id,
            parent_id=drive.folders.manifests, artifact=manifest,
        )
        published.append(month)
    # Run the real importer + exact-source trainer against the signed-in Drive
    # restore, not the temporary files we downloaded from the API.
    from parlay_bot.research.source_native_offline import run_native_research
    outcome = await run_native_research(
        drive=drive, venue="bitget", months=months, root=root/"restored",
        report_dir=root/"reports", publish=True,
    )
    if outcome.get("status") != "verified_research_published":
        raise HistoryImportError("official source months did not qualify for model research")
    return {
        "venue": "bitget", "asset": asset, "source_months": months,
        "previously_verified": sorted(verified), "new_months_published": published,
        "restored_manifests": outcome["restored_manifests"],
        "research_models_published": outcome["published_models"],
        "live_model_promoted": False, "trades_placed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch actual public Bitget historical futures and publish research")
    parser.add_argument("--asset", choices=sorted(ASSETS), default="BTC")
    parser.add_argument("--months", type=int, choices=[6], default=6)
    parser.add_argument("--root", type=Path, default=Path("data/bitget-bootstrap"))
    args = parser.parse_args()
    root = args.root
    from parlay_bot.research.publish import _drive
    drive = _drive(Settings())
    async def run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
            try:
                months = complete_months(now=datetime.now(UTC), count=args.months)
                return await bootstrap(
                    drive, asset=args.asset, root=root, months=months,
                    client=client,
                )
            finally:
                await drive.aclose()
    try:
        outcome = asyncio.run(run())
    except (HistoryImportError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Official Bitget bootstrap blocked safely: {exc}\n")
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
