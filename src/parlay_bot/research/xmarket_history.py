"""Verified XMarket inputs through the existing supplement/Drive pipeline.

No credentials are introduced. Existing native collectors publish daily
first-observed samples; the weekly archive runner restores these and uses
the existing exact-contract ingest to measure historical fidelity. A bounded
backfill advances a saved cursor, so an interrupted run is resumable.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from parlay_bot.perpetuals.xmarket_inputs import (
    BUNDLE_PREFIX,
    BUNDLE_SCHEMA,
    canonical,
    parse_points,
)
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import _artifact, prepare, publish
from parlay_bot.research.supplemental_history import (
    KALSHI,
    _json_get,
    ingest_kalshi_contract,
    ingest_polymarket_token,
    kalshi_public_json,
)
from parlay_bot.research.xmarket_features import (
    PRICE_SEMANTICS,
    sampled_bars,
    timestamp,
    validate_sample,
)

GAMMA = "https://gamma-api.polymarket.com"
MIN_COMPARISON_POINTS = 30
PRICE_TOLERANCE = 0.02
MAX_MISMATCH_RATE = 0.05


def source_failure(source: str, stage: str, exc: Exception, *, window_close=None) -> dict:
    """Keep actionable, non-secret failure evidence in offline training reports.

    Generic HistoryImportError wrappers otherwise conceal whether a native
    provider returned a 404, throttled requests, timed out, or rejected data.
    Never record exception text, request URLs, headers, or response bodies.
    """
    reason = exc
    seen = set()
    while getattr(reason, "__cause__", None) is not None and id(reason) not in seen:
        seen.add(id(reason))
        reason = reason.__cause__
    status = reason.response.status_code if isinstance(reason, httpx.HTTPStatusError) else None
    if status is not None:
        category = f"http_{status}"
        retryable = status in (408, 425, 429) or 500 <= status < 600
    elif isinstance(reason, httpx.TimeoutException):
        category, retryable = "timeout", True
    elif isinstance(reason, httpx.TransportError):
        category, retryable = "transport", True
    elif isinstance(exc, HistoryImportError):
        category, retryable = "history_integrity", False
    elif isinstance(exc, (ValueError, KeyError)):
        category, retryable = "contract_identity_or_schema", False
    else:
        category, retryable = "unclassified", False
    record = {"source": source, "stage": stage, "error": type(exc).__name__,
              "failure_code": category, "retryable": retryable}
    if window_close is not None:
        record["window_close"] = timestamp(window_close).isoformat()
    return record


def validate_bundle(doc: dict) -> dict:
    if doc.get("schema") != BUNDLE_SCHEMA or doc.get("asset") not in ("BTC", "ETH", "SOL", "XRP"):
        raise ValueError("invalid XMarket input bundle")
    samples = doc.get("samples")
    if not isinstance(samples, list) or len(samples) > 30_000:
        raise ValueError("unbounded XMarket bundle")
    for row in samples:
        validate_sample(row)
        if row["asset"] != doc["asset"]:
            raise ValueError("cross-asset XMarket input bundle")
    return doc


def compare_history(live, history) -> dict:
    """Compare exact native contracts on the bars actually used by the model.

    Both twins use the same final-minute sampling rule. No raw-minute count
    can inflate the number of independent bar comparisons, and a 15-minute
    fallback is checked only on its explicitly pinned native grid.
    """
    checks = {}
    assets = {r["asset"] for r in [*live, *history]}
    if len(assets) > 1:
        raise ValueError("XMarket fidelity cannot pool different assets")
    asset = next(iter(assets), "BTC")
    for source in PRICE_SEMANTICS:
        fidelity = max((r["fidelity_minutes"] for r in history if r["source"] == source), default=1)
        prospective, retrospective, revised = {}, {}, set()
        for dest, points in ((prospective, live), (retrospective, history)):
            for row in points:
                if row["source"] != source:
                    continue
                key = row["contract_id"], row["at"]
                if key in dest and dest[key]["price"] != row["price"]:
                    revised.add(key)
                dest[key] = row
        observed = sampled_bars(list(prospective.values()), asset=asset, pm_fidelity=fidelity)[source]
        historical = sampled_bars(list(retrospective.values()), asset=asset, pm_fidelity=fidelity)[source]
        paired_keys = set(observed) & set(historical)
        paired = len(paired_keys)
        mismatches = sum(
            observed[key]["contract_id"] != historical[key]["contract_id"]
            or (historical[key]["contract_id"], historical[key]["at"]) in revised
            or abs(float(historical[key]["price"]) - float(observed[key]["price"])) > PRICE_TOLERANCE
            for key in paired_keys)
        missing = len(set(observed) - set(historical))
        rate = mismatches / paired if paired else None
        coverage = paired / len(observed) if observed else 0
        if not prospective:
            blocked_reason = "no_prospective_export"
        elif not observed:
            blocked_reason = "no_eligible_live_feature_bars"
        elif not retrospective:
            blocked_reason = "no_independent_history"
        elif not historical:
            blocked_reason = "no_eligible_historical_feature_bars"
        elif not paired:
            blocked_reason = "no_exact_feature_bar_overlap"
        elif paired < MIN_COMPARISON_POINTS:
            blocked_reason = "insufficient_exact_pairs"
        elif rate > MAX_MISMATCH_RATE:
            blocked_reason = "price_mismatch"
        elif coverage < 0.8:
            blocked_reason = "insufficient_overlap_coverage"
        else:
            blocked_reason = None
        checks[source] = {"status": "verified" if blocked_reason is None else "unverified",
            "blocked_reason": blocked_reason,
            "raw_live_points": len(prospective), "raw_history_points": len(retrospective),
            "live_feature_bars": len(observed), "history_feature_bars": len(historical),
            "paired_points": paired, "comparison_unit": "native-contract-at-feature-bar",
            "mismatches": mismatches, "missing_points": missing,
            "mismatch_rate": rate, "overlap_coverage": coverage,
            "price_tolerance": PRICE_TOLERANCE, "minimum_pairs": MIN_COMPARISON_POINTS,
            "effective_fidelity_minutes": fidelity, "native_grid_only": fidelity == 15}
    return checks


def combine(bundles, *, history=(), errors=()) -> dict:
    bundles = [validate_bundle(b) for b in bundles]
    if len({b["asset"] for b in bundles}) > 1:
        raise ValueError("XMarket inputs must be scoped to one asset")
    live = [r for b in bundles for r in b["samples"] if r.get("origin") == "live"]
    history = [*history, *(r for b in bundles for r in b["samples"] if r.get("origin") == "backfill")]
    # Historical files may overlap daily live exports. Keep the earliest
    # prospective observation, but reject a changed price/identity.
    points = {}
    conflicts = set()
    for row in [*history, *live]:
        validate_sample(row)
        key = row["source"], row["contract_id"], row["at"]
        previous = points.get(key)
        if previous is not None and any(row.get(k) != previous.get(k) for k in
                                        ("asset", "price", "opens_at", "closes_at", "price_semantics")):
            # Fidelity comparison reports genuine revisions; conflicted bars
            # are excluded instead of silently preferring either version.
            conflicts.add(key)
        elif previous is None or timestamp(row["observed_at"]) < timestamp(previous["observed_at"]):
            points[key] = row
    latest = max(bundles, key=lambda b: b.get("created_at", ""), default={})
    return {"samples": [r for key, r in points.items() if key not in conflicts],
            "live_venues": latest.get("live_venues", []),
            "verification": compare_history(live, history), "errors": list(errors),
            "receipts": [{"file_id": b.get("_drive_file_id"), "sha256": b["_bundle_sha256"]}
                         for b in bundles if b.get("_bundle_sha256")]}


def load_local_inputs(path: Path, *, asset: str | None = None) -> dict:
    """Only canonical, typed input bundles; arbitrary price CSVs are not provenance."""
    pattern = f"xmarket-inputs-{asset}-*.json" if asset else "xmarket-inputs-*.json"
    paths = sorted(path.glob(pattern)) if path.is_dir() else [path]
    docs = [json.loads(p.read_text()) for p in paths]
    if len(docs) == 1 and docs[0].get("schema") == "rival-xmarket-training-inputs-v1":
        for row in docs[0].get("samples", []):
            validate_sample(row)
        return docs[0]
    return combine(docs)


def _identity_pm(raw: dict, *, asset: str, opens: datetime) -> dict:
    from parlay_bot.discord_app.polymarket_watch import _chainlink_twap, _decode_array, market_slug

    slug = market_slug(asset, 15, opens)
    if raw.get("slug") != slug or len(raw.get("markets", [])) != 1:
        raise ValueError("unverified Gamma contract")
    m = raw["markets"][0]
    start = timestamp(raw.get("eventStartTime") or m.get("eventStartTime"))
    end = timestamp(raw.get("endDate") or m.get("endDate"))
    tokens = _decode_array(m.get("clobTokenIds"))
    condition = m.get("conditionId")
    if (start != opens or end != opens + timedelta(minutes=15)
            or _decode_array(m.get("outcomes")) != ["Up", "Down"]
            or len(tokens) != 2 or tokens[0] == tokens[1]
            or not all(isinstance(t, str) and re.fullmatch(r"[0-9]{20,90}", t) for t in tokens)
            or not isinstance(condition, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition)
            or not _chainlink_twap(m.get("resolutionSource"), m.get("description"), asset)):
        raise ValueError("Gamma event binding mismatch")
    return {"asset": asset, "contract_id": condition, "token_id": tokens[0], "slug": slug,
            "opens_at": start.isoformat(), "closes_at": end.isoformat()}


def collect_contract(client, *, source, identity, root, now, fidelity=1):
    """Reuse supplemental manifest/hash/publish format, including identity binding."""
    start, end = (int(timestamp(identity[k]).timestamp()) for k in ("opens_at", "closes_at"))
    if source == "polymarket":
        item = ingest_polymarket_token(client, condition_id=identity["contract_id"],
            token_id=identity["token_id"], start_ts=start, end_ts=end,
            fidelity=fidelity, out_dir=root, contract_binding=identity, timeout_seconds=8)
        payload = {"history": [{"t": int(timestamp(r["at"]).timestamp()), "p": r["price"]}
                   for r in (json.loads(l) for l in item.data_path.read_bytes().splitlines())]}
    else:
        item = ingest_kalshi_contract(client, ticker=identity["contract_id"], start_ts=start,
            end_ts=end, period_minutes=1, out_dir=root, series_ticker="KX" + identity["asset"] + "15M",
            contract_binding=identity, timeout_seconds=8, prefer_live=True,
            rate_limit_retries=2, pace_seconds=0.35)
        payload = {"candlesticks": [{"end_period_ts": int(timestamp(r["at"]).timestamp()),
                    "yes_ask": r["yes_ask"], "yes_bid": r["yes_bid"]}
                   for r in (json.loads(l) for l in item.data_path.read_bytes().splitlines())]}
    if source == "polymarket" and fidelity < 15 and len(payload["history"]) < 3:
        return collect_contract(client, source=source, identity=identity, root=root, now=now, fidelity=15)
    publication = prepare(item.manifest_path)
    metadata = json.loads(item.manifest_path.read_text())
    if metadata.get("xmarket_contract") != identity:
        raise ValueError("missing supplemental contract binding")
    points = parse_points(source, payload, identity=identity, observed_at=now,
                          origin="backfill", fidelity=fidelity)
    for row in points:
        row["provider_sha256"] = metadata["source_sha256"]
        row["normalized_sha256"] = metadata["data_sha256"]
        validate_sample(row)
    return points, publication


def backfill(client, *, asset, bundles, root, before, max_windows=96, days=90):
    """Bounded/resumable historical acquisition plus fresh fidelity overlap checks."""
    now = datetime.now(UTC)
    history, publications, errors = [], [], []
    identities = {}
    for bundle in bundles:
        for p in bundle["samples"]:
            if p.get("origin") == "live":
                identity = {k: p[k] for k in
                    ("asset", "contract_id", "opens_at", "closes_at", "token_id", "slug") if k in p}
                identities[p["source"], p["contract_id"]] = identity
    # Compare up to 16 recent native contracts per source, on independently
    # fetched history; 30+ actual timestamp pairs are required to pass.
    for source in PRICE_SEMANTICS:
        items = [(key, val) for key, val in identities.items() if key[0] == source]
        consecutive_failures = 0
        for (src, _), identity in sorted(items, key=lambda x: x[1]["opens_at"], reverse=True)[:32]:
            try:
                rows, publication = collect_contract(client, source=src, identity=identity, root=root, now=now)
                history.extend(rows)
                publications.append(publication)
                consecutive_failures = 0
            except (httpx.HTTPError, ValueError, HistoryImportError) as exc:
                failure = source_failure(source, "overlap", exc, window_close=identity["closes_at"])
                errors.append(failure)
                # Try other contracts after an isolated bad identity. Don't
                # spend 32 network timeouts on a throttled/down provider.
                consecutive_failures += 1
                if failure["retryable"] or consecutive_failures >= 3:
                    break
    # Archive "through" can name an unfinished native window. Never try
    # acquiring its as-yet-unpublished history or advancing a cursor past now.
    last_closed = datetime.fromtimestamp(int(now.timestamp()) // 900 * 900, UTC)
    cursor = min(timestamp(before), last_closed)
    floor = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    for _ in range(max_windows):
        opened = cursor - timedelta(minutes=15)
        if opened < floor:
            break
        from parlay_bot.discord_app.polymarket_watch import market_slug
        failed_window = False
        try:
            event = _json_get(client, f"{GAMMA}/events/slug/{market_slug(asset, 15, opened)}", {}, timeout=8)
            identity = _identity_pm(event, asset=asset, opens=opened)
            rows, publication = collect_contract(client, source="polymarket", identity=identity,
                                                  root=root, now=now)
            history.extend(rows)
            publications.append(publication)
        except (httpx.HTTPError, ValueError, HistoryImportError) as exc:
            errors.append(source_failure("polymarket", "backfill", exc, window_close=cursor))
            failed_window = True
        try:
            series = "KX" + asset + "15M"
            params = {"series_ticker": series, "min_close_ts": int(cursor.timestamp()) - 1,
                      "max_close_ts": int(cursor.timestamp()) + 1, "limit": 10}
            markets = kalshi_public_json(client, KALSHI + "/markets", params,
                timeout=8, rate_limit_retries=2, pace_seconds=0.35).get("markets", [])
            if not markets:
                markets = kalshi_public_json(client, KALSHI + "/historical/markets", params,
                    timeout=8, rate_limit_retries=2, pace_seconds=0.35).get("markets", [])
            candidates = [m for m in markets if isinstance(m.get("ticker"), str)
                          and m["ticker"].startswith(series + "-")
                          and timestamp(m["open_time"]) == opened
                          and timestamp(m["close_time"]) == cursor]
            if len(candidates) != 1:
                raise ValueError("native Kalshi contract not uniquely bound")
            identity = {"asset": asset, "contract_id": candidates[0]["ticker"],
                        "opens_at": opened.isoformat(), "closes_at": cursor.isoformat()}
            rows, publication = collect_contract(client, source="kalshi", identity=identity, root=root, now=now)
            history.extend(rows)
            publications.append(publication)
        except (httpx.HTTPError, ValueError, KeyError, HistoryImportError) as exc:
            errors.append(source_failure("kalshi", "backfill", exc, window_close=cursor))
            failed_window = True
        if failed_window:
            break
        cursor = opened
    return history, publications, cursor, errors


async def weekly_inputs(drive, *, asset: str, through, cache: Path, max_windows=96) -> dict:
    bundles, errors = [], []
    if drive is not None:
        cursor = None
        for _ in range(32):
            items, cursor = await drive.read_verified_research_reports(limit=20, page_token=cursor,
                return_next_page=True, name_prefix=BUNDLE_PREFIX + asset + "-")
            for file_id, payload in items:
                bundles.append({**validate_bundle(json.loads(payload)), "_drive_file_id": file_id,
                                "_bundle_sha256": hashlib.sha256(payload).hexdigest()})
            if cursor is None:
                break
        else:
            raise ValueError("XMarket input listing exceeds bounded archive window")
    cache.mkdir(parents=True, exist_ok=True)
    for path in cache.glob(f"{BUNDLE_PREFIX}{asset}-*.json"):
        payload = path.read_bytes()
        bundles.append({**validate_bundle(json.loads(payload)),
                        "_bundle_sha256": hashlib.sha256(payload).hexdigest()})
    # A new restore starts at yesterday's close. Persisted cursors advance
    # older work rather than downloading the same day every Sunday.
    before = min([timestamp(b["backfill_before"]) for b in bundles if b.get("backfill_before")]
                 or [timestamp(through).replace(second=0, microsecond=0) + timedelta(minutes=5)])
    before = datetime.fromtimestamp(int(before.timestamp()) // 900 * 900, UTC)
    def collect():
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            return backfill(client, asset=asset, bundles=bundles, root=cache / "supplements",
                            before=before, max_windows=max_windows)
    history, publications, next_before, failures = await asyncio.to_thread(collect)
    errors.extend(failures)
    newest = max(bundles, key=lambda b: b.get("created_at", ""), default={})
    bundle = {"schema": BUNDLE_SCHEMA, "asset": asset, "samples": history,
              "live_venues": newest.get("live_venues", []), "verification": {},
              "backfill_before": next_before.isoformat(), "created_at": datetime.now(UTC).isoformat()}
    if history:
        # Split files stay under the existing 2.2 MB report import ceiling.
        for offset in range(0, len(history), 2000):
            doc = {**bundle, "samples": history[offset:offset + 2000]}
            digest = hashlib.sha256(canonical(doc)).hexdigest()
            path = cache / f"{BUNDLE_PREFIX}{asset}-backfill-{digest[:16]}.json"
            path.write_bytes(canonical(doc))
            if drive is not None:
                for publication in publications:
                    await publish(drive, publication)
                publications = []
                await drive.ensure_research_output(artifact=_artifact(path, len(doc["samples"])),
                    batch_id="xmarket-inputs-" + digest[:24], kind="report")
    return combine([*bundles, bundle], errors=errors)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restore/verify/backfill XMarket native history")
    parser.add_argument("--asset", required=True, choices=("BTC", "ETH", "SOL", "XRP"))
    parser.add_argument("--through", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-windows", type=int, default=96)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.max_windows <= 8640:
        parser.error("max-windows must be 0..8640")
    async def run():
        from parlay_bot.config import Settings
        from parlay_bot.research.publish import _drive
        drive = _drive(Settings()) if args.publish else None
        try:
            with TemporaryDirectory() as work:
                inputs = await weekly_inputs(drive, asset=args.asset, through=args.through,
                                              cache=Path(work), max_windows=args.max_windows)
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_bytes(canonical({"schema": "rival-xmarket-training-inputs-v1", "asset": args.asset, **inputs}))
                print(json.dumps({"samples": len(inputs["samples"]), "verification": inputs["verification"],
                                  "errors": inputs["errors"]}))
        finally:
            if drive is not None:
                await drive.aclose()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
