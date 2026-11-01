"""Scheduled official Gate USDT-perp bulk-file research into RivaL's existing Drive.

Source-exact, bounded, idempotent, public monthly gzip archives. The raw
provider files are the evidence. Neither collecting them nor uploading them
retroactively approves native ML, future performance or real executions.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.archive.drive import DriveArchiveError
from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_bulk_archive import ASSETS, TYPES, download
from parlay_bot.research.publish import _artifact, _drive

FIRST = "2023-01"  # Gate explicitly advertises candle/trade downloads since Jan 2023.
DEFAULT_KINDS = (
    "candlesticks_5m", "mark_prices", "funding_applies",
    "funding_updates", "trades",
)
MAX_PAGES = 20
MAX_ATTEMPTS = 48
MAX_NEW = 32
EXACT_NAME = re.compile(
    r"gatebulk-futures_usdt-(BTC|ETH|SOL|XRP)_USDT-"
    r"(candlesticks_1m|candlesticks_5m|candlesticks_1h|mark_prices|"
    r"funding_applies|funding_updates|trades)-"
    r"(20\d{2}-(?:0[1-9]|1[0-2]))--([0-9a-f]{16})\.manifest\.json"
)


def complete_months(now: datetime) -> list[str]:
    if now.utcoffset() is None:
        raise ValueError("Gate history requires a timezone-aware UTC clock")
    clock = now.astimezone(UTC)
    stop = clock.year * 12 + clock.month - 1
    start = 2023 * 12
    return [
        f"{year:04d}-{month_zero + 1:02d}"
        for year, month_zero in (divmod(idx, 12) for idx in range(start, stop))
    ]


def stem(*, asset: str, kind: str, month: str, sha: str) -> str:
    if (asset not in ASSETS or kind not in TYPES or len(sha) != 64
        or not re.fullmatch("[0-9a-f]{64}", sha)
        or not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month)):
        raise HistoryImportError("untrusted Gate file identity")
    return f"gatebulk-futures_usdt-{asset}_USDT-{kind}-{month}--{sha[:16]}"


async def listed_archives(drive) -> dict[tuple[str, str, str], str]:
    """Bounded restart hint from source-specific remote manifest filenames.

    This does not pretend skipped manifests have been re-downloaded or
    re-verified by today's run. New files always use full SHA readback.
    """
    await drive._verify_folder_boundary()
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        "and name contains 'gatebulk-futures_usdt-' "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    page_token = None
    found: dict[tuple[str, str, str], str] = {}
    for _ in range(MAX_PAGES):
        params = {
            "q": query, "spaces": "drive", "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,parents,appProperties,trashed)",
        }
        if page_token:
            params["pageToken"] = page_token
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params,
            headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError(
                f"Gate private Drive manifest inventory failed HTTP {response.status_code}"
            )
        result = response.json()
        files = result.get("files") if isinstance(result, dict) else None
        if not isinstance(files, list) or len(files) > 1000:
            raise HistoryImportError("invalid Gate manifest listing")
        for entry in files:
            if (not isinstance(entry, dict) or entry.get("trashed")
                or folder not in (entry.get("parents") or [])):
                raise HistoryImportError("Gate Drive manifest outside trusted boundary")
            props = entry.get("appProperties") or {}
            if not isinstance(props, dict) or props.get("rival_kind") != "manifest":
                raise HistoryImportError("Gate Drive manifest missing source ownership")
            name = entry.get("name")
            match = EXACT_NAME.fullmatch(name) if isinstance(name, str) else None
            if not match:
                # Non-matching metadata is not proof of an eligible archive.
                continue
            asset, kind, month, short_sha = match.groups()
            key = (asset, kind, month)
            if key in found and found[key] != short_sha:
                raise HistoryImportError("conflicting Gate monthly source manifests")
            found[key] = short_sha
        token = result.get("nextPageToken")
        if token is None:
            return found
        if (not isinstance(token, str) or not 1 <= len(token) <= 1000
            or not token.isprintable()):
            raise HistoryImportError("malformed Gate Drive pagination cursor")
        page_token = token
    raise HistoryImportError("Gate private manifest listing exceeds bounded page budget")


async def listed_native_months(drive) -> set[tuple[str, str]]:
    """Resume hint: normalized Gate native manifests, never a proof of quality."""
    await drive._verify_folder_boundary()
    folder = drive.folders.manifests
    query = (
        f"'{drive._escape_query(folder)}' in parents and trashed = false "
        "and name contains 'gate-' "
        "and appProperties has { key='rival_kind' and value='manifest' }"
    )
    found: set[tuple[str, str]] = set()
    page_token = None
    matcher = re.compile(
        r"gate-(BTC|ETH|SOL|XRP)_USDT-USDT-5m-"
        r"(20\d{2}-(?:0[1-9]|1[0-2]))--[0-9a-f]{16}\.manifest\.json"
    )
    for _ in range(MAX_PAGES):
        params = {"q": query, "spaces": "drive", "pageSize": 1000,
                  "fields": "nextPageToken,files(name,parents,appProperties,trashed)"}
        if page_token:
            params["pageToken"] = page_token
        response = await drive._client.get(
            f"{drive.DRIVE_URL}/files", params=params,
            headers=await drive._headers(),
        )
        if response.status_code != 200:
            raise HistoryImportError("Gate native month inventory unavailable")
        data = response.json()
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list) or len(files) > 1000:
            raise HistoryImportError("invalid Gate native month inventory")
        for item in files:
            if (not isinstance(item, dict) or item.get("trashed")
                or folder not in (item.get("parents") or [])
                or (item.get("appProperties") or {}).get("rival_kind") != "manifest"):
                raise HistoryImportError("untrusted Gate native research listing")
            match = matcher.fullmatch(item.get("name", ""))
            if match:
                key = (match[1], match[2])
                if key in found:
                    raise HistoryImportError("duplicate Gate normalized native month")
                found.add(key)
        page_token = data.get("nextPageToken")
        if page_token is None:
            return found
        if not isinstance(page_token, str) or not page_token:
            raise HistoryImportError("malformed Gate native month page token")
    raise HistoryImportError("Gate native month inventory exceeds bounded pages")


def prioritize_backfill(
    jobs: list[tuple[str, str, str]], *,
    existing: dict[tuple[str, str, str], str],
    native: set[tuple[str, str]], months: list[str],
    now: datetime, max_attempts: int,
) -> list[tuple[str, str, str]]:
    """Complete recent source-month 5m first, then deep history and enrichment.

    Keep a small rotating share for existing archived-but-unqualified months
    so gaps are investigated without starving fresh multi-year acquisitions.
    """
    recent = set(months[-6:])
    recertify = [
        key for key in jobs if key[1] == "candlesticks_5m"
        and key in existing and (key[0], key[2]) not in native
    ]
    recent_candles = [
        key for key in jobs if key[1] == "candlesticks_5m"
        and key[2] in recent and key not in existing
    ]
    old_candles = [
        key for key in jobs if key[1] == "candlesticks_5m"
        and key[2] not in recent and key not in existing
    ]
    extra = [
        key for key in jobs if key[1] != "candlesticks_5m"
        and key not in existing
    ]
    recheck_count = min(len(recertify), max(1, max_attempts // 8))
    chosen = rotate_attempts(recertify, now=now, count=recheck_count)
    remaining = max_attempts - len(chosen)
    for group in (recent_candles, old_candles, extra):
        if remaining <= 0:
            break
        selected = rotate_attempts(group, now=now, count=remaining)
        chosen.extend(selected)
        remaining -= len(selected)
    return chosen


async def publish_raw(drive, *, archive: Path, source: dict,
                      asset: str, kind: str, month: str, root: Path) -> dict:
    """Preserve original provider bytes plus deterministic immutable receipt.

    Provider checksum is unavailable: our SHA proves byte identity and
    faithful Drive restoration, not a provider-signed data authenticity claim.
    """
    sha = source["compressed_sha256"]
    key = stem(asset=asset, kind=kind, month=month, sha=sha)
    target_dir = root / "gate-raw-verified"
    target_dir.mkdir(parents=True, exist_ok=True)
    raw = target_dir / (key + ".csv.gz")
    payload = archive.read_bytes()
    if hashlib.sha256(payload).hexdigest() != sha:
        raise HistoryImportError("Gate raw bytes diverged after source inspection")
    if raw.exists() and raw.read_bytes() != payload:
        raise HistoryImportError("immutable Gate raw archive conflict")
    if not raw.exists():
        raw.write_bytes(payload)
    manifest = target_dir / (key + ".manifest.json")
    proof = {
        "schema_version": "rival-gate-raw-futures-usdt-v1",
        "source": "gate_official_futures_usdt_monthly_gzip",
        "venue": "gate", "asset": asset,
        "contract": f"{asset}_USDT", "quote": "USDT",
        "market_type": "usdt_linear_perpetual",
        "kind": kind, "month": month,
        "source_url": source["source_url"],
        "raw_file": raw.name,
        "raw_sha256": sha,
        "rows": source["rows"],
        "first_source_timestamp": source["first_source_timestamp"],
        "last_source_timestamp": source["last_source_timestamp"],
        "header_present": source["has_header"],
        "provider_signed_checksum": False,
        "timestamp_calendar_verified": False,
        "full_utc_candle_month_verified": source["full_utc_candle_month_verified"],
        "expected_5m_candles": source["expected_5m_candles"],
        "retrospective": True,
        "prospective_forecast": False,
        "live_trading_authorized": False,
        "model_training_eligible": False,
        "no_exact_bid_ask_or_fills_verified": True,
    }
    encoded = (json.dumps(proof, sort_keys=True, indent=2) + "\n").encode()
    if manifest.exists() and manifest.read_bytes() != encoded:
        raise HistoryImportError("immutable Gate source receipt conflict")
    if not manifest.exists():
        manifest.write_bytes(encoded)
    history = _artifact(raw, int(source["rows"]))
    manifest_artifact = _artifact(manifest, 1)
    if history.sha256 != sha:
        raise HistoryImportError("Gate source artifact digest mismatch")
    batch_id = "gatebulk-" + sha[:24]
    saved = await drive.ensure_history_file(
        artifact=history, batch_id=batch_id, mime_type="application/gzip",
    )
    await drive.verify_downloaded_artifact(
        file_id=saved.file_id, parent_id=drive.folders.history, artifact=history,
    )
    stored_manifest = await drive.ensure_manifest_file(
        artifact=manifest_artifact, batch_id=batch_id,
    )
    await drive.verify_downloaded_artifact(
        file_id=stored_manifest.file_id,
        parent_id=drive.folders.manifests, artifact=manifest_artifact,
    )
    return {
        "asset": asset, "kind": kind, "month": month,
        "raw_sha256": sha, "rows": history.row_count,
        "source_url": source["source_url"],
        "warehouse_readback_sha256_verified": True,
        "full_utc_candle_month_verified": source["full_utc_candle_month_verified"],
        "source_native_model_eligible": False,
    }


def rotate_attempts(work: list[tuple[str, str, str]], *,
                    now: datetime, count: int) -> list[tuple[str, str, str]]:
    """Rotate failures rather than repeatedly retrying the oldest 404 forever."""
    if not work:
        return []
    cycle = int(now.astimezone(UTC).timestamp() // (6 * 3600))
    offset = (cycle * count) % len(work)
    return (work[offset:] + work[:offset])[:count]


async def collect_gate(*, drive, root: Path, report_dir: Path,
                       now: datetime | None = None,
                       assets: tuple[str, ...] = ASSETS,
                       kinds: tuple[str, ...] = DEFAULT_KINDS,
                       max_new: int = 24, max_attempts: int = 40) -> dict:
    clock = now or datetime.now(UTC)
    if (clock.utcoffset() is None or not assets or not kinds
        or len(assets) != len(set(assets)) or len(kinds) != len(set(kinds))
        or any(a not in ASSETS for a in assets)
        or any(k not in TYPES for k in kinds)
        or not 1 <= max_new <= MAX_NEW
        or not max_new <= max_attempts <= MAX_ATTEMPTS):
        raise ValueError("unapproved Gate historical batch scope")
    months = complete_months(clock)
    existing = await listed_archives(drive)
    native_months = await listed_native_months(drive)
    all_jobs = [(asset, kind, month)
                for month in months for kind in kinds for asset in assets]
    pending = [job for job in all_jobs if job not in existing]
    selected = prioritize_backfill(
        all_jobs, existing=existing, native=native_months,
        months=months, now=clock, max_attempts=max_attempts,
    )
    result: dict = {
        "schema_version": "rival-gate-public-bulk-progress-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "scope": {"assets": list(assets), "kinds": list(kinds),
                  "first_month": FIRST,
                  "last_complete_month": months[-1] if months else None},
        "planned_jobs": len(all_jobs), "remote_manifest_names_skipped_without_readback":
            len(all_jobs) - len(pending),
        "pending_jobs": len(pending),
        "selected_jobs": len(selected),
        "published": [], "reverified_existing": [],
        "unavailable_or_rejected": [],
        "native_qualified": [], "native_blockers": [],
        "same_venue_api_fallback": [],
        "five_minute_source_gap_inventory": [],
        "native_months_already_listed_without_readback": len(native_months),
        "model_research": {"status": "not_run_no_six_verified_months",
                           "published_models": 0},
        "provider_throttled": False,
        "full_source_coverage_proven": False,
        "live_predictions_modified": False,
        "model_approved": False,
    }
    root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    # One host client, sequential bounded requests, independent from Discord
    # and from existing OKX/Binance budgets.
    try:
        async with asyncio.timeout(9900):
            with httpx.Client(timeout=65, follow_redirects=False) as client:
                api_fallback_attempts = 0
                for asset, kind, month in selected:
                    if len(result["published"]) >= max_new:
                        break
                    key = (asset, kind, month)
                    try:
                        output = root / "incoming" / kind / month / (
                            f"{asset}_USDT-{month}.csv.gz"
                        )
                        receipt = await asyncio.to_thread(
                            download, client=client, asset=asset, kind=kind,
                            month=month, output=output,
                        )
                        already_published = key in existing
                        if (already_published
                            and existing[key] != receipt["compressed_sha256"][:16]):
                            raise HistoryImportError(
                                "Gate provider archive changed since immutable Drive copy"
                            )
                        saved = await publish_raw(
                            drive, archive=output, source=receipt,
                            asset=asset, kind=kind, month=month, root=root,
                        )
                        if already_published:
                            result["reverified_existing"].append(saved)
                        else:
                            result["published"].append(saved)
                        existing[key] = receipt["compressed_sha256"][:16]
                        if kind == "candlesticks_5m":
                            from parlay_bot.research.gate_native import (
                                gaps as gate_missing,
                            )
                            from parlay_bot.research.gate_native import (
                                original_candles,
                            )

                            native_rows, _ = await asyncio.to_thread(
                                original_candles, output, asset=asset, month=month,
                                expected_sha=receipt["compressed_sha256"],
                            )
                            missing_stamps = gate_missing(native_rows, month=month)
                            result["five_minute_source_gap_inventory"].append({
                                "asset": asset, "month": month,
                                "raw_sha256": receipt["compressed_sha256"],
                                "candles_original": len(native_rows),
                                "missing_five_minute_slots": len(missing_stamps),
                                "missing_utc_timestamps": [
                                    datetime.fromtimestamp(stamp, UTC).isoformat()
                                    for stamp in missing_stamps[:128]
                                ],
                                "gap_timestamp_list_truncated": len(missing_stamps) > 128,
                                "raw_calendar_complete": not missing_stamps,
                                "model_training_eligible": False,
                                "source_provider_checksum_signed": False,
                            })
                        if kind == "candlesticks_5m" and (asset, month) not in native_months:
                            from parlay_bot.research.gate_native import qualify_and_publish

                            try:
                                # This reads real Gate API gaps and contract
                                # metadata, never invents trading history.
                                qualification = await qualify_and_publish(
                                    drive, client=client, archive=output,
                                    source=receipt, asset=asset, month=month,
                                    root=root, now=clock,
                                )
                                result["native_qualified"].append(qualification)
                                native_months.add((asset, month))
                            except (HistoryImportError, DriveArchiveError,
                                    OSError, httpx.HTTPError, RuntimeError,
                                    ValueError) as native_exc:
                                reason = str(native_exc)[:240]
                                result["native_blockers"].append({
                                    "asset": asset, "month": month,
                                    "status": "research_training_not_qualified",
                                    "reason": reason,
                                })
                                if any(code in reason for code in (
                                    "HTTP 429", "HTTP 503", "HTTP 502", "HTTP 504",
                                )):
                                    result["provider_throttled"] = True
                                    break
                    except (HistoryImportError, DriveArchiveError, httpx.HTTPError,
                            OSError, RuntimeError, ValueError) as exc:
                        reason = str(exc)[:240]
                        result["unavailable_or_rejected"].append({
                            "asset": asset, "kind": kind, "month": month,
                            "reason": reason,
                        })
                        # A missing gzip may still have genuine same-Gate API
                        # history. Keep it separate from the official bulk
                        # archive and never count it toward native model proof.
                        if (kind == "candlesticks_5m" and "HTTP 404" in reason
                            and api_fallback_attempts < 1):
                            api_fallback_attempts += 1
                            from parlay_bot.research.gate_api_fallback import (
                                recover_unavailable_archive,
                            )
                            try:
                                fallback = await recover_unavailable_archive(
                                    drive, client=client, asset=asset,
                                    month=month, root=root, now=clock,
                                )
                            except (HistoryImportError, DriveArchiveError,
                                    httpx.HTTPError, OSError, ValueError) as api_exc:
                                api_reason = str(api_exc)[:240]
                                fallback = {
                                    "asset": asset, "month": month,
                                    "status": "api_unavailable_or_unqualified",
                                    "reason": api_reason,
                                    "model_training_eligible": False,
                                }
                                if any(code in api_reason for code in (
                                    "HTTP 429", "HTTP 503", "HTTP 502", "HTTP 504",
                                )):
                                    result["provider_throttled"] = True
                            result["same_venue_api_fallback"].append(fallback)
                            if result["provider_throttled"]:
                                break
                        if any(status in reason for status in
                               ("HTTP 429", "HTTP 503", "HTTP 502", "HTTP 504")):
                            result["provider_throttled"] = True
                            break  # Stop respecting the provider, not cycle proxies.
                recent = months[-6:]
                if len(recent) == 6 and any(
                    all((asset, month) in native_months for month in recent)
                    for asset in assets
                ):
                    from parlay_bot.research.source_native_offline import run_native_research

                    try:
                        trained = await run_native_research(
                            drive=drive, venue="gate", months=recent,
                            root=root / "gate-model-window",
                            report_dir=report_dir, publish=True,
                        )
                        result["model_research"] = {
                            "status": trained.get("status"),
                            "published_models": trained.get("published_models", 0),
                            "live_approved": False,
                        }
                    except (HistoryImportError, DriveArchiveError,
                            RuntimeError, OSError, ValueError) as exc:
                        result["model_research"] = {
                            "status": "blocked", "published_models": 0,
                            "reason": str(exc)[:240],
                        }
    finally:
        from parlay_bot.research.gate_coverage import build_gate_coverage

        coverage = build_gate_coverage(
            months=months, assets=assets, kinds=kinds, existing=existing,
            native_months=native_months, result=result,
        )
        ledger = report_dir / "gate-source-month-ledger.json"
        ledger.write_text(
            json.dumps(coverage, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["coverage_ledger_local_path"] = str(ledger)
        result["coverage_status_counts"] = coverage["source_month_status_counts"]
        result["native_status_counts"] = coverage["native_5m_status_counts"]
        result["coverage_ledger_private_drive_verified"] = False
        destination = report_dir / "gate-bulk-progress.json"
        destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate official file-first Drive archival")
    parser.add_argument("--root", type=Path, default=Path("data/gate-bulk"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    parser.add_argument("--max-new", type=int, default=24)
    parser.add_argument("--max-attempts", type=int, default=40)
    parser.add_argument("--assets", nargs="+", choices=ASSETS, default=list(ASSETS))
    parser.add_argument("--kinds", nargs="+", choices=TYPES,
                        default=list(DEFAULT_KINDS))
    args = parser.parse_args(argv)
    try:
        drive = _drive(Settings())
        async def worker():
            try:
                report = await collect_gate(
                    drive=drive, root=args.root, report_dir=args.report_dir,
                    max_new=args.max_new, max_attempts=args.max_attempts,
                    assets=tuple(args.assets), kinds=tuple(args.kinds),
                )
                # Persist the source-month ledger beyond ephemeral Actions
                # artifacts, with actual Drive byte readback. A failed upload
                # must never be displayed as a completed archival job.
                ledger_path = Path(report["coverage_ledger_local_path"])
                proof = _artifact(ledger_path, 1)
                stored = await drive.ensure_research_output(
                    artifact=proof, batch_id="gate-ledger-" + proof.sha256[:24],
                    kind="report",
                )
                await drive.verify_downloaded_artifact(
                    file_id=stored.file_id, parent_id=drive.folders.reports,
                    artifact=proof,
                )
                report["coverage_ledger_private_drive_verified"] = True
                report["coverage_ledger_sha256"] = proof.sha256
                (args.report_dir / "gate-bulk-progress.json").write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                return report
            finally:
                await drive.aclose()
        data = asyncio.run(worker())
    except (HistoryImportError, DriveArchiveError, httpx.HTTPError,
            RuntimeError, ValueError, OSError) as exc:
        parser.exit(2, f"Gate archival blocked: {exc}\n")
    print(json.dumps({
        "published_new": len(data["published"]),
        "reverified_existing": len(data["reverified_existing"]),
        "unavailable_or_rejected": len(data["unavailable_or_rejected"]),
        "provider_throttled": data["provider_throttled"],
        "native_qualified": len(data["native_qualified"]),
        "native_blocked": len(data["native_blockers"]),
        "five_minute_gap_inventory_files": len(data["five_minute_source_gap_inventory"]),
        "model_research": data["model_research"]["status"],
        "ledger_drive_verified": data["coverage_ledger_private_drive_verified"],
        "source_states": data["coverage_status_counts"],
        "pending_jobs": data["pending_jobs"],
    }, sort_keys=True))
    # Never print an empty run as a successful production ingestion when
    # every selected archive is blocked or the provider asks to slow down.
    return 2 if data["provider_throttled"] or (
        data["selected_jobs"] and not data["published"]
        and not data["reverified_existing"]
        and not any(
            f.get("status") == "api_complete_research_only"
            and f.get("source_api_response_readback_verified") is True
            for f in data["same_venue_api_fallback"]
        )
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
