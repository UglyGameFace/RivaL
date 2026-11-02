"""Bitget *original source* liquidity research into private RivaL Drive.

Every sample retains the original response, byte hash, exchange clock, request
and provenance. Depth and recent-trade endpoints are point-in-time snapshots,
NOT historical books or a complete trade tape. No live model is modified.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import json
import math
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.bitget_v3_archive import ASSETS, Pacer
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

HOST = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"
LIVE_FAMILIES = (
    "orderbook", "rpi-orderbook", "open-interest",
    "open-interest-history", "fills", "liquidations",
)
FUNDING_FAMILIES = ("history-fund-rate",)
FAMILIES = (*LIVE_FAMILIES, *FUNDING_FAMILIES)
SCHEMA = "rival-bitget-liquidity-original-api-batch-v1"
MAX_BODY = 512_000
MAX_PAGES = {"liquidations": 3, "history-fund-rate": 3}
MAX_REQUESTS = 44  # 4 symbols x 6 live calls + 2 extra liquidation pages.
DEPTH = 100


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _number(x: object, *, positive: bool = True) -> Decimal:
    if not isinstance(x, (int, float, str)) or isinstance(x, bool):
        raise HistoryImportError("invalid Bitget numeric type")
    try:
        value = Decimal(str(x))
    except InvalidOperation as exc:
        raise HistoryImportError("invalid Bitget numerical value") from exc
    if not value.is_finite() or (value <= 0 if positive else value < 0):
        raise HistoryImportError("invalid Bitget nonpositive or nonfinite value")
    return value


def _time(x: object, now_ms: int, *, max_age_ms: int | None = None) -> int:
    if not isinstance(x, (str, int)) or isinstance(x, bool):
        raise HistoryImportError("invalid Bitget source timestamp type")
    try:
        stamp = int(x)
    except ValueError as exc:
        raise HistoryImportError("invalid Bitget source timestamp") from exc
    if (str(x) != str(stamp) or not 1_500_000_000_000 <= stamp
        or stamp > now_ms + 300_000
        or (max_age_ms is not None and now_ms - stamp > max_age_ms)):
        raise HistoryImportError("Bitget source timestamp stale, future or malformed")
    return stamp


def _orderbook(obj: object, *, rpi: bool, now_ms: int) -> dict:
    if not isinstance(obj, dict):
        raise HistoryImportError("Bitget depth data is not an object")
    stamp = _time(obj.get("ts"), now_ms, max_age_ms=120_000)
    expected = 3 if rpi else 2
    prices = {}
    quantities = {}
    for key in ("a", "b"):
        levels = obj.get(key)
        if not isinstance(levels, list) or not 1 <= len(levels) <= DEPTH:
            raise HistoryImportError("Bitget depth levels missing or unbounded")
        seen = set()
        rows = []
        for row in levels:
            if not isinstance(row, list) or len(row) != expected:
                raise HistoryImportError("Bitget orderbook level schema mismatch")
            price = _number(row[0])
            quantity = _number(row[1], positive=False)
            if rpi:
                quantity += _number(row[2], positive=False)
            if price in seen:
                raise HistoryImportError("Bitget duplicate depth price level")
            seen.add(price)
            rows.append(price)
            if quantity < 0:
                raise HistoryImportError("negative depth quantity")
        if rows != sorted(rows, reverse=key == "b"):
            raise HistoryImportError("Bitget depth order inconsistent")
        prices[key] = rows[0]
        quantities[key] = sum(
            _number(x[1], positive=False) +
            (_number(x[2], positive=False) if rpi else Decimal(0))
            for x in levels
        )
    if prices["b"] >= prices["a"]:
        raise HistoryImportError("crossed or inverted Bitget orderbook")
    return {
        "source_ts_ms": stamp, "best_bid": str(prices["b"]),
        "best_ask": str(prices["a"]),
        "spread": str(prices["a"] - prices["b"]),
        "bid_quantity_base": str(quantities["b"]),
        "ask_quantity_base": str(quantities["a"]),
        "bid_levels": len(obj["b"]), "ask_levels": len(obj["a"]),
        "rpi_included": rpi,
    }


def validate(raw: bytes, *, asset: str, family: str, now_ms: int) -> dict:
    """Validate ONLY provider-supplied values; never synthesize missing rows."""
    if (asset not in ASSETS or family not in FAMILIES
        or not 0 < len(raw) <= MAX_BODY):
        raise HistoryImportError("Bitget liquidity API input outside scope")
    try:
        obj = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise HistoryImportError("Bitget source did not return JSON") from exc
    if not isinstance(obj, dict) or obj.get("code") != "00000":
        raise HistoryImportError("Bitget liquidity API rejected response")
    _time(obj.get("requestTime"), now_ms, max_age_ms=120_000)
    data = obj.get("data")
    if family in ("orderbook", "rpi-orderbook"):
        return _orderbook(data, rpi=family == "rpi-orderbook", now_ms=now_ms)
    if family == "open-interest":
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise HistoryImportError("Bitget open interest response incompatible")
        _time(data.get("ts"), now_ms, max_age_ms=120_000)
        rows = data["list"]
        if len(rows) != 1 or rows[0].get("symbol") != asset + "USDT":
            raise HistoryImportError("Bitget open interest symbol mismatch")
        _number(rows[0].get("openInterest"), positive=False)
        return {"records": 1, "source_ts_ms": int(data["ts"])}
    if family == "open-interest-history":
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise HistoryImportError("Bitget OI history schema mismatch")
        rows = data["list"]
        if len(rows) > 5000:
            raise HistoryImportError("Bitget OI history too large")
        seen = set()
        for item in rows:
            if not isinstance(item, dict) or item.get("symbol") != asset + "USDT":
                raise HistoryImportError("Bitget OI history symbol mismatch")
            t = _time(item.get("timestamp"), now_ms)
            if t in seen:
                raise HistoryImportError("duplicate Bitget OI history bucket")
            seen.add(t)
            for field in ("openInterest", "oneSideOpenInterest",
                          "openInterestValue", "oneSideOpenInterestValue"):
                _number(item.get(field), positive=False)
        return {"records": len(rows), "first_ts_ms": min(seen) if seen else None,
                "last_ts_ms": max(seen) if seen else None,
                "full_historical_coverage_verified": False}
    if family == "history-fund-rate":
        if not isinstance(data, dict) or not isinstance(data.get("resultList"), list):
            raise HistoryImportError("Bitget funding history schema mismatch")
        rows = data["resultList"]
        if len(rows) > 100:
            raise HistoryImportError("Bitget funding history page unbounded")
        stamps = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") != asset + "USDT":
                raise HistoryImportError("Bitget funding record wrong symbol")
            stamp = _time(row.get("fundingRateTimestamp"), now_ms)
            if stamp in stamps:
                raise HistoryImportError("Bitget duplicate funding settlement")
            stamps.add(stamp)
            value = _number(row.get("fundingRate"), positive=False) if (
                str(row.get("fundingRate", "")).startswith("-") is False
            ) else Decimal(str(row["fundingRate"]))
            if not value.is_finite() or abs(value) > Decimal("1"):
                raise HistoryImportError("Bitget funding value invalid")
        return {"records": len(rows), "last_ts_ms": max(stamps) if stamps else None,
                "full_historical_coverage_verified": False}
    if family == "fills":
        rows = data
    else:
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise HistoryImportError("Bitget liquidation data schema mismatch")
        rows = data["list"]
    if not isinstance(rows, list) or len(rows) > 100:
        raise HistoryImportError("Bitget trade / liquidation response unbounded")
    stamps = []
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryImportError("invalid Bitget trade row")
        if family == "fills":
            if not isinstance(row.get("execId"), str) or not row["execId"]:
                raise HistoryImportError("Bitget fill lacks execution identity")
            qty_field, time_field = "size", "ts"
        else:
            if row.get("symbol") != asset + "USDT":
                raise HistoryImportError("Bitget liquidation symbol mismatch")
            qty_field, time_field = "amount", "ts"
        if row.get("side") not in {"buy", "sell"}:
            raise HistoryImportError("Bitget trade side malformed")
        _number(row.get("price"))
        _number(row.get(qty_field))
        stamps.append(_time(row.get(time_field), now_ms))
    return {"records": len(rows), "last_ts_ms": max(stamps) if stamps else None,
            "complete_trade_tape_verified": False}


def _parameters(asset: str, family: str, page: int) -> dict:
    params = {"category": CATEGORY, "symbol": asset + "USDT"}
    if family in ("orderbook", "rpi-orderbook"):
        params["limit"] = str(DEPTH)
    elif family == "open-interest-history":
        params["period"] = "5m"
    elif family == "history-fund-rate":
        params.update({"limit": "100", "cursor": str(page)})
    elif family in ("fills", "liquidations"):
        params["limit"] = "100"
    return params


async def _request(client, pacer: Pacer, *, asset: str, family: str,
                   page: int = 1, cursor: str | None = None) -> tuple[bytes, dict]:
    params = _parameters(asset, family, page)
    if cursor is not None:
        if family != "liquidations" or not 1 <= len(cursor) <= 100:
            raise HistoryImportError("invalid Bitget liquidation cursor")
        params["cursor"] = cursor
    url = f"{HOST}/api/v3/market/{family}"
    for attempt in range(3):
        await pacer.wait()
        try:
            response = await client.get(url, params=params, follow_redirects=False)
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise HistoryImportError("Bitget source transport unavailable") from exc
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code == 200:
            return response.content, params
        if response.status_code not in (429, 500, 502, 503, 504):
            raise HistoryImportError(
                f"Bitget source endpoint rejected HTTP {response.status_code}"
            )
        if attempt == 2:
            raise HistoryImportError("Bitget source rate limited / unavailable")
        await asyncio.sleep(2 ** attempt)
    raise HistoryImportError("Bitget source retries exhausted")


async def collect(client: httpx.AsyncClient, *, now: datetime, mode: str,
                  pacer: Pacer | None = None) -> tuple[list[dict], dict]:
    if mode not in ("live", "funding") or now.utcoffset() is None:
        raise HistoryImportError("Bitget liquidity requires UTC-aware clock/mode")
    now = now.astimezone(UTC)
    now_ms = int(now.timestamp() * 1000)
    families = LIVE_FAMILIES if mode == "live" else FUNDING_FAMILIES
    pacing = pacer or Pacer(0.5)
    originals = []
    issues = []
    counted = 0
    for asset in ASSETS:
        for family in families:
            max_pages = MAX_PAGES.get(family, 1)
            cursor = None
            visited: set[str] = set()
            for page in range(1, max_pages + 1):
                if counted >= MAX_REQUESTS:
                    raise HistoryImportError("Bitget liquidity total request cap exceeded")
                counted += 1
                try:
                    raw, params = await _request(
                        client, pacing, asset=asset, family=family,
                        page=page, cursor=cursor,
                    )
                    metrics = validate(raw, asset=asset, family=family, now_ms=now_ms)
                except (HistoryImportError, ValueError) as exc:
                    issues.append({"asset": asset, "family": family,
                                   "page": page, "reason": str(exc)[:160]})
                    break
                original = {
                    "asset": asset, "family": family, "page": page,
                    "request_url": f"{HOST}/api/v3/market/{family}",
                    "params": params, "response_sha256": _sha(raw),
                    "response_base64": base64.b64encode(raw).decode("ascii"),
                    "metrics": metrics,
                }
                originals.append(original)
                if family not in MAX_PAGES:
                    break
                if family == "history-fund-rate":
                    if metrics["records"] < 100:
                        break
                elif family == "liquidations":
                    try:
                        data = json.loads(raw)["data"]
                        token = data.get("cursor")
                    except (ValueError, KeyError, AttributeError) as exc:
                        issues.append({"asset": asset, "family": family,
                                       "reason": "missing liquidation page cursor"})
                        break
                    if metrics["records"] < 100:
                        break
                    if not isinstance(token, (str, int)) or str(token) in visited:
                        issues.append({"asset": asset, "family": family,
                                       "reason": "invalid/repeated liquidation cursor"})
                        break
                    cursor = str(token)
                    visited.add(cursor)
    if not originals:
        raise HistoryImportError("Bitget liquidity returned no verified source responses")
    observed = {
        "schema_version": SCHEMA, "venue": "bitget",
        "category": CATEGORY, "mode": mode,
        "collected_at": now.isoformat(),
        "assets": list(ASSETS), "families_requested": list(families),
        "requests_attempted": counted,
        "responses_verified": len(originals),
        "issues": issues,
        "source_api": HOST + "/api/v3/market/",
        "point_in_time_snapshot_only": mode == "live",
        "historical_book_completeness_verified": False,
        "historical_trades_completeness_verified": False,
        "provider_signed_checksum": False,
        "model_training_eligible": False,
        "prospective_forecast": False,
        "live_trading_authorized": False,
    }
    return originals, observed


async def publish(drive, *, root: Path, records: list[dict],
                  report: dict) -> dict:
    """Commit immutable source bytes + provenance to Drive and hash-read back."""
    if not records or report.get("schema_version") != SCHEMA:
        raise HistoryImportError("Bitget liquidity source publication invalid")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(report["collected_at"]).strftime("%Y%m%dT%H%M%S")
    # gzip contains only original API response bytes in base64 plus each
    # exact request. Exchange timestamps stay distinct from collection time.
    with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with gzip.open(temporary, "wb") as z:
            for record in records:
                z.write((json.dumps(record, sort_keys=True, allow_nan=False)
                         + "\n").encode())
        raw_sha = _sha(temporary.read_bytes())
        stem = f"bitget-liquidity-{report['mode']}-{stamp}-{raw_sha[:16]}"
        source = root / (stem + ".responses.jsonl.gz")
        temporary.replace(source)
        manifest = root / (stem + ".manifest.json")
        body = {
            **report, "raw_file": source.name, "raw_sha256": raw_sha,
            "response_sha256s": [record["response_sha256"] for record in records],
            "verified_response_count": len(records),
            "first_observed_at": report["collected_at"],
        }
        manifest.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
        archived = _artifact(source, len(records))
        proof = _artifact(manifest, 1)
        batch = "bitget-liq-" + _sha(
            f"{report['mode']}:{stamp}:{raw_sha}".encode()
        )[:24]
        saved = await drive.ensure_history_file(
            artifact=archived, batch_id=batch, mime_type="application/gzip",
        )
        await drive.verify_downloaded_artifact(
            file_id=saved.file_id, parent_id=drive.folders.history,
            artifact=archived,
        )
        stamped = await drive.ensure_manifest_file(artifact=proof, batch_id=batch)
        await drive.verify_downloaded_artifact(
            file_id=stamped.file_id, parent_id=drive.folders.manifests,
            artifact=proof,
        )
        return {
            "status": "verified_source_archive",
            "mode": report["mode"], "responses": len(records),
            "issues": report["issues"],
            "raw_sha256": archived.sha256, "manifest_sha256": proof.sha256,
            "drive_sha256_readback_verified": True,
            "model_training_eligible": False,
            "live_trading_authorized": False,
        }
    finally:
        temporary.unlink(missing_ok=True)


async def run(*, mode: str, root: Path, report_dir: Path,
              probe_only: bool = False) -> dict:
    clock = datetime.now(UTC)
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        records, manifest = await collect(client, now=clock, mode=mode)
    if probe_only:
        result = {
            "status": "source_probe_only", **manifest,
            "records": len(records), "drive_verified": False,
        }
    else:
        drive = _drive(Settings())
        try:
            result = await publish(
                drive, root=root, records=records, report=manifest,
            )
        finally:
            await drive.aclose()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"bitget-liquidity-{mode}-progress.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bitget original liquidity archive")
    parser.add_argument("--mode", choices=("live", "funding"), default="live")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("data/bitget-liquidity"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(run(
            mode=args.mode, root=args.root, report_dir=args.report_dir,
            probe_only=args.probe_only,
        ))
    except (HistoryImportError, httpx.HTTPError, OSError) as exc:
        parser.exit(1, f"Bitget liquidity source archival failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
