"""OKX official one-minute ZIP archives -> exact source-native UTC five-minute months.

Files from OKX's public historical catalog are cut on UTC+08 boundaries, so
one complete UTC month can require two adjacent monthly ZIPs. We never infer
missing minutes, borrow another venue's observations, or label a self-computed
SHA-256 as a provider-signed checksum. Existing verified five-minute REST
archives remain immutable and can still be used when file history is absent.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path

import httpx

from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.official_okx_bootstrap import (
    ASSETS,
    STEP,
    inventory_extra_modules,
    month_bounds,
    number,
)
from parlay_bot.research.okx_advanced_archive import archive_url
from parlay_bot.research.source_native import SOURCE

INTEGRITY = "okx-v5-official-1m-zip-derived-5m-v1"
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ROWS = 50_000
MINUTE_MS = 60_000
FIVE_MS = 300_000

# The archived CSV schema must identify its columns, or be a strict
# eight/nine-column native OHLCV tuple. Never guess which volume is which.
CANONICAL = ("ts", "o", "h", "l", "c", "vol", "volccy", "volccyquote")
ALIASES = {
    "timestamp": "ts", "open_time": "ts", "time": "ts",
    "open": "o", "high": "h", "low": "l", "close": "c",
    "volume": "vol", "volume_contracts": "vol",
    "vol_ccy": "volccy", "volume_base": "volccy",
    "vol_ccy_quote": "volccyquote", "volume_quote": "volccyquote",
}


def _columns(row: list[str]) -> tuple[int, ...] | None:
    normalized = [ALIASES.get(col.strip().lower(), col.strip().lower())
                  for col in row]
    if all(key in normalized for key in CANONICAL):
        if len(set(normalized)) != len(normalized):
            raise HistoryImportError("duplicate OKX archive headers")
        return tuple(normalized.index(key) for key in CANONICAL)
    return None


def _read_csv(data: bytes, *, target_start_ms: int,
              target_end_ms: int, asset: str) -> dict[int, tuple]:
    if len(data) > MAX_EXPANDED:
        raise HistoryImportError("expanded one-minute archive exceeds cap")
    try:
        body = data.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(body))
        first = next(reader)
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise HistoryImportError("invalid OKX one-minute CSV") from exc
    columns = _columns(first)
    if columns is None:
        # Numeric native tuples may have no header; cannot accept arbitrary
        # human-readable data with unknown units or ambiguous timestamps.
        if len(first) not in (8, 9):
            raise HistoryImportError("unsupported OKX minute CSV schema")
        columns = tuple(range(8))
        confirm_index = 8 if len(first) == 9 else None
        rows = chain((first,), reader)
    else:
        names = [name.strip().lower() for name in first]
        confirm_index = names.index("confirm") if "confirm" in names else None
        rows = reader
    collected = {}
    try:
        for index, row in enumerate(rows):
            if index >= MAX_ROWS:
                raise HistoryImportError("oversized OKX minute archive row count")
            if len(row) <= max(columns):
                raise HistoryImportError("incomplete OKX minute CSV row")
            if confirm_index is not None and (
                len(row) <= confirm_index or row[confirm_index].strip() != "1"
            ):
                raise HistoryImportError("unfinished OKX archived minute")
            values = [row[i].strip() for i in columns]
            ts = values[0]
            if len(ts) != 13 or not ts.isascii() or not ts.isdecimal():
                raise HistoryImportError("ambiguous OKX archived minute timestamp")
            opened = int(ts)
            if opened % MINUTE_MS:
                raise HistoryImportError("off-grid OKX archived minute")
            # One catalog month can straddle the adjacent UTC calendar month.
            if not target_start_ms <= opened < target_end_ms:
                continue
            if opened in collected:
                raise HistoryImportError("duplicate OKX archived minute")
            op, hi, lo, cl = (number(value, positive=True)
                              for value in values[1:5])
            contracts, base, quote = (number(value) for value in values[5:8])
            if lo > min(op, cl) or hi < max(op, cl) or hi < lo:
                raise HistoryImportError("invalid OKX minute OHLC envelope")
            collected[opened] = (op, hi, lo, cl, contracts, base, quote)
    except (csv.Error, UnicodeError) as exc:
        raise HistoryImportError("malformed OKX minute CSV") from exc
    return collected


async def _download_archive(client: httpx.AsyncClient, *, url: str) -> bytes:
    # Bounded, streamed, redirect-free read; the calling client's shared
    # public-IP pace is applied to stream acquisition too.
    total = 0
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as tmp:
        try:
            async with client.stream("GET", url, timeout=120,
                                     follow_redirects=False) as response:
                if response.status_code != 200:
                    if response.status_code == 429 and hasattr(client, "pacer"):
                        await client.pacer.cooldown(30)
                    raise HistoryImportError(
                        f"official OKX archive unavailable HTTP {response.status_code}"
                    )
                length = response.headers.get("content-length")
                if length and (not length.isdecimal() or int(length) > MAX_DOWNLOAD):
                    raise HistoryImportError("oversized OKX archive Content-Length")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD:
                        raise HistoryImportError("OKX archive exceeded transfer cap")
                    tmp.write(chunk)
        except httpx.HTTPError as exc:
            raise HistoryImportError("official OKX archive transfer failed") from exc
        if not total:
            raise HistoryImportError("empty official OKX minute archive")
        tmp.seek(0)
        return tmp.read()


def archive_cache_path(*, root: Path, asset: str, month: str,
                       url: str) -> Path:
    """Exact immutable archive cache locator shared with Drive preservation."""
    if asset not in ASSETS:
        raise HistoryImportError("unapproved OKX archive cache asset")
    month_bounds(month)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return root / ".okx-one-minute-zip-cache" / f"{asset}-{month}-{key}.zip"


async def _cached_archive(client: httpx.AsyncClient, *, root: Path,
                          asset: str, month: str, url: str) -> bytes:
    """Reuse adjacent-month official bytes only after checking their saved hash."""
    path = archive_cache_path(root=root, asset=asset, month=month, url=url)
    cache = path.parent
    checksum_path = cache / f"{asset}-{month}-{key}.sha256"
    if path.exists():
        try:
            payload = path.read_bytes()
            expected = checksum_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise HistoryImportError("cached OKX archive checksum missing") from exc
        if (not 0 < len(payload) <= MAX_DOWNLOAD or len(expected) != 64
            or hashlib.sha256(payload).hexdigest() != expected):
            raise HistoryImportError("cached OKX archive content changed")
        return payload
    if checksum_path.exists():
        raise HistoryImportError("orphaned OKX archive checksum")
    payload = await _download_archive(client, url=url)
    _unzip(payload)  # Never cache a corrupt/unsafe ZIP.
    cache.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as f:
        temp = Path(f.name)
        f.write(payload)
    try:
        # Never rewrite an existing source archive silently.
        if path.exists():
            raise HistoryImportError("concurrent OKX archive cache conflict")
        checksum_path.write_text(digest + "\n", encoding="ascii")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return payload


def _unzip(payload: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise HistoryImportError("ambiguous OKX one-minute ZIP members")
            member = members[0]
            if (member.is_dir() or Path(member.filename).name != member.filename
                or not member.filename.endswith(".csv") or
                member.file_size > MAX_EXPANDED or
                member.flag_bits & 0x1):
                raise HistoryImportError("unsafe OKX minute archive member")
            with archive.open(member) as handle:
                data = handle.read(MAX_EXPANDED + 1)
            if len(data) != member.file_size or len(data) > MAX_EXPANDED:
                raise HistoryImportError("OKX archive expansion failed byte check")
            return data
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
        raise HistoryImportError("corrupt OKX one-minute ZIP") from exc


def _next_month(month: str) -> str:
    _, end = month_bounds(month)
    return end.strftime("%Y-%m")


async def download_file_month(
    client: httpx.AsyncClient, *, asset: str, month: str, root: Path,
    now: datetime | None = None,
):
    """Import only 100%-verified native 1m -> 5m files; no API fallback here."""
    if asset not in ASSETS:
        raise HistoryImportError("unsupported OKX bulk archive asset")
    start, end = month_bounds(month)
    observed = now or datetime.now(UTC)
    if observed.utcoffset() is None or observed.astimezone(UTC) < end:
        raise HistoryImportError("unclosed UTC month cannot be archived")
    begin_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    seen: dict[int, tuple] = {}
    source_files = []
    digests = hashlib.sha256()
    # OKX monthly archive files are cut on UTC+08 midnights, unlike
    # RivaL's UTC calendar acceptance. The following month provides the
    # last eight UTC hours of the requested month.
    for archive_month in (month, _next_month(month)):
        catalog = await inventory_extra_modules(
            client, asset=asset, month=archive_month, module_ids=(2,),
        )
        item = catalog["modules"]["one_minute_candles"]
        files = item["files"]
        if item["status"] != "cataloged_not_downloaded" or len(files) != 1:
            raise HistoryImportError("complete official OKX 1m ZIP pair unavailable")
        url = archive_url(asset=asset, month=archive_month, record=files[0])
        if not url.endswith(".zip"):
            raise HistoryImportError("OKX minute catalog is not a supported ZIP")
        payload = await _cached_archive(
            client, root=root, asset=asset, month=archive_month, url=url,
        )
        raw_hash = hashlib.sha256(payload).hexdigest()
        part = _read_csv(_unzip(payload), target_start_ms=begin_ms,
                         target_end_ms=end_ms, asset=asset)
        if set(part) & set(seen):
            raise HistoryImportError("overlapping OKX one-minute archive records")
        seen.update(part)
        source_files.append({"url": url, "month": archive_month,
                             "sha256": raw_hash, "records_in_target": len(part)})
        digests.update(bytes.fromhex(raw_hash))

    minutes = range(begin_ms, end_ms, MINUTE_MS)
    if len(seen) != len(minutes) or set(seen) != set(minutes):
        raise HistoryImportError("incomplete official OKX 1m source calendar")
    records = []
    for tick in range(begin_ms, end_ms, FIVE_MS):
        five = [seen[tick + n * MINUTE_MS] for n in range(5)]
        dt = datetime.fromtimestamp(tick / 1000, UTC)
        op, hi, lo, cl = (
            five[0][0], max(item[1] for item in five),
            min(item[2] for item in five), five[-1][3],
        )
        sums = [math.fsum(item[i] for item in five) for i in (4, 5, 6)]
        if not all(math.isfinite(value) for value in sums):
            raise HistoryImportError("unrepresentable OKX 5m volume aggregates")
        records.append({
            "venue": "okx", "contract": f"{asset}-USDT-SWAP",
            "quote": "USDT", "interval_minutes": 5,
            "opened_at": dt.isoformat(),
            "closed_at": (dt + STEP - timedelta(milliseconds=1)).isoformat(),
            "first_observed_at": observed.astimezone(UTC).isoformat(),
            "open": op, "high": hi, "low": lo, "close": cl,
            "volume_contracts": sums[0], "volume_base": sums[1],
            "volume_quote": sums[2], "trades": -1,
        })
    return _persist(
        source=SOURCE, key=f"okx-{asset}-USDT-SWAP-USDT-5m-{month}",
        records=records, raw_sha=digests.hexdigest(),
        url=source_files[0]["url"], out_dir=root,
        extra={
            "venue": "okx", "contract": f"{asset}-USDT-SWAP",
            "quote": "USDT", "interval_minutes": 5,
            "checksum_verified": True,
            "provider_signed_checksum": False,
            "source_integrity": INTEGRITY,
            "source_transport": "official_public_https",
            "source_archive_type": "one_minute_monthly_zip",
            "source_archive_files": source_files,
            "source_minutes_verified": len(seen),
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "original_observation_time_known": False,
        },
    )
