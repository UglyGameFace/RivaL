"""Unattended, source-exact Bitget historical 5m futures to RivaL's private Drive.

The public API was sampled from a GitHub runner for 2021/2023/2025 BTC,
including 2025 market/mark/index/premium. This code never substitutes other
venues, fabricates holes, or promotes historical data into forward predictions.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import json
import re
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from parlay_bot.archive.drive import DriveArchiveError
from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, _drive

ASSETS = ("BTC", "ETH", "SOL", "XRP")
FAMILIES = ("market", "mark", "index", "premium")
ENDPOINT = "https://api.bitget.com/api/v3/market/history-candles"
METADATA = "https://api.bitget.com/api/v3/market/instruments"
STEP = 300_000
PAGE = 96  # Provider caps history at 100; a boundary row can be included.
MAX_BODY = 160_000
MAX_REPORT_ERRORS = 1200
MANIFEST_RE = re.compile(
    r"bitget-v3-(BTC|ETH|SOL|XRP)-(market|mark|index|premium)-"
    r"(20\d{2}-(?:0[1-9]|1[0-2]))--([0-9a-f]{16})\.manifest\.json"
)


def bounds(month: str) -> tuple[int, int]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise HistoryImportError("invalid Bitget source month") from exc
    if start.strftime("%Y-%m") != month or month < "2021-01":
        raise HistoryImportError("Bitget month outside authorized history window")
    end = (start.replace(year=start.year + 1, month=1)
           if start.month == 12 else start.replace(month=start.month + 1))
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def months(now: datetime, first: str) -> list[str]:
    if now.utcoffset() is None:
        raise ValueError("timezone-aware UTC clock required")
    bounds(first)
    y, m = map(int, first.split("-"))
    current = now.astimezone(UTC)
    begin = y * 12 + m - 1
    last_exclusive = current.year * 12 + current.month - 1
    if last_exclusive <= begin or last_exclusive - begin > 120:
        raise ValueError("invalid bounded Bitget history range")
    return [f"{year:04d}-{mo + 1:02d}" for year, mo in
            (divmod(i, 12) for i in range(begin, last_exclusive))]


def windows(month: str) -> list[tuple[int, int]]:
    start, end = bounds(month)
    return [(tick, min(end, tick + PAGE * STEP))
            for tick in range(start, end, PAGE * STEP)]


def number(value: object, *, allow_negative: bool = False,
           allow_zero: bool = True) -> Decimal:
    if not isinstance(value, str) or not 1 <= len(value) <= 70:
        raise HistoryImportError("invalid Bitget numeric field type")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise HistoryImportError("non-numeric Bitget candle field") from exc
    if (not result.is_finite() or (result < 0 and not allow_negative)
        or (result == 0 and not allow_zero)):
        raise HistoryImportError("unreasonable Bitget price or volume")
    return result


def validate_response(raw: bytes, *, start: int, end: int,
                      family: str) -> dict[int, list]:
    if not 0 < len(raw) <= MAX_BODY or family not in FAMILIES:
        raise HistoryImportError("Bitget source response exceeds safe limit")
    try:
        obj = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise HistoryImportError("Bitget returned invalid history JSON") from exc
    if (not isinstance(obj, dict) or obj.get("code") != "00000"
        or not isinstance(obj.get("data"), list)
        or len(obj["data"]) > 100):
        raise HistoryImportError("Bitget source rejected or oversized history page")
    seen: dict[int, list] = {}
    for row in obj["data"]:
        if not isinstance(row, list) or len(row) != 7:
            raise HistoryImportError("Bitget seven-field source schema incompatible")
        ts = row[0]
        if (not isinstance(ts, str) or not ts.isascii()
            or len(ts) != 13 or not ts.isdecimal()):
            raise HistoryImportError("Bitget timestamp invalid")
        opened = int(ts)
        if opened % STEP:
            raise HistoryImportError("Bitget source timestamp off 5m grid")
        if not start - STEP <= opened <= end:
            raise HistoryImportError("Bitget response crossed unrelated source window")
        prices = [number(x, allow_negative=(family == "premium"),
                         allow_zero=(family == "premium")) for x in row[1:5]]
        op, high, low, close = prices
        if low > min(op, close) or high < max(op, close) or high < low:
            raise HistoryImportError("Bitget source OHLC envelope invalid")
        number(row[5])
        number(row[6])
        if start <= opened < end:
            if opened in seen and seen[opened] != row:
                raise HistoryImportError("conflicting duplicate Bitget candle")
            seen[opened] = row
    return seen


class Pacer:
    """Keep well below Bitget's published per-IP limit."""

    def __init__(self, seconds: float = 0.4) -> None:
        if seconds < 0.25:
            raise ValueError("Bitget research max 4 history requests/second")
        self.seconds = seconds
        self.next_request = 0.0

    async def wait(self) -> None:
        gap = self.next_request - time.monotonic()
        if gap > 0:
            await asyncio.sleep(gap)
        self.next_request = time.monotonic() + self.seconds


