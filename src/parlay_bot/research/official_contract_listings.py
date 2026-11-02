"""Official, source-exact perpetual contract trading-start evidence.

Only Binance USD-M onboardDate and OKX USDT-SWAP listTime are treated as
published listing times. Missing, delisted, unreturned and other venues remain
UNKNOWN. A metadata timestamp is NOT a promise that archive bytes exist.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.source_family_coverage import ASSETS, VENUES

BINANCE = "https://fapi.binance.com/fapi/v1/exchangeInfo"
OKX = "https://www.okx.com/api/v5/public/instruments"
MAX_BYTES = 2_000_000


def _time(value: object, *, now: datetime) -> str:
    if type(value) not in (int, str) or not str(value).isdecimal():
        raise HistoryImportError("official contract listing timestamp missing")
    millis = int(value)
    stamp = datetime.fromtimestamp(millis / 1000, UTC)
    if (not datetime(2019, 1, 1, tzinfo=UTC) <= stamp
        <= now.astimezone(UTC) + timedelta(minutes=5)):
        raise HistoryImportError("official listing time outside plausible range")
    return stamp.isoformat()


def parse_official(venue: str, raw: bytes, *, now: datetime) -> list[dict]:
    if (venue not in {"binance", "okx"} or now.utcoffset() is None
        or not 0 < len(raw) <= MAX_BYTES):
        raise HistoryImportError("unsupported official listing evidence")
    try:
        doc = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise HistoryImportError("contract metadata JSON invalid") from exc
    if not isinstance(doc, dict):
        raise HistoryImportError("contract metadata must be object")
    if venue == "binance":
        rows = doc.get("symbols")
        if not isinstance(rows, list) or len(rows) > 3000:
            raise HistoryImportError("untrusted Binance exchange information")
    else:
        rows = doc.get("data")
        if (doc.get("code") != "0" or not isinstance(rows, list)
            or len(rows) > 3000):
            raise HistoryImportError("untrusted OKX instrument information")
    result = []
    for asset in ASSETS:
        instrument = (asset + "USDT" if venue == "binance"
                      else asset + "-USDT-SWAP")
        matches = [r for r in rows if isinstance(r, dict) and
                   r.get("symbol" if venue == "binance" else "instId") == instrument]
        state = "not_returned_by_current_metadata"
        published = None
        if len(matches) > 1:
            raise HistoryImportError("duplicate official source instrument")
        if matches:
            row = matches[0]
            if venue == "binance":
                identity = (
                    row.get("quoteAsset") == "USDT"
                    and row.get("contractType") == "PERPETUAL"
                    and row.get("baseAsset") == asset
                )
                field = "onboardDate"
            else:
                identity = (
                    row.get("instType") == "SWAP"
                    and row.get("instId") == instrument
                    and row.get("settleCcy") == "USDT"
                    and row.get("ctType") == "linear"
                )
                field = "listTime"
            if not identity:
                raise HistoryImportError("wrong market/contract in listing metadata")
            if row.get(field) not in (None, ""):
                published = _time(row[field], now=now)
                state = "official_contract_start_timestamp"
            else:
                state = "official_contract_start_not_published"
        result.append({
            "venue": venue, "asset": asset, "instrument": instrument,
            "contract_trading_start_utc": published,
            "evidence_state": state, "published_field": (
                "onboardDate" if venue == "binance" else "listTime"),
            "source_is_current_instrument_snapshot": True,
            "older_delisted_contracts_ruled_out": False,
            "original_provider_response_sha256": hashlib.sha256(raw).hexdigest(),
            "calendar_month_coverage_verified": False,
        })
    return result


async def _source(client: httpx.AsyncClient, venue: str) -> bytes:
    url = BINANCE if venue == "binance" else OKX
    params = None if venue == "binance" else {"instType": "SWAP"}
    async with client.stream(
        "GET", url, params=params, follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise HistoryImportError(
                f"{venue} contract source HTTP {response.status_code}")
        if response.is_redirect:
            raise HistoryImportError("contract metadata redirected")
        out = bytearray()
        async for block in response.aiter_bytes():
            out.extend(block)
            if len(out) > MAX_BYTES:
                raise HistoryImportError("official metadata response too large")
    return bytes(out)


async def collect(*, out_dir: Path, now: datetime | None = None,
                  publish: bool = True) -> dict:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    results = [{
        "venue": venue, "asset": asset, "contract_trading_start_utc": None,
        "evidence_state": "official_start_not_yet_integrated",
    } for venue in VENUES if venue not in {"binance", "okx"} for asset in ASSETS]
    failures = []
    out_dir.mkdir(parents=True, exist_ok=True)
    drive = _drive(Settings()) if publish else None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for venue in ("binance", "okx"):
                try:
                    raw = await _source(client, venue)
                    parsed = parse_official(venue, raw, now=stamp)
                    digest = hashlib.sha256(raw).hexdigest()
                    path = out_dir / f"{venue}-contract-metadata-{digest[:20]}.json"
                    if path.exists() and path.read_bytes() != raw:
                        raise HistoryImportError("immutable source metadata collision")
                    path.write_bytes(raw)
                    if drive is not None:
                        item = _artifact(path, 1)
                        saved = await drive.ensure_history_file(
                            artifact=item, batch_id="contract-meta-" + digest[:24],
                            mime_type="application/json",
                        )
                        await drive.verify_downloaded_artifact(
                            file_id=saved.file_id,
                            parent_id=drive.folders.history, artifact=item,
                        )
                    results.extend(parsed)
                except (httpx.HTTPError, HistoryImportError, OSError) as exc:
                    failures.append({"venue": venue, "error": str(exc)[:160]})
                    results.extend({
                        "venue": venue, "asset": asset,
                        "contract_trading_start_utc": None,
                        "evidence_state": "source_probe_failed_no_inference",
                    } for asset in ASSETS)
        report = {
            "schema_version": "rival-official-perpetual-listings-v1",
            "observed_at": stamp.isoformat(),
            "current_metadata_only": True,
            "all_listing_dates_verified": False,
            "assets": results, "failures": failures,
            "historical_data_availability_proven": False,
            "source_content_readback_verified": publish and not failures,
        }
        path = out_dir / "official-contract-listing-evidence.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if drive is not None:
            item = _artifact(path, len(results))
            await drive.ensure_research_output(
                artifact=item, batch_id="listing-report-" + item.sha256[:24],
                kind="report",
            )
        return report
    finally:
        if drive is not None:
            await drive.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args()
    report = asyncio.run(collect(out_dir=args.out_dir))
    print(json.dumps({
        "official_listing_dates_found": sum(
            bool(r["contract_trading_start_utc"]) for r in report["assets"]),
        "unresolved_instruments": sum(
            not bool(r["contract_trading_start_utc"]) for r in report["assets"]),
        "provider_failures": report["failures"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
