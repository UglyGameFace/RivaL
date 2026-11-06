"""Fetch Drive source-month manifests to cross-check candidate provenance.

A candidate claims its training months by hash. The claim is only checked
against the manifest actually stored in Drive for that (venue, contract,
quote, month): a name match alone proves nothing, and a fabricated hash
must not pass. Manifest bytes are SHA-checked against the Drive
``appProperties.rival_sha256`` before parsing, exactly like the byte audit.

Months with no resolvable manifest name, or with no manifest listed in
Drive, are simply absent from the result -- the registry records them as
``manifest_missing`` and holds the candidate. Any failed download or
conflicting duplicate raises, so the caller defers the import instead of
recording a false verification.
"""
from __future__ import annotations

import json
import re

from parlay_bot.research.candidate_registry import (
    CandidateValidationError,
    verified_candidate,
)
from parlay_bot.research.drive_source_census import list_names
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.history_byte_audit import _get_source

_MAX_MANIFEST_BYTES = 8192

# Binance auxiliary families by bar interval (minutes); 5m candles use the
# short ``<CONTRACT>-5m-<month>`` name instead. A weekly partition has no
# auxiliary family, so it stays unresolvable and unverified.
_BINANCE_AUX_FAMILY = {
    1: "candles_1m", 15: "candles_15m", 60: "candles_1h",
    240: "candles_4h", 1440: "candles_1d",
}


def manifest_prefix(*, venue: str, contract: str, quote: str,
                    interval_minutes: int, month: str) -> str | None:
    """Drive manifest name prefix for one normalized source month.

    Returns None when the month has no resolvable normalized-manifest name.
    """
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month or ""):
        return None
    if venue == "binance":
        if interval_minutes == 5:
            return f"{contract}-5m-{month}--"
        family = _BINANCE_AUX_FAMILY.get(interval_minutes)
        if family is None:
            return None
        return f"binance-aux-{contract.removesuffix('USDT')}-{family}-{month}--"
    if interval_minutes != 5:
        return None
    return f"{venue}-{contract}-{quote}-5m-{month}--"


async def _download_manifest(drive, *, name: str) -> dict:
    """One trusted Drive manifest by exact name, SHA-checked before parsing."""
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        f"and name = '{drive._escape_query(name)}'"
    )
    response = await drive._client.get(
        f"{drive.DRIVE_URL}/files", params={
            "q": query, "spaces": "drive", "pageSize": 20,
            "fields": "nextPageToken,files(id,name,size,parents,trashed,appProperties)",
        }, headers=await drive._headers(), follow_redirects=False,
    )
    if response.status_code != 200:
        raise HistoryImportError("remote source manifest lookup failed")
    payload = response.json()
    files = payload.get("files") if isinstance(payload, dict) else None
    if (not isinstance(files, list) or len(files) != 1
            or payload.get("nextPageToken")):
        raise HistoryImportError("missing, duplicated or unbounded manifest")
    entry = files[0]
    props = entry.get("appProperties") or {}
    sha = props.get("rival_sha256")
    if (entry.get("name") != name or entry.get("trashed")
            or folder not in entry.get("parents", [])
            or props.get("rival_kind") != "manifest"
            or not isinstance(sha, str)):
        raise HistoryImportError("manifest outside trusted warehouse")
    contents = await _get_source(
        drive, file_id=entry["id"],
        expected_sha=sha, size=int(entry["size"]), max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        info = json.loads(contents)
    except (ValueError, TypeError) as exc:
        raise HistoryImportError("remote source manifest malformed") from exc
    if not isinstance(info, dict):
        raise HistoryImportError("remote source manifest must be an object")
    return info


async def fetch_source_manifests(drive, *, scopes, names=None) -> dict:
    """Fetch Drive manifests for claimed candidate source months.

    ``scopes`` is an iterable of (venue, contract, quote, interval_minutes,
    months). Returns {(venue, contract, quote, month): parsed manifest}.
    """
    if names is None:
        names = await list_names(drive)
    wanted = {}
    for venue, contract, quote, interval_minutes, months in scopes:
        for month in months:
            prefix = manifest_prefix(
                venue=venue, contract=contract, quote=quote,
                interval_minutes=interval_minutes, month=month,
            )
            if prefix is None:
                continue
            wanted[(venue, contract, quote, month)] = prefix
    found = {}
    for key, prefix in wanted.items():
        pattern = re.compile(re.escape(prefix) + r"[0-9a-f]{16}\.manifest\.json")
        matches = [n for n in names if pattern.fullmatch(n)]
        if not matches:
            continue
        if len(matches) > 1:
            raise HistoryImportError(
                f"conflicting Drive manifests for {key[0]} {key[1]} {key[3]}"
            )
        found[key] = matches[0]
    results = {}
    for key, name in found.items():
        info = await _download_manifest(drive, name=name)
        venue, contract, _quote, _month = key
        if info.get("venue") != venue or info.get("contract") != contract:
            raise HistoryImportError("remote source manifest identity mismatch")
        results[key] = info
    return results


def _scopes_for_document(document: dict) -> list:
    """(venue, contract, quote, interval, months) for each candidate member."""
    if "candidate_models" not in document:
        spec = verified_candidate(json.dumps(document).encode())["model_spec"]
        identity = spec["identity"]
        return [(identity["venue"], identity["contract"], identity["quote"],
                 identity["interval_minutes"],
                 sorted(spec["source_sha256_by_month"]))]
    if (document.get("schema_version") != "rival-offline-candidate-registry-v1"
            or not isinstance(document.get("candidate_models"), list)):
        raise CandidateValidationError("invalid historical bundle")
    scopes = []
    for member in document["candidate_models"]:
        doc = {**member, "research_only": True, "live_model_promoted": False}
        spec = verified_candidate(json.dumps(doc).encode())["model_spec"]
        identity = spec["identity"]
        scopes.append((identity["venue"], identity["contract"],
                       identity["quote"], identity["interval_minutes"],
                       sorted(spec["source_sha256_by_month"])))
    return scopes


async def fetch_manifests_for_payload(drive, payload: bytes, *, names=None) -> dict:
    """Drive manifests for every source month a candidate document claims.

    Raises ValueError for an invalid document (same as import) and
    HistoryImportError for any Drive failure, so the caller can defer the
    import instead of admitting the candidate unchecked.
    """
    try:
        document = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise CandidateValidationError("invalid historical model document") from exc
    if not isinstance(document, dict):
        raise CandidateValidationError("invalid historical model document")
    scopes = _scopes_for_document(document)
    return await fetch_source_manifests(drive, scopes=scopes, names=names)