async def request_page(client: httpx.AsyncClient, *, asset: str, family: str,
                       begin: int, end: int, pacer: Pacer) -> tuple[bytes, dict]:
    if asset not in ASSETS or family not in FAMILIES:
        raise HistoryImportError("Bitget historical request outside scope")
    params = {"category": "USDT-FUTURES", "symbol": asset + "USDT",
              "interval": "5m", "type": family, "limit": "100",
              "startTime": str(begin - 1), "endTime": str(end - 1)}
    for attempt in range(4):
        await pacer.wait()
        try:
            response = await client.get(ENDPOINT, params=params, follow_redirects=False)
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise HistoryImportError("Bitget request transport exhausted") from exc
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code == 200:
            raw = response.content
            return raw, validate_response(raw, start=begin, end=end, family=family)
        if response.status_code in (401, 403, 451):
            raise HistoryImportError(
                f"Bitget historical API restricted HTTP {response.status_code}"
            )
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
            raise HistoryImportError(
                f"Bitget history failed HTTP {response.status_code}"
            )
        await asyncio.sleep(min(30, 3 * 2 ** attempt))
    raise HistoryImportError("Bitget page retry budget exceeded")


async def verify_symbol(client: httpx.AsyncClient, *, asset: str,
                        pacer: Pacer) -> dict:
    if asset not in ASSETS:
        raise HistoryImportError("unapproved Bitget asset")
    await pacer.wait()
    response = await client.get(
        METADATA, params={"category": "USDT-FUTURES", "symbol": asset + "USDT"},
        follow_redirects=False,
    )
    if response.status_code != 200 or len(response.content) > MAX_BODY:
        raise HistoryImportError(
            f"Bitget contract metadata access denied HTTP {response.status_code}"
        )
    info = response.json()
    rows = info.get("data") if isinstance(info, dict) else None
    match = [item for item in rows if isinstance(item, dict)
             and item.get("symbol") == asset + "USDT"
             and item.get("category") == "USDT-FUTURES"
             and item.get("quoteCoin") == "USDT"
             and item.get("type") == "perpetual"] if isinstance(rows, list) else []
    if info.get("code") != "00000" or len(match) != 1:
        raise HistoryImportError("Bitget USDT perpetual identity not verified")
    return {"symbol": asset + "USDT", "category": "USDT-FUTURES",
            "metadata_sha256": hashlib.sha256(response.content).hexdigest(),
            "historical_listing_date_verified": False}


async def existing_manifests(drive, *, asset: str) -> set[tuple[str, str]]:
    """A Drive name is a resumability hint only, never proof of current bytes."""
    await drive._verify_folder_boundary()
    folder = drive.folders.manifests
    predicate = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        f"and name contains 'bitget-v3-{asset}-' "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    found: dict[tuple[str, str], str] = {}
    page = None
    for _ in range(10):
        params = {
            "q": predicate, "spaces": "drive", "pageSize": 1000,
            "fields": "nextPageToken,files(name,parents,trashed,appProperties)",
        }
        if page:
            params["pageToken"] = page
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params,
            headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("Bitget private Drive inventory unavailable")
        info = response.json()
        files = info.get("files") if isinstance(info, dict) else None
        if not isinstance(files, list) or len(files) > 1000:
            raise HistoryImportError("untrusted Bitget private Drive inventory")
        for item in files:
            if (not isinstance(item, dict) or item.get("trashed")
                or folder not in (item.get("parents") or [])):
                raise HistoryImportError("Bitget manifest outside private Drive boundary")
            props = item.get("appProperties") or {}
            if not isinstance(props, dict) or props.get("rival_kind") != "manifest":
                continue
            name = item.get("name")
            match = MANIFEST_RE.fullmatch(name) if isinstance(name, str) else None
            if not match or match.group(1) != asset:
                continue
            a, family, month, short = match.groups()
            sha = props.get("rival_sha256")
            if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or sha[:16] != short):
                raise HistoryImportError("Bitget remote manifest hash invalid")
            key = (family, month)
            if key in found and found[key] != sha:
                raise HistoryImportError("conflicting Bitget source month manifest")
            found[key] = sha
        page = info.get("nextPageToken")
        if page is None:
            return set(found)
        if not isinstance(page, str) or not page or len(page) > 1000:
            raise HistoryImportError("untrusted Bitget Drive pagination")
    raise HistoryImportError("Bitget Drive inventory exceeds ten pages")


