"""Additive, source-verified historical context for RivaL's existing research warehouse.

These are retrospective covariates, NOT live quotes, executable books, labels,
model promotion, or evidence of real forward prediction skill.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.research.free_history import (
    ASSETS,
    HistoryImportError,
    _binance_rows,
    _checked_zip,
    _fetch,
    _month_key,
    _persist,
    _sha,
)

BASE = "https://data.binance.vision/data/futures/um/monthly"
POLY = "https://clob.polymarket.com/prices-history"
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
# The archive provides monthly files for these four data families.
KINDS = {
    "funding": ("fundingRate", "binance_um_funding"),
    "mark": ("markPriceKlines", "binance_um_mark_klines"),
    "index": ("indexPriceKlines", "binance_um_index_klines"),
    "premium": ("premiumIndexKlines", "binance_um_premium_klines"),
}
HISTORY_SOURCES = frozenset(v[1] for v in KINDS.values()) | {
    "polymarket_token_history", "kalshi_contract_candles",
}
_MAX_CONTRACT_POINTS = 100_000
_MAX_CONTRACT_JOBS = 40


def binance_supplement_url(*, asset: str, kind: str, month: str) -> str:
    if asset not in ASSETS or kind not in KINDS:
        raise HistoryImportError("unsupported Binance asset or supplement")
    month = _month_key(month)
    family = KINDS[kind][0]
    symbol = asset + "USDT"
    if kind == "funding":
        return f"{BASE}/{family}/{symbol}/{symbol}-{family}-{month}.zip"
    return f"{BASE}/{family}/{symbol}/5m/{symbol}-5m-{month}.zip"


def _binance_download(client: httpx.Client, url: str) -> bytes:
    filename = url.rsplit("/", 1)[-1]
    try:
        checksum = _fetch(client, url + ".CHECKSUM", limit=1024).decode("ascii").strip()
    except UnicodeError as exc:
        raise HistoryImportError("non-ASCII Binance checksum") from exc
    match = re.fullmatch(r"([0-9A-Fa-f]{64})\\s+\\*?([A-Za-z0-9_.-]+)", checksum)
    if match is None or match[2] != filename:
        raise HistoryImportError("missing or mismatched official Binance checksum")
    payload = _fetch(client, url)
    if _sha(payload) != match[1].lower():
        raise HistoryImportError("Binance supplementary archive SHA-256 mismatch")
    return _checked_zip(payload, filename[:-4] + ".csv"), _sha(payload)


def _finite(raw: object, *, allow_negative: bool = False) -> float:
    try:
        value = float(raw)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HistoryImportError("invalid supplementary numeric value") from exc
    if not math.isfinite(value) or (value < 0 and not allow_negative):
        raise HistoryImportError("nonfinite or prohibited negative source value")
    return value


def _premium_rows(data: bytes) -> tuple[list[dict], int]:
    """Premium/index basis can be negative; ordinary OHLC parser rejects it."""
    try:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
    except UnicodeError as exc:
        raise HistoryImportError("invalid premium CSV encoding") from exc
    step = 300_000
    previous = None
    rows = []
    gaps = 0
    for index, fields in enumerate(reader):
        if not fields:
            continue
        if index == 0 and fields[0].strip().lower() in {"open_time", "open time"}:
            continue
        if len(fields) != 12 or len(rows) >= _MAX_CONTRACT_POINTS:
            raise HistoryImportError("invalid premium kline columns/count")
        try:
            start, end, trades = int(fields[0]), int(fields[6]), int(fields[8])
        except (TypeError, ValueError, OverflowError) as exc:
            raise HistoryImportError("invalid premium kline time/trades") from exc
        if (start <= 0 or start % step or end != start + step - 1
            or trades < 0 or previous is not None and (
                start <= previous or (start - previous) % step
            )):
            raise HistoryImportError("invalid premium candle grid")
        gaps += 0 if previous is None else (start - previous) // step - 1
        op, high, low, close = (_finite(fields[i], allow_negative=True) for i in (1, 2, 3, 4))
        if low > min(op, close) or high < max(op, close) or high < low:
            raise HistoryImportError("inconsistent premium OHLC")
        # Volumes/trades in reference-price klines are source fields, not liquidity.
        for i in (5, 7, 9, 10):
            _finite(fields[i])
        if len(fields[11]) > 100:
            raise HistoryImportError("invalid premium ignored field")
        rows.append({
            "opened_at": datetime.fromtimestamp(start / 1000, UTC).isoformat(),
            "closed_at": datetime.fromtimestamp(end / 1000, UTC).isoformat(),
            "open": op, "high": high, "low": low, "close": close,
        })
        previous = start
    if not rows:
        raise HistoryImportError("empty premium archive")
    return rows, gaps


def _funding_rows(data: bytes) -> list[dict]:
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        names = set(reader.fieldnames or [])
    except UnicodeError as exc:
        raise HistoryImportError("invalid funding CSV encoding") from exc
    if not {"calc_time", "funding_interval_hours", "last_funding_rate"} <= names:
        raise HistoryImportError("official funding archive columns missing")
    output = []
    previous = None
    for item in reader:
        if len(output) >= _MAX_CONTRACT_POINTS:
            raise HistoryImportError("too many funding observations")
        try:
            when = int(item["calc_time"])
            hours = int(item["funding_interval_hours"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise HistoryImportError("invalid funding time or interval") from exc
        rate = _finite(item["last_funding_rate"], allow_negative=True)
        if (when <= 0 or when < 1_000_000_000_000
            or not 1 <= hours <= 24 or abs(rate) > 1
            or (previous is not None and when <= previous)):
            raise HistoryImportError("out-of-order or implausible funding observation")
        output.append({
            "funding_at": datetime.fromtimestamp(when / 1000, UTC).isoformat(),
            "funding_interval_hours": hours,
            "rate": rate,
        })
        previous = when
    if not output:
        raise HistoryImportError("empty funding archive")
    return output


def ingest_binance_supplement(
    client: httpx.Client, *, asset: str, kind: str, month: str, out_dir: Path,
):
    url = binance_supplement_url(asset=asset, kind=kind, month=month)
    csv_data, raw_sha = _binance_download(client, url)
    if kind == "funding":
        rows = _funding_rows(csv_data)
        gaps = 0
        field = "funding_at"
    elif kind == "premium":
        rows, gaps = _premium_rows(csv_data)
        field = "opened_at"
    else:
        rows, gaps = _binance_rows(csv_data, "5m")
        field = "opened_at"
    if rows[0][field][:7] != month or rows[-1][field][:7] != month:
        raise HistoryImportError("supplement archive outside stated month")
    # Mandatory separately versioned source identity, never a candle label.
    source = KINDS[kind][1]
    return _persist(
        source=source, key=f"{asset}USDT-{kind}-{month}", records=rows,
        raw_sha=raw_sha, url=url, out_dir=out_dir, gaps=gaps,
        extra={
            "venue": "binance", "contract": f"{asset}USDT",
            "market_type": "usd_m_linear_futures", "quote": "USDT",
            "feature_family": kind, "checksum_verified": True,
            "time_reference": "historical_exchange_event_not_live_first_observed",
            "no_verified_bid_ask_or_executability": True,
            "not_an_exact_polymarket_or_kalshi_settlement_oracle": True,
        },
    )


def _json_get(client: httpx.Client, url: str, params: dict) -> object:
    try:
        response = client.get(url, params=params, timeout=30, follow_redirects=False)
        if response.is_redirect:
            raise HistoryImportError("history provider redirected unexpectedly")
        response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            raise HistoryImportError("contract history exceeded byte budget")
        return response.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise HistoryImportError("contract history source unavailable or malformed") from exc


def _time_bounds(*, start_ts: int, end_ts: int) -> None:
    if (type(start_ts) is not int or type(end_ts) is not int
        or not 1_700_000_000 <= start_ts < end_ts
        or end_ts - start_ts > 200 * 86400
        or end_ts > int(datetime.now(UTC).timestamp())):
        raise HistoryImportError("invalid historical contract time window")


def ingest_polymarket_token(
    client: httpx.Client, *, condition_id: str, token_id: str,
    start_ts: int, end_ts: int, fidelity: int, out_dir: Path,
):
    _time_bounds(start_ts=start_ts, end_ts=end_ts)
    if (not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition_id)
        or not re.fullmatch(r"[0-9]{20,90}", token_id)
        or fidelity not in (1, 5, 15, 60, 240, 1440)):
        raise HistoryImportError("unverified Polymarket contract identity or fidelity")
    params = {"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity}
    raw = _json_get(client, POLY, params)
    if not isinstance(raw, dict) or not isinstance(raw.get("history"), list):
        raise HistoryImportError("missing Polymarket token history")
    observed = []
    previous = 0
    for point in raw["history"]:
        if not isinstance(point, dict) or type(point.get("t")) is not int:
            raise HistoryImportError("invalid Polymarket price point")
        instant = point["t"]
        value = _finite(point.get("p"))
        if (instant < start_ts or instant > end_ts or instant <= previous
            or not 0 < value < 1 or len(observed) >= _MAX_CONTRACT_POINTS):
            raise HistoryImportError("invalid Polymarket contract price sequence")
        observed.append({"at": datetime.fromtimestamp(instant, UTC).isoformat(), "price": value})
        previous = instant
    if not observed:
        raise HistoryImportError("no Polymarket price observations in requested range")
    key = f"{condition_id[2:18]}-{token_id[:20]}-{start_ts}-{end_ts}-{fidelity}"
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return _persist(
        source="polymarket_token_history", key=key, records=observed,
        raw_sha=_sha(payload), url=POLY, out_dir=out_dir,
        extra={
            "condition_id": condition_id, "token_id": token_id,
            "fidelity_minutes": fidelity, "range_unix": [start_ts, end_ts],
            "no_historical_orderbook_or_fill_evidence": True,
            "not_an_authenticated_resolution_receipt": True,
        },
    )


def ingest_kalshi_contract(
    client: httpx.Client, *, ticker: str,
    start_ts: int, end_ts: int, period_minutes: int, out_dir: Path,
):
    _time_bounds(start_ts=start_ts, end_ts=end_ts)
    if (not re.fullmatch(r"[A-Z0-9-]{2,110}", ticker)
        or period_minutes not in (1, 60, 1440)):
        raise HistoryImportError("invalid Kalshi ticker or period")
    url = f"{KALSHI}/historical/markets/{ticker}/candlesticks"
    raw = _json_get(client, url, {
        "start_ts": start_ts, "end_ts": end_ts, "period_interval": period_minutes,
    })
    if not isinstance(raw, dict) or not isinstance(raw.get("candlesticks"), list):
        raise HistoryImportError("missing archived Kalshi candlesticks")
    rows = []
    previous = 0
    for bar in raw["candlesticks"]:
        if not isinstance(bar, dict) or type(bar.get("end_period_ts")) is not int:
            raise HistoryImportError("invalid Kalshi candlestick")
        instant = bar["end_period_ts"]
        if (instant < start_ts or instant > end_ts or instant <= previous
            or len(rows) >= _MAX_CONTRACT_POINTS):
            raise HistoryImportError("out-of-order/out-of-range Kalshi candles")
        price_fields = {}
        for side in ("yes_bid", "yes_ask", "price"):
            value = bar.get(side)
            if value is None:
                price_fields[side] = None
                continue
            if not isinstance(value, dict):
                raise HistoryImportError("invalid Kalshi OHLC")
            p = {}
            for item in ("open", "high", "low", "close"):
                raw_v = value.get(item)
                if raw_v is None:
                    p[item] = None
                else:
                    amount = _finite(raw_v)
                    if not 0 <= amount <= 1:
                        raise HistoryImportError("out-of-range Kalshi binary price")
                    p[item] = amount
            present = [v for v in p.values() if v is not None]
            if len(present) == 4 and (p["low"] > min(p["open"], p["close"])
                                     or p["high"] < max(p["open"], p["close"])):
                raise HistoryImportError("Kalshi candle has inconsistent price range")
            price_fields[side] = p
        volume = bar.get("volume")
        oi = bar.get("open_interest")
        rows.append({
            "at": datetime.fromtimestamp(instant, UTC).isoformat(),
            **price_fields,
            "volume": _finite(volume) if volume is not None else None,
            "open_interest": _finite(oi) if oi is not None else None,
        })
        previous = instant
    if not rows:
        raise HistoryImportError("no Kalshi candles in archived contract range")
    return _persist(
        source="kalshi_contract_candles",
        key=f"{ticker}-{start_ts}-{end_ts}-{period_minutes}",
        records=rows, raw_sha=_sha(json.dumps(raw, sort_keys=True).encode()),
        url=url, out_dir=out_dir,
        extra={
            "ticker": ticker, "period_minutes": period_minutes,
            "range_unix": [start_ts, end_ts],
            "historical_quotes_not_executable_fill_proof": True,
            "settlement_outcome_not_included": True,
        },
    )


def _resume_verified(out_dir: Path, source: str, key: str) -> bool:
    for manifest in (out_dir / source).glob(key + "--*.manifest.json"):
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            name = metadata["file"]
            if (metadata.get("source") != source or Path(name).name != name
                or metadata.get("prospective_forecast") is not False
                or not (manifest.parent / name).is_file()):
                continue
            data = (manifest.parent / name).read_bytes()
            if (_sha(data) == metadata["data_sha256"]
                and len(data.splitlines()) == metadata["records"]):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


def supplemental_jobs(months: list[str], assets: list[str]) -> list[tuple]:
    if len(set(months)) != len(months) or not set(assets) <= ASSETS:
        raise HistoryImportError("invalid supplemental coverage")
    return [("binance", asset, kind, _month_key(month))
            for month in months for asset in assets for kind in KINDS]


def _identity(job: tuple) -> tuple[str, str]:
    family = job[0]
    if family == "binance":
        _, asset, kind, month = job
        return KINDS[kind][1], f"{asset}USDT-{kind}-{month}"
    if family == "polymarket":
        _, condition, token, start, end, fidelity = job
        return "polymarket_token_history", (
            f"{condition[2:18]}-{token[:20]}-{start}-{end}-{fidelity}"
        )
    if family == "kalshi":
        _, ticker, start, end, period = job
        return "kalshi_contract_candles", f"{ticker}-{start}-{end}-{period}"
    raise HistoryImportError("unrecognized supplementary history job")


def collect_supplemental(
    client: httpx.Client, *, root: Path, jobs: list[tuple], max_new: int = 100,
) -> dict:
    if not 1 <= max_new <= 100:
        raise ValueError("supplemental batch limit must be 1..100")
    result = {"planned": len(jobs), "already_verified": 0, "downloaded": 0,
              "rejected": 0, "remaining": 0, "failed": [],
              "live_predictions_modified": False}
    for job in jobs:
        try:
            source, key = _identity(job)
            if _resume_verified(root, source, key):
                result["already_verified"] += 1
                continue
            if result["downloaded"] + result["rejected"] >= max_new:
                result["remaining"] += 1
                continue
            if job[0] == "binance":
                ingest_binance_supplement(client, asset=job[1], kind=job[2],
                                           month=job[3], out_dir=root)
            elif job[0] == "polymarket":
                ingest_polymarket_token(
                    client, condition_id=job[1], token_id=job[2],
                    start_ts=job[3], end_ts=job[4], fidelity=job[5], out_dir=root,
                )
            else:
                ingest_kalshi_contract(
                    client, ticker=job[1], start_ts=job[2], end_ts=job[3],
                    period_minutes=job[4], out_dir=root,
                )
            result["downloaded"] += 1
        except HistoryImportError as exc:
            result["rejected"] += 1
            result["failed"].append({"source": job[0], "identity": str(job[1:]), "reason": str(exc)})
    return result


def _load_contract_jobs(path: Path | None) -> list[tuple]:
    if path is None:
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HistoryImportError("invalid exact-contract manifest") from exc
    if not isinstance(items, list) or len(items) > _MAX_CONTRACT_JOBS:
        raise HistoryImportError("contract manifest must contain at most 40 identities")
    result = []
    for x in items:
        if not isinstance(x, dict):
            raise HistoryImportError("invalid exact contract entry")
        venue = x.get("venue")
        if venue == "polymarket":
            job = ("polymarket", x["condition_id"], x["token_id"],
                   x["start_ts"], x["end_ts"], x.get("fidelity", 5))
        elif venue == "kalshi":
            job = ("kalshi", x["ticker"], x["start_ts"], x["end_ts"],
                   x.get("period_minutes", 60))
        else:
            raise HistoryImportError("unknown venue in contract manifest")
        # Validate before making even the first request.
        _identity(job)
        _time_bounds(start_ts=job[-3], end_ts=job[-2])
        result.append(job)
    if len(set(result)) != len(result):
        raise HistoryImportError("duplicate exact contract history request")
    return result


def _coverage_status(root: Path, jobs: list[tuple]) -> dict:
    families: dict[str, dict] = {}
    for job in jobs:
        source, key = _identity(job)
        item = families.setdefault(source, {"requested": 0, "verified": 0, "missing": 0})
        item["requested"] += 1
        item["verified" if _resume_verified(root, source, key) else "missing"] += 1
    return families


def _report_status(root: Path, jobs: list[tuple], acquired: dict) -> dict:
    return {
        "schema": "rival-supplemental-history-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "acquisition": acquired, "verified_coverage": _coverage_status(root, jobs),
        "capabilities_not_retroactively_proven": {
            "historical_order_book_depth": "not_collected_no_verified_archive",
            "realized_entry_exit_fills": "not_collected_no_trade_receipts",
            "options_implied_volatility": "not_collected_source_pending",
            "on_chain_transaction_metrics": "not_collected_source_pending",
            "macroeconomic_event_timestamps": "not_collected_source_pending",
            "historical_perpetual_open_interest": "not_collected_source_pending",
            "contract_settlement_receipts": "not_collected_exact_outcome_required",
            "live_model_accuracy": "unverified_history_does_not_establish_forward_skill",
        },
        "retrospective_only": True, "live_model_promoted": False,
        "model_training_from_supplements_enabled": False,
        "automatic_trading_enabled": False,
    }


async def _publish_all(root: Path, report_path: Path) -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.research.publish import _artifact, _drive, prepare, publish
    drive = _drive(Settings())
    try:
        transferred = []
        for source in sorted(HISTORY_SOURCES):
            for path in sorted((root / source).glob("*.manifest.json")):
                transferred.append(await publish(drive, prepare(path)))
        artifact = _artifact(report_path, 1)
        obj = await drive.ensure_research_output(
            artifact=artifact, batch_id="supplement-" + artifact.sha256[:24],
            kind="report",
        )
        return {
            "verified_archive_files": len(transferred),
            "report_drive_id": obj.file_id,
            "archive_readback_verified": True,
        }
    finally:
        await drive.aclose()


def main(argv: list[str] | None = None) -> int:
    from parlay_bot.research.batch import complete_months

    parser = argparse.ArgumentParser(description="RivaL additive verified supplementary research")
    parser.add_argument("--root", type=Path, default=Path("data/research-history"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/research-reports"))
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS), default=["BTC", "ETH", "SOL"])
    parser.add_argument("--max-new", type=int, default=100)
    parser.add_argument("--contract-manifest", type=Path,
                        help="Exact IDs/times; no inferred market identity or contract resolution")
    parser.add_argument("--publish", action="store_true", help="Use existing verified private Drive publisher")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        months = complete_months(now=datetime.now(UTC), count=args.months)
        jobs = supplemental_jobs(months, args.assets) + _load_contract_jobs(args.contract_manifest)
        if args.plan:
            print(json.dumps({"jobs": jobs, "requested": len(jobs)}, indent=2))
            return 0
        with httpx.Client() as client:
            result = collect_supplemental(client, root=args.root, jobs=jobs, max_new=args.max_new)
        report = _report_status(args.root, jobs, result)
        args.report_dir.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
        path = args.report_dir / ("supplemental-" + _sha(encoded)[:20] + ".json")
        if path.exists() and path.read_bytes() != encoded:
            raise HistoryImportError("conflicting supplemental report")
        path.write_bytes(encoded)
        if args.publish:
            report["drive"] = asyncio.run(_publish_all(args.root, path))
        print(json.dumps({"acquisition": result, "coverage": report["verified_coverage"],
                          "report": str(path), "drive": report.get("drive")}, sort_keys=True))
        return 1 if result["rejected"] else 0
    except (HistoryImportError, OSError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(1, f"Supplemental research blocked: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
