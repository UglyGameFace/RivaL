"""Warehouse-first source files for model training (private Google Drive).

Every official Binance file training needs is read from the RivaL Drive
warehouse when present. A file not yet in the warehouse is downloaded from
the official source once, verified against Binance's published SHA256
checksum when Binance publishes one, and saved into the warehouse, so the warehouse converges to the
complete training set. Training never depends on an unverified byte.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import zipfile
from datetime import date
from pathlib import Path

_LOG = logging.getLogger(__name__)
OFFICIAL = "https://data.binance.vision/data/futures/um/"
PREFIX = "rival-src__"
# Names already used by the existing warehouse collectors.
_KLINES = re.compile(r"monthly/klines/(\w+USDT)/5m/(\1-5m-20\d\d-\d\d\.zip)")
_AUX = re.compile(r"monthly/(premiumIndexKlines|fundingRate)/([A-Z]+)USDT/(?:5m/)?"
                  r"\2USDT-(?:5m|fundingRate)-(20\d\d-\d\d)\.zip")
_AUX_FAMILY = {"premiumIndexKlines": "premium", "fundingRate": "funding"}


def warehouse_names(url: str) -> tuple[str, ...]:
    """Acceptable warehouse names for an official URL, preferred first."""
    if not url.startswith(OFFICIAL):
        raise ValueError("not an official Binance futures data URL")
    path = url[len(OFFICIAL):]
    if not re.fullmatch(r"[A-Za-z0-9_./-]{10,200}", path) or ".." in path:
        raise ValueError("untrusted source path")
    names = [PREFIX + path.replace("/", "__")]
    if match := _KLINES.fullmatch(path):
        names.insert(0, match.group(2))
    elif match := _AUX.fullmatch(path):
        names.insert(0, f"binance-aux-{match.group(2)}-{_AUX_FAMILY[match.group(1)]}-"
                        f"{match.group(3)}--")  # followed by a digest prefix
    return tuple(names)


class TrainingWarehouse:
    def __init__(self, drive, cache: Path, *, max_uploads: int = 1500) -> None:
        self.drive = drive
        self.cache = cache
        self.max_uploads = max_uploads
        self.index: dict[str, tuple[str, str, int]] = {}
        self.stats = {"indexed_files": 0, "from_warehouse": 0, "already_cached": 0, "official_saved": 0, "official_not_saved": 0,
                      "missing_at_source": 0, "save_failed": 0, "cache_stale": 0, "warehouse_fallback": 0}
        self._upload_lock = asyncio.Semaphore(4)
        self.record_provenance: dict[str, list[dict]] = {}

    async def load_index(self) -> None:
        """One bounded listing per naming family in the warehouse history folder."""
        drive = self.drive
        await drive._verify_folder_boundary()
        folder = drive.folders.history
        # Drive's "name contains" matches from the START of words, so the
        # klines originals ("BTCUSDT-5m-2024-03.zip") need one prefix per coin.
        tokens = [f"{coin}USDT-5m-" for coin in ("BTC", "ETH", "SOL", "XRP")]
        for token in (*tokens, "binance-aux-", PREFIX):
            page = None
            for _ in range(200):
                params = {
                    "q": (f"'{drive._escape_query(folder)}' in parents and trashed = false "
                          f"and name contains '{token}'"),
                    "spaces": "drive", "pageSize": 1000,
                    "fields": "nextPageToken,files(id,name,size,md5Checksum,parents)",
                }
                if page:
                    params["pageToken"] = page
                response = await drive._client.get(f"{drive.DRIVE_URL}/files", params=params,
                                                   headers=await drive._headers())
                response.raise_for_status()
                doc = response.json()
                for row in doc.get("files") or ():
                    if folder in (row.get("parents") or ()) and row.get("md5Checksum"):
                        self.index[str(row["name"])] = (
                            str(row["id"]), str(row["md5Checksum"]), int(row.get("size") or 0))
                page = doc.get("nextPageToken")
                if not page:
                    break
        self.stats["indexed_files"] = len(self.index)
        _LOG.info("warehouse index: %d files", len(self.index))

    def _lookup(self, url: str):
        """(zip entry, checksum entry or None) for the first stored name."""
        for name in warehouse_names(url):
            if name.endswith("--"):  # digest-suffixed auxiliary original
                hits = sorted(k for k in self.index
                              if k.startswith(name) and k.endswith(".zip"))
                if hits:
                    return self.index[hits[0]], self.index.get(hits[0] + ".CHECKSUM")
            elif name in self.index:
                return self.index[name], self.index.get(name + ".CHECKSUM")
        return None

    async def _download(self, file_id: str, md5: str) -> bytes:
        drive = self.drive
        response = await drive._client.get(f"{drive.DRIVE_URL}/files/{file_id}",
                                           params={"alt": "media"},
                                           headers=await drive._headers())
        response.raise_for_status()
        raw = response.content
        if hashlib.md5(raw, usedforsecurity=False).hexdigest() != md5:
            raise ValueError("warehouse bytes failed integrity check")
        return raw

    async def _save(self, url: str, raw: bytes) -> None:
        from parlay_bot.research.publish import _artifact

        name = warehouse_names(url)[-1]
        if self.stats["official_saved"] >= self.max_uploads:
            self.stats["official_not_saved"] += 1
            return
        self.stats["official_saved"] += 1  # reserve before awaiting
        path = self.cache / "upload" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        try:
            async with self._upload_lock:
                await self.drive.ensure_history_file(
                    artifact=_artifact(path, 1),
                    batch_id="src-" + hashlib.sha256(name.encode()).hexdigest()[:40],
                    mime_type="application/zip",
                )
        except Exception:  # noqa: BLE001 - training continues on verified bytes
            self.stats["official_saved"] -= 1
            self.stats["save_failed"] += 1
            _LOG.warning("could not save %s to the warehouse", name)
        finally:
            path.unlink(missing_ok=True)

    def _is_stale_cache(self, url: str, raw: bytes) -> bool:
        """A local cache hit is trusted only when its bytes check out: the
        warehouse index's MD5 when the file is warehoused, else a valid zip
        (every file fetched here is one). Anything else is a stale/corrupt
        cache, never silently trained on."""
        hit = self._lookup(url)
        if hit is not None:
            (_file_id, md5, _size), _checksum = hit
            return hashlib.md5(raw, usedforsecurity=False).hexdigest() != md5
        return not zipfile.is_zipfile(io.BytesIO(raw))

    def fetcher(self, official_fetch):
        """A drop-in for feature_signal_experiment.fetch (warehouse first)."""
        async def fetch(client, url, cache, sem):
            local = cache / hashlib.sha1(url.encode()).hexdigest()
            if local.exists():
                raw = local.read_bytes() or None
                if raw is None:
                    self.stats["already_cached"] += 1
                    return None  # an empty file is a 404 cached at download time
                if self._is_stale_cache(url, raw):
                    self.stats["cache_stale"] += 1
                    _LOG.warning("stale local cache for %s; re-fetching instead "
                                 "of training on it", url)
                    local.unlink(missing_ok=True)
                else:
                    self.stats["already_cached"] += 1
                    if self._lookup(url) is None:
                        await self._save(url, raw)  # cached official bytes -> warehouse
                    return raw
            hit = self._lookup(url)
            if hit is not None:
                try:
                    (file_id, md5, _), checksum = hit
                    async with sem:
                        raw = await self._download(file_id, md5)
                        if checksum is not None:
                            # Binance's own published SHA256 for this file.
                            expected = (await self._download(checksum[0], checksum[1])
                                        ).decode().split()[0].lower()
                            if hashlib.sha256(raw).hexdigest() != expected:
                                raise ValueError("warehouse file differs from Binance's")
                    local.write_bytes(raw)
                    self.stats["from_warehouse"] += 1
                    return raw
                except Exception:  # noqa: BLE001 - fall back to the official original
                    self.stats["warehouse_fallback"] += 1
                    _LOG.warning("warehouse read failed for %s; using official file", url)
            raw = await official_fetch(client, url, cache, sem)  # checksum-verified
            if raw is None:
                self.stats["missing_at_source"] += 1
                return None
            await self._save(url, raw)
            return raw
        return fetch

    async def records(self, table: str, *, max_files: int = 400,
                      first_day: date | None = None, last_day: date | None = None,
                      require_sha256: bool = False) -> list[bytes]:
        """RivaL's own nightly backups of one live table (one file per UTC day).

        Written by perpetuals.records_backup as
        rival-records__<table>__<YYYY-MM-DD>.ndjson.gz in the history folder.
        A saved day is never rewritten, so a local copy stays valid.
        """
        if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", table)
                or not 1 <= max_files <= 400
                or first_day is not None and last_day is not None and first_day > last_day):
            raise ValueError("invalid records restoration scope")
        drive = self.drive
        await drive._verify_folder_boundary()
        folder = drive.folders.history
        prefix = f"rival-records__{table}__"
        found: dict[str, tuple[str, str, str | None, int | None]] = {}
        page = None
        for _ in range(50):
            params = {
                "q": (f"'{drive._escape_query(folder)}' in parents and trashed = false "
                      f"and name contains '{drive._escape_query(prefix)}'"),
                "spaces": "drive", "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,size,md5Checksum,parents,appProperties)",
            }
            if page:
                params["pageToken"] = page
            response = await drive._client.get(f"{drive.DRIVE_URL}/files", params=params,
                                               headers=await drive._headers())
            response.raise_for_status()
            doc = response.json()
            if (not isinstance(doc, dict) or not isinstance(doc.get("files"), list)
                    or len(doc["files"]) > 1000):
                raise ValueError("malformed records inventory page")
            for row in doc.get("files") or ():
                name = str(row.get("name") or "")
                if (name.startswith(prefix) and name.endswith(".ndjson.gz")
                        and folder in (row.get("parents") or ())):
                    day = date.fromisoformat(name[len(prefix):-len(".ndjson.gz")])
                    if require_sha256 and name != prefix + day.isoformat() + ".ndjson.gz":
                        raise ValueError("noncanonical records original day")
                    if (first_day is not None and day < first_day
                            or last_day is not None and day > last_day):
                        continue
                    props = row.get("appProperties") or {}
                    md5 = row.get("md5Checksum")
                    if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-f]{32}", md5):
                        if require_sha256:
                            raise ValueError("records original lacks verified integrity metadata")
                        continue
                    sha = props.get("rival_sha256")
                    size = int(row["size"]) if row.get("size") is not None else None
                    if require_sha256 and (
                        props.get("rival_kind") != "history"
                        or not isinstance(row.get("id"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", row["id"])
                        or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                        or size is None or not 0 < size <= 64 * 1024 * 1024
                    ):
                        raise ValueError("records original lacks verified SHA-256/size metadata")
                    entry = (str(row["id"]), md5, sha, size)
                    if name in found and found[name] != entry:
                        raise ValueError("conflicting records originals for the same day")
                    found[name] = entry
            page = doc.get("nextPageToken")
            if not page:
                break
            if not isinstance(page, str) or len(page) > 1000:
                raise ValueError("invalid records inventory cursor")
        else:
            raise ValueError("records inventory exceeded bounded pages")
        out = []
        local_dir = self.cache / "records"
        local_dir.mkdir(parents=True, exist_ok=True)
        # The cap silently dropped every day older than the newest
        # ``max_files``. State it: which days were found, how many the cap
        # dropped, and the dropped range — a model trained on this list must
        # be able to say how much history it never saw.
        all_days = sorted(found)
        dropped = all_days[:-max_files] if len(all_days) > max_files else []
        kept = all_days[-max_files:]
        if require_sha256 and dropped:
            raise ValueError("records scope exceeds the restoration file budget")

        def _day_of(name: str) -> str:
            return name[len(prefix):-len(".ndjson.gz")]

        self.stats[f"records_{table}_coverage"] = {
            "cap_days": max_files,
            "days_found": len(all_days),
            "days_returned": len(kept),
            "days_dropped_by_cap": len(dropped),
            "dropped_first_day": _day_of(dropped[0]) if dropped else None,
            "dropped_last_day": _day_of(dropped[-1]) if dropped else None,
        }
        if dropped:
            _LOG.warning("records %s: %d of %d days dropped by the %d-day cap "
                         "(%s..%s)", table, len(dropped), len(all_days), max_files,
                         _day_of(dropped[0]), _day_of(dropped[-1]))
        receipts = []
        total = 0
        for name in kept:
            file_id, md5, sha, size = found[name]
            total += size or 0
            if require_sha256 and total > 512 * 1024 * 1024:
                raise ValueError("records scope exceeds the restoration byte budget")
            local = local_dir / name
            raw = local.read_bytes() if local.exists() else b""
            if hashlib.md5(raw, usedforsecurity=False).hexdigest() != md5:
                raw = await self._download(file_id, md5)  # md5-verified
                local.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            if require_sha256 and (len(raw) != size or digest != sha):
                raise ValueError("records original SHA-256/size mismatch")
            receipts.append({"file": name, "file_id": file_id,
                             "day": _day_of(name), "md5": md5,
                             "sha256": digest, "size_bytes": len(raw),
                             "drive_sha256_verified": require_sha256})
            out.append(raw)
        self.record_provenance[table] = receipts
        self.stats[f"records_{table}"] = len(out)
        return out