async def collect_month(client: httpx.AsyncClient, *, asset: str, family: str,
                        month: str, root: Path, pacer: Pacer,
                        metadata: dict) -> tuple[Path, Path, dict]:
    """Retain source request/response bytes and certify exact 5m UTC grid."""
    steps = windows(month)
    root.mkdir(parents=True, exist_ok=True)
    raw_path = root / f"bitget-{asset}-{family}-{month}.responses.jsonl.gz"
    seen: set[int] = set()
    raw_sha = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with temporary.open("wb") as sink:
            with gzip.GzipFile(filename="", mode="wb", fileobj=sink, mtime=0) as stream:
                for begin, end in steps:
                    data, rows = await request_page(
                        client, asset=asset, family=family,
                        begin=begin, end=end, pacer=pacer,
                    )
                    for opened in rows:
                        if opened in seen:
                            raise HistoryImportError("duplicate cross-page Bitget candle")
                        seen.add(opened)
                    raw_sha.update(data)
                    entry = {
                        "request": {"endpoint": ENDPOINT, "category": "USDT-FUTURES",
                                    "symbol": asset + "USDT", "type": family,
                                    "interval": "5m", "startTime": begin - 1,
                                    "endTime": end - 1, "limit": 100},
                        "response_sha256": hashlib.sha256(data).hexdigest(),
                        "response_base64": base64.b64encode(data).decode("ascii"),
                    }
                    stream.write((json.dumps(entry, sort_keys=True) + "\n").encode())
        first, last = bounds(month)
        expected = (last - first) // STEP
        if len(seen) != expected or seen != set(range(first, last, STEP)):
            raise HistoryImportError(
                f"Bitget {asset}/{family}/{month} incomplete UTC grid "
                f"{len(seen)}/{expected}"
            )
        data_digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        name = f"bitget-v3-{asset}-{family}-{month}--{data_digest[:16]}"
        saved_raw = root / (name + ".responses.jsonl.gz")
        temporary.replace(saved_raw)
        manifest = root / (name + ".manifest.json")
        certificate = {
            "schema_version": "rival-bitget-v3-raw-api-month-v1",
            "venue": "bitget", "contract": asset + "USDT", "asset": asset,
            "category": "USDT-FUTURES", "market_type": "usdt_linear_perpetual",
            "quote": "USDT", "family": family, "interval_minutes": 5,
            "utc_month": month, "source_url": ENDPOINT,
            "source_transport": "bitget_public_v3_api",
            "raw_file": saved_raw.name,
            "raw_sha256": data_digest,
            "concatenated_original_response_sha256": raw_sha.hexdigest(),
            "source_metadata_sha256": metadata["metadata_sha256"],
            "historical_listing_date_verified": False,
            "provider_signed_checksum": False,
            "request_pages": len(steps), "candles": expected,
            "exact_utc_5m_grid_verified": True,
            "first_observed_at": datetime.now(UTC).isoformat(),
            "retrospective": True, "prospective_forecast": False,
            "model_training_eligible": False, "live_trading_authorized": False,
            "not_an_orderbook_or_funding_archive": True,
        }
        manifest.write_text(json.dumps(certificate, indent=2, sort_keys=True) + "\n")
        return saved_raw, manifest, certificate
    finally:
        temporary.unlink(missing_ok=True)


async def publish_month(drive, *, raw: Path, manifest: Path, count: int) -> dict:
    """Upload source bytes and manifest; read both back, verifying SHA256."""
    original = _artifact(raw, count)
    proof = _artifact(manifest, 1)
    batch = "bitgetv3-" + original.sha256[:24]
    saved = await drive.ensure_history_file(
        artifact=original, batch_id=batch, mime_type="application/gzip",
    )
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history, artifact=original,
    )
    stored = await drive.ensure_manifest_file(artifact=proof, batch_id=batch)
    await drive.verify_downloaded_artifact(
        file_id=stored.file_id, parent_id=drive.folders.manifests, artifact=proof,
    )
    return {"raw_sha256": original.sha256, "manifest_sha256": proof.sha256,
            "source_api_original_bytes_readback_verified": True,
            "models_approved": False}


async def run(*, drive, client: httpx.AsyncClient, root: Path,
              report_dir: Path, asset: str, first: str,
              max_requests: int, max_months: int, now: datetime | None = None,
              pacer: Pacer | None = None) -> dict:
    clock = now or datetime.now(UTC)
    all_months = months(clock, first)
    if asset not in ASSETS or not 1 <= max_requests <= 40000 or not 1 <= max_months <= 400:
        raise ValueError("invalid bounded Bitget overnight job")
    pacer = pacer or Pacer()
    existing = await existing_manifests(drive, asset=asset)
    metadata = await verify_symbol(client, asset=asset, pacer=pacer)
    # Prioritize the latest six full months for research, then deepest history.
    prioritized = all_months[-6:] + all_months[:-6]
    job = [(month, family) for month in prioritized for family in FAMILIES]
    report = {
        "schema_version": "rival-bitget-v3-overnight-progress-v1",
        "asset": asset, "created_at": datetime.now(UTC).isoformat(),
        "source": ENDPOINT, "venue": "bitget", "source_families": list(FAMILIES),
        "planned_source_months": len(job), "existing_manifest_names_only": 0,
        "source_months_published_and_readback": [], "unavailable": [],
        "api_requests_completed": 0, "request_budget": max_requests,
        "full_2021_present_coverage_proven": False, "live_models_approved": False,
    }
    root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    for month, family in job:
        if (family, month) in existing:
            report["existing_manifest_names_only"] += 1
            continue
        needed = len(windows(month))
        if (report["api_requests_completed"] + needed > max_requests
            or len(report["source_months_published_and_readback"]) >= max_months):
            report["budget_exhausted_before_next_month"] = True
            break
        # Budget is reserved for a whole month, including at most three retry
        # attempts per page; available counts never certify missing rows.
        try:
            raw, manifest, certificate = await collect_month(
                client, asset=asset, family=family, month=month,
                root=root, pacer=pacer, metadata=metadata,
            )
            proof = await publish_month(drive, raw=raw, manifest=manifest,
                                        count=certificate["request_pages"])
            report["source_months_published_and_readback"].append(
                {"month": month, "family": family, "candles": certificate["candles"],
                 **proof}
            )
        except (HistoryImportError, DriveArchiveError, httpx.HTTPError,
                RuntimeError, OSError, ValueError) as exc:
            if len(report["unavailable"]) < MAX_REPORT_ERRORS:
                report["unavailable"].append(
                    {"month": month, "family": family, "reason": str(exc)[:180]}
                )
            if any(term in str(exc) for term in
                   ("restricted HTTP", "HTTP 429", "HTTP 503", "HTTP 504")):
                report["source_access_blocked_or_throttled"] = True
                break
        finally:
            report["api_requests_completed"] += needed
    path = report_dir / f"bitget-{asset}-overnight-progress.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if drive.folders.reports:
        artifact = _artifact(path, 1)
        stored = await drive.ensure_research_output(
            artifact=artifact, batch_id="bitget-report-" + artifact.sha256[:24],
            kind="report",
        )
        await drive.verify_downloaded_artifact(
            file_id=stored.file_id, parent_id=drive.folders.reports,
            artifact=artifact,
        )
        report["report_drive_sha256_readback_verified"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bitget USDT-perp 2021+ source archive")
    parser.add_argument("--asset", required=True, choices=ASSETS)
    parser.add_argument("--first", default="2021-01")
    parser.add_argument("--max-requests", type=int, default=24000)
    parser.add_argument("--max-months", type=int, default=320)
    parser.add_argument("--root", type=Path, default=Path("data/bitget-v3-archive"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)

    async def worker() -> dict:
        drive = _drive(Settings())
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(40), follow_redirects=False,
            ) as client:
                return await run(
                    drive=drive, client=client, asset=args.asset,
                    first=args.first, max_requests=args.max_requests,
                    max_months=args.max_months, root=args.root,
                    report_dir=args.report_dir,
                )
        finally:
            await drive.aclose()

    try:
        result = asyncio.run(worker())
    except (HistoryImportError, DriveArchiveError, httpx.HTTPError,
            RuntimeError, ValueError, OSError) as exc:
        parser.exit(2, f"Bitget overnight acquisition blocked: {exc}\n")
    print(json.dumps({
        "asset": result["asset"], "new_months": len(
            result["source_months_published_and_readback"]
        ), "remote_manifest_hints_not_reverified":
        result["existing_manifest_names_only"],
        "failed_or_missing": len(result["unavailable"]),
        "requests_budgeted": result["api_requests_completed"],
        "readback_receipts": len(result["source_months_published_and_readback"]),
        "complete_2021_present": False,
        "source_access_blocked": result.get("source_access_blocked_or_throttled", False),
    }, sort_keys=True))
    return 2 if result.get("source_access_blocked_or_throttled") or (
        result["unavailable"] and not result["source_months_published_and_readback"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
