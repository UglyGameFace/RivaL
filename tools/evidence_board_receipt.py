"""Publish tiny authenticated, read-only RivaL host/DB/Drive evidence receipts.

The public dashboard is never given Discloud credentials, PostgreSQL URLs,
Google OAuth tokens, raw reports, model weights, or private Drive file IDs.
Only a GitHub Actions main-branch job with a GitHub OIDC identity may publish.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import UTC, datetime
from urllib import error, parse, request

from discloud_learning_cycle_receipt import CMD, parse_receipt
from evidence_board_github import read_github_snapshot, read_public_intelligence

ASSETS = {"BTC", "ETH", "SOL", "XRP"}
HORIZONS = {5, 15, 60}
REPORT_NAME = re.compile(r"xmarket-v1-(BTC|ETH|SOL|XRP)-(5|15|60)m-report\.json")
XMARKET_REASONS = {
    "no_prospective_export", "no_eligible_live_feature_bars", "no_independent_history",
    "no_eligible_historical_feature_bars", "no_exact_feature_bar_overlap",
    "insufficient_exact_pairs", "price_mismatch", "insufficient_overlap_coverage",
    "insufficient_overlap", "missing_history", "mismatch", "http_429", "unverified",
    "insufficient_training_split", "insufficient_labelled_rows", "offline_gates_failed",
    "offline_gates_passed",
}
XMARKET_STAGES = {
    "not_initialized", "waiting_for_inputs", "waiting_for_completed_day_export",
    "waiting_for_fit", "imported_no_shadow_rows", "shadow_issuing", "read_unavailable",
}
INGEST_URL = "https://rival-evidence-collector.staytrippynlw.workers.dev/ingest"
INGEST_ATTEMPT_URL = "https://rival-evidence-collector.staytrippynlw.workers.dev/ingest-attempt"
COLLECTOR_LATEST_URL = "https://rival-evidence-collector.staytrippynlw.workers.dev/latest"
AUDIENCE = "https://rival-evidence-collector.staytrippynlw.workers.dev"
# A redundant trigger (push / workflow_run flood) must not re-hit Discloud,
# Drive and the receiver while a receipt younger than this stands. Schedule
# runs are the cadence backbone and never coalesce.
COALESCE_FRESH_SECONDS = 300
COLLECTION_EVENTS = ("schedule", "workflow_dispatch", "push", "workflow_run")
# Per-source reliability reporting (Delivery 1): every upstream is tracked
# independently, with its own freshness bar. A failed read is unavailable,
# never zero; a genuinely measured zero stays a zero with status ok (or
# measured_zero where the read itself reports "nothing found").
SOURCE_NAMES = (
    "discloud_forecast", "discloud_learning", "postgres", "drive",
    "github", "intelligence", "intelligence_host", "performance",
)
SOURCE_FRESHNESS_SECONDS = {
    "discloud_forecast": 900, "discloud_learning": 900, "postgres": 3600,
    "drive": 86400, "github": 3600, "intelligence": 3600,
    "intelligence_host": 3600, "performance": 3600,
}
SOURCE_STATUSES = {
    "ok", "unreachable", "auth_failed", "failed_read", "no_records",
    "measured_zero", "not_configured", "stale",
}
ATTEMPT_OUTCOMES = ("accepted", "failed", "coalesced")
ATTEMPT_REASON = re.compile(r"[a-z0-9_]{1,80}")
# Failure reasons that are safe to publish: fixed labels only, never raw
# provider bodies, tokens, URLs, or private identifiers.
READ_ERROR_LABELS = {
    "drive_credentials_not_configured": ("not_configured", "credentials_not_configured"),
    "host_credentials_not_configured": ("not_configured", "credentials_not_configured"),
    "drive_access_token_unavailable": ("auth_failed", "drive_access_token_unavailable"),
    "github_oidc_not_available": ("auth_failed", "github_oidc_not_available"),
    "github_oidc_token_missing": ("auth_failed", "github_oidc_token_missing"),
}
PAIR_MARKER = "RIVAL_PAIRED_SCORE_RECEIPT="
PAIR_CMD = ("python -c 'import sys;sys.path.insert(0,\"src\");"
            "from parlay_bot.perpetuals.evidence_score_receipt import main;main()'")


def _url_json(url: str, *, method: str = "GET", body: bytes | None = None,
              headers: dict[str, str] | None = None, timeout: int = 25,
              limit: int = 500_000) -> dict:
    req = request.Request(url, data=body, method=method, headers=headers or {})
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("provider_response_too_large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise TypeError("provider_response_not_object")
        return result


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 10_000_000 else None


def _sha_prefix(value: object) -> str | None:
    """A 12-hex-char artifact identifier; never a full digest or weights."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value):
        return value
    return None


def _artifact_prefix(document: dict) -> str | None:
    """Canonical-JSON SHA prefix, the same identity the host import stores.

    The host keys an imported artifact by sha256 of the canonical document
    (sorted keys, compact separators). Recomputing it here from the same
    parsed Drive document lets the board match a report to its host import
    without ever publishing a full digest, weights or Drive file ID.
    """
    import hashlib

    try:
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (TypeError, ValueError):
        return None
    return digest[:12]


def _iso_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        clock = datetime.fromisoformat(value)
        return clock.astimezone(UTC).isoformat() if clock.utcoffset() is not None else None
    except ValueError:
        return None


def _export_summary(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    published, pending = [], []
    source = value.get("published")
    for item in (source if isinstance(source, list) else [])[:4]:
        if not isinstance(item, dict) or item.get("asset") not in ASSETS:
            continue
        day = item.get("latest_exported_day")
        published.append({"asset": item["asset"],
            "exported_days": _nonnegative(item.get("exported_days")),
            "latest_exported_day": day if isinstance(day, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None})
    source = value.get("pending_by_asset")
    for item in (source if isinstance(source, list) else [])[:4]:
        if isinstance(item, dict) and item.get("asset") in ASSETS:
            pending.append({"asset": item["asset"], "days": _nonnegative(item.get("days"))})
    return {"published": published, "pending_by_asset": pending,
        "recent_completed_days_pending": _nonnegative(value.get("recent_completed_days_pending")),
        "eligible_before": _iso_timestamp(value.get("eligible_before")),
        "does_not_prove_independent_drive_restore": True}



def _xmarket_scope_progress(value: object) -> list[dict] | None:
    """Publish only bounded per-scope host counts/statuses, never model documents."""
    if not isinstance(value, list):
        return None
    output, seen = [], set()
    for row in value[:12]:
        if not isinstance(row, dict):
            continue
        asset, horizon = row.get("asset"), row.get("horizon_minutes")
        if asset not in ASSETS or type(horizon) is not int or horizon not in HORIZONS:
            continue
        if (asset, horizon) in seen or row.get("status") not in XMARKET_STAGES:
            continue
        seen.add((asset, horizon))
        output.append({
            "asset": asset,
            "horizon": horizon,
            "status": row["status"],
            "stored_artifacts": _nonnegative(row.get("stored_gate_passing_artifacts")),
            "shadow_rows_24h": _nonnegative(row.get("shadow_rows_24h")),
            "latest_imported_at": _iso_timestamp(row.get("latest_imported_at")),
            "latest_shadow_at": _iso_timestamp(row.get("latest_shadow_at")),
            "fresh_native_sources": _nonnegative(row.get("fresh_native_sources")),
            "expected_native_sources": 2,
            "latest_artifact_sha256_prefix": _sha_prefix(
                row.get("latest_artifact_sha256_prefix")),
            "latest_trained_through": _iso_timestamp(
                row.get("latest_trained_through")),
            "missing_native_sources": sorted(set(row.get("missing_native_sources", []))
                & {"polymarket", "kalshi"}) if isinstance(
                row.get("missing_native_sources"), list) and all(
                isinstance(x, str) for x in row["missing_native_sources"]) else [],
            "stale_native_sources": sorted(set(row.get("stale_native_sources", []))
                & {"polymarket", "kalshi"}) if isinstance(
                row.get("stale_native_sources"), list) and all(
                isinstance(x, str) for x in row["stale_native_sources"]) else [],
            "proves_live_edge": False,
        })
    return sorted(output, key=lambda r: (r["asset"], r["horizon"]))


def _recent_forecast_summary(source: object) -> dict:
    """Second allowlist before the signed Cloudflare receiver checks fields.

    Up to one research forecast per supported asset/horizon, no raw contracts,
    weights, lineages, account IDs, passwords or broker order identifiers.
    """
    unavailable = {"status": "read_unavailable", "captured_at": None,
                   "window_hours": 6, "rows": [], "rows_per_scope": 3,
                   "not_trading_calls": True}
    if not isinstance(source, dict) or source.get("status") not in (
        "observed", "no_recent_forecasts",
    ):
        return unavailable
    at = _iso_timestamp(source.get("captured_at"))
    if not at:
        return unavailable
    rows, counts = [], {}
    raw = source.get("rows")
    if not isinstance(raw, list):
        return unavailable
    from datetime import datetime as _dt

    capture = _dt.fromisoformat(at)
    for row in raw[:36]:
        if not isinstance(row, dict):
            continue
        asset, horizon = row.get("asset"), row.get("horizon")
        if asset not in ASSETS or horizon not in HORIZONS:
            continue
        if counts.get((asset, horizon), 0) >= 3:
            continue
        venue, quote = row.get("venue"), row.get("quote")
        if (not isinstance(venue, str)
                or re.fullmatch(r"[a-z0-9_-]{2,28}", venue) is None
                or not isinstance(quote, str)
                or re.fullmatch(r"[A-Z]{2,8}", quote) is None):
            continue
        issued, target = _iso_timestamp(row.get("issued_at")), _iso_timestamp(
            row.get("target_close_at")
        )
        opened = _iso_timestamp(row.get("target_opened_at"))
        settled_raw = row.get("settled_at")
        settled = (_iso_timestamp(settled_raw) if settled_raw is not None
                   else None)
        if issued is None or target is None or opened is None:
            continue
        # The audited outcome target is the close of a five-minute candle.
        if ((_dt.fromisoformat(target) - _dt.fromisoformat(opened))
                .total_seconds() != 300):
            continue
        if settled_raw is not None and settled is None:
            continue
        prob, baseline, lead = (row.get("probability_up"),
                                row.get("baseline_up"), row.get("lead_seconds"))
        if not (type(prob) in (int, float) and type(baseline) in (int, float)
                and 0 <= prob <= 1 and 0 <= baseline <= 1
                and type(lead) in (int, float) and 0 < lead <= 7200):
            continue
        direction, status, outcome = (
            row.get("direction"), row.get("status"), row.get("outcome_up")
        )
        if direction not in ("UP", "DOWN", "NEUTRAL") or status not in (
            "open", "awaiting_settlement", "settled", "ungraded",
        ):
            continue
        if (prob > 0.5 and direction != "UP" or
                prob < 0.5 and direction != "DOWN" or
                prob == 0.5 and direction != "NEUTRAL"):
            continue
        if status == "settled" and type(outcome) is not int or (
            status == "settled" and outcome not in (0, 1)
        ) or (status != "settled" and outcome is not None):
            continue
        # Preserve the original host event clock: an open target cannot
        # have closed yet, a settled/ungraded/awaiting target cannot claim
        # a future close, and a settlement cannot predate its target close.
        if ((status == "open" and _dt.fromisoformat(target) <= capture)
                or (status != "open"
                    and _dt.fromisoformat(target) > capture)):
            continue
        if status == "settled":
            if (settled is None
                    or _dt.fromisoformat(settled) < _dt.fromisoformat(target)
                    or (_dt.fromisoformat(settled) - capture).total_seconds() > 120):
                continue
        elif settled is not None:
            continue
        counts[(asset, horizon)] = counts.get((asset, horizon), 0) + 1
        rows.append({
            "asset": asset, "horizon": horizon, "venue": venue,
            "quote": quote, "issued_at": issued,
            "target_opened_at": opened, "target_close_at": target,
            "settled_at": settled if status == "settled" else None,
            "probability_up": round(float(prob), 6),
            "baseline_up": round(float(baseline), 6),
            "direction": direction, "status": status,
            "lead_seconds": round(float(lead), 2),
            "outcome_up": outcome if status == "settled" else None,
            # The model version is a receipt-wide constant; the signed
            # receiver re-attaches it so rows do not repeat it 36 times.
        })
    status = "observed" if rows else (
        "no_recent_forecasts" if source["status"] == "no_recent_forecasts"
        else "read_unavailable"
    )
    return {"status": status, "captured_at": at, "window_hours": 6,
            "rows_per_scope": 3, "rows": rows, "not_trading_calls": True}


def _finite_number(value: object, *, limit: float) -> float | None:
    if type(value) in (int, float) and -limit <= float(value) <= limit:
        return round(float(value), 6)
    return None


def _paper_checks(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    keys = ("future_outcomes", "beats_baselines", "quote_coverage",
            "after_cost_paper", "valid_records")
    out = {}
    for key in keys:
        if type(value.get(key)) is not bool:
            return None
        out[key] = value[key]
    return out


def _paper_scope_row(value: object) -> dict | None:
    """One exact-source paper evaluation: counts, gates, cost, promotion."""
    if not isinstance(value, dict):
        return None
    asset, horizon = value.get("asset"), value.get("horizon_minutes")
    venue, quote = value.get("venue"), value.get("quote")
    if (asset not in ASSETS or type(horizon) is not int or horizon not in HORIZONS
            or not isinstance(venue, str)
            or re.fullmatch(r"[a-z0-9_-]{2,28}", venue) is None
            or not isinstance(quote, str)
            or re.fullmatch(r"[A-Z]{2,8}", quote) is None
            or type(value.get("fresh")) is not bool
            or type(value.get("qualified_paper")) is not bool):
        return None
    checks = _paper_checks(value.get("checks"))
    coverage = value.get("quote_coverage")
    if (checks is None or type(coverage) not in (int, float)
            or not 0 <= float(coverage) <= 1):
        return None
    row = {
        "asset": asset, "venue": venue, "quote": quote,
        "horizon_minutes": horizon, "fresh": value["fresh"],
        "targets": _nonnegative(value.get("targets")),
        "days": _nonnegative(value.get("days")),
        "eligible_quotes": _nonnegative(value.get("eligible_quotes")),
        "closed_quotes": _nonnegative(value.get("closed_quotes")),
        "quote_coverage": round(float(coverage), 4),
        "missing_quote_windows": _nonnegative(value.get("missing_quote_windows")),
        "qualified_paper": value["qualified_paper"],
        "checks": checks,
        "does_not_prove_real_fills": True,
    }
    if (row["targets"] is None or row["days"] is None
            or row["eligible_quotes"] is None or row["closed_quotes"] is None):
        return None
    # A trade count is a count like every other one: _nonnegative caps
    # it at 10,000,000 (Delivery 4 -- an unbounded int here passed
    # straight into the signed receipt and the collector stored it).
    trades = _nonnegative(value.get("trades"))
    if trades is not None:
        row["trades"] = trades
    gain = _finite_number(value.get("gain"), limit=10)
    if gain is not None:
        row["gain"] = gain
    severe = _finite_number(value.get("severe_lower_bps"), limit=100_000)
    if severe is not None:
        row["severe_lower_bps"] = severe
    if value.get("promotion_status") in ("promoted_paper", "demoted", "held"):
        row["promotion_status"] = value["promotion_status"]
    return row


def _xmarket_paper_scopes(value: object) -> list[dict]:
    """Aggregate the 71 source evaluations to one row per asset/horizon.

    The signed receipt is capped at 80KB, so full per-venue rows cannot be
    published; the aggregate keeps every scope's gate tallies, venue list
    and worst counts, and says so in the UI instead of sampling venues.
    """
    if not isinstance(value, list):
        return []
    grouped: dict[tuple, dict] = {}
    for row in value:
        if not isinstance(row, dict):
            continue
        asset, horizon = row.get("asset"), row.get("horizon_minutes")
        venue = row.get("venue")
        if (asset not in ASSETS or type(horizon) is not int
                or horizon not in HORIZONS or not isinstance(venue, str)
                or re.fullmatch(r"[a-z0-9_-]{2,28}", venue) is None
                or row.get("status") not in ("recorded", "invalid_saved_evidence")
                or type(row.get("fresh")) is not bool
                or type(row.get("qualified_paper")) is not bool):
            continue
        key = (asset, horizon)
        agg = grouped.setdefault(key, {
            "asset": asset, "horizon": horizon, "sources_evaluated": 0,
            "fresh_valid_sources": 0, "qualified_sources": 0,
            "venue_count": 0, "min_targets": None, "min_days": None,
            "min_coverage": None, "failing_checks": {},
            "_venues": set(),
        })
        agg["sources_evaluated"] += 1
        agg["_venues"].add(venue)
        agg["venue_count"] = len(agg["_venues"])
        if row["status"] != "recorded":
            continue
        if row["fresh"]:
            agg["fresh_valid_sources"] += 1
            if row["qualified_paper"]:
                agg["qualified_sources"] += 1
            checks = _paper_checks(row.get("checks"))
            if checks is not None:
                for name, passed in checks.items():
                    if not passed:
                        agg["failing_checks"][name] = (
                            agg["failing_checks"].get(name, 0) + 1)
        targets, days = _nonnegative(row.get("targets")), _nonnegative(row.get("days"))
        coverage = row.get("quote_coverage")
        if targets is not None:
            agg["min_targets"] = targets if agg["min_targets"] is None else min(
                agg["min_targets"], targets)
        if days is not None:
            agg["min_days"] = days if agg["min_days"] is None else min(
                agg["min_days"], days)
        if type(coverage) in (int, float) and 0 <= float(coverage) <= 1:
            agg["min_coverage"] = (float(coverage) if agg["min_coverage"] is None
                                   else min(agg["min_coverage"], float(coverage)))
    out = []
    for agg in sorted(grouped.values(), key=lambda r: (r["asset"], r["horizon"])):
        row = {key: value for key, value in agg.items()
               if not key.startswith("_")}
        row["min_coverage"] = (round(row["min_coverage"], 4)
                               if row["min_coverage"] is not None else None)
        out.append(row)
    return out[:12]


def _paper_proof(value: object) -> dict:
    """Authoritative paper-evaluation gates; zero qualified is a real result."""
    out = {
        "status": "unavailable", "evaluated_scopes": None,
        "fresh_scopes": None, "qualified_paper_scopes": None,
        "best_exact_source": None,
        "xmarket_v1": {"status": "unavailable", "evaluated_scopes": None,
                       "fresh_scopes": None, "qualified_paper_scopes": None,
                       "scopes": [], "scope_handoff": None},
        "does_not_prove_real_fills": True,
    }
    if not isinstance(value, dict):
        return out
    if value.get("status") in ("recorded", "no_evaluations", "unavailable"):
        out["status"] = value["status"]
    out["evaluated_scopes"] = _nonnegative(value.get("evaluated_scopes"))
    out["fresh_scopes"] = _nonnegative(value.get("fresh_scopes"))
    out["qualified_paper_scopes"] = _nonnegative(
        value.get("qualified_paper_scopes"))
    best = _paper_scope_row(value.get("best_exact_source"))
    if best is not None:
        out["best_exact_source"] = best
    xm = value.get("xmarket_v1")
    if isinstance(xm, dict):
        x = out["xmarket_v1"]
        if xm.get("status") in ("recorded", "no_evaluations", "unavailable"):
            x["status"] = xm["status"]
        x["evaluated_scopes"] = _nonnegative(xm.get("evaluated_scopes"))
        x["fresh_scopes"] = _nonnegative(xm.get("fresh_scopes"))
        x["qualified_paper_scopes"] = _nonnegative(
            xm.get("qualified_paper_scopes"))
        x["scopes"] = _xmarket_paper_scopes(xm.get("scopes"))
        handoff = xm.get("scope_handoff")
        if isinstance(handoff, dict) and handoff.get("status") == "recorded":
            # Counts only: per-scope handoff rows would repeat the scope
            # table above inside the 80KB signed receipt.
            x["scope_handoff"] = {
                "fitted_scopes": _nonnegative(handoff.get("fitted_scopes")),
                "issuing_scopes": _nonnegative(handoff.get("issuing_scopes")),
                "issuing_scopes_with_fresh_source_evaluation": _nonnegative(
                    handoff.get("issuing_scopes_with_fresh_source_evaluation")),
                "issuing_scopes_missing_supported_source_evaluation": _nonnegative(
                    handoff.get(
                        "issuing_scopes_missing_supported_source_evaluation")),
                "does_not_prove_edge": True,
            }
    return out


def _model_artifacts(report: object, forecast_status: object,
                     scope_rows: object) -> dict:
    """Exact serving-artifact identities: version plus truncated digests."""
    out = {"status": "unavailable", "incumbent": None, "xmarket": []}
    if not isinstance(report, dict):
        return out
    lineage = report.get("actual_learning")
    if isinstance(lineage, dict) and lineage.get("status") not in (
            "read_unavailable", "unavailable", None):
        version = lineage.get("model_version")
        version = (version if isinstance(version, str)
                   and re.fullmatch(r"[a-z][a-z0-9-]{1,79}", version) else None)
        status = lineage.get("status")
        status = (status if isinstance(status, str) and status in (
            "weight_updates_observed", "no_recent_weight_change_proven",
            "insufficient_comparable_snapshots", "no_lineage") else "unknown")
        lab = lineage.get("lab") if isinstance(lineage.get("lab"), dict) else {}
        lab_status = lab.get("status")
        out["status"] = "recorded"
        out["incumbent"] = {
            "family": "prospective-logistic",
            "model_version": version,
            "serving_status": ("serving" if forecast_status == "fresh"
                               else "unverified"),
            "lineage_status": status,
            "weights_sha256_prefix": _sha_prefix(
                lineage.get("latest_weights_sha256_prefix")),
            "seed_sha256_prefix": _sha_prefix(
                lineage.get("latest_seed_sha256_prefix")),
            "online_training_updates": _nonnegative(
                lineage.get("latest_online_training_updates")),
            "sampled_forecasts": _nonnegative(lineage.get("sampled_forecasts")),
            "sampled_streams": _nonnegative(
                lineage.get("sampled_model_streams")),
            "verified_weight_updates": _nonnegative(
                lineage.get("streams_with_verified_weight_updates")),
            "lab": {
                "status": (lab_status if lab_status in (
                    "not_initialized", "recorded", "no_saved_learners")
                    else "unknown"),
                "sampled_streams": _nonnegative(lab.get("sampled_streams")),
                "streams_with_training_updates": _nonnegative(
                    lab.get("streams_with_training_updates")),
                "latest_checkpoint_at": _iso_timestamp(
                    lab.get("latest_checkpoint_at")),
            },
            "proves_improvement": False,
        }
    for row in scope_rows if isinstance(scope_rows, list) else []:
        if (isinstance(row, dict) and type(row.get("stored_artifacts")) is int
                and row["stored_artifacts"] > 0):
            out["xmarket"].append({
                "asset": row["asset"], "horizon": row["horizon"],
                "artifact_sha256_prefix": _sha_prefix(
                    row.get("latest_artifact_sha256_prefix")),
                "trained_through": _iso_timestamp(
                    row.get("latest_trained_through")),
                "latest_imported_at": _iso_timestamp(
                    row.get("latest_imported_at")),
                "stored_artifacts": row["stored_artifacts"],
            })
    out["xmarket"] = sorted(out["xmarket"],
                            key=lambda r: (r["asset"], r["horizon"]))
    return out


def _research_loop(value: object) -> dict:
    """Model Lab queue: real experiment statuses, never a leaderboard."""
    out = {"status": "unavailable", "enabled": None,
           "drive_folder_ids_present": None, "recent_by_status": {},
           "recent_submitted_to_drive": None}
    if not isinstance(value, dict):
        return out
    if value.get("status") in ("recorded", "unavailable"):
        out["status"] = value["status"]
    if type(value.get("enabled")) is bool:
        out["enabled"] = value["enabled"]
    if type(value.get("drive_folder_ids_present")) is bool:
        out["drive_folder_ids_present"] = value["drive_folder_ids_present"]
    by_status = value.get("recent_by_status")
    if isinstance(by_status, dict):
        for key, count in list(by_status.items())[:16]:
            if (isinstance(key, str)
                    and re.fullmatch(r"[a-z][a-z0-9_-]{2,40}", key)
                    and type(count) is int and 0 <= count <= 16):
                out["recent_by_status"][key] = count
    out["recent_submitted_to_drive"] = _nonnegative(
        value.get("recent_submitted_to_drive"))
    return out


def _xmarket_quotes(value: object) -> dict:
    """Source-exact quote/entry availability per fitted XMarket scope."""
    out = {"status": "unavailable", "window_start": None,
           "captured_at": None, "scopes": [],
           "counts_are_independent_targets": False,
           "does_not_prove_prediction_edge": True}
    if not isinstance(value, dict):
        return out
    if value.get("status") in ("recorded", "no_fitted_scopes", "read_unavailable"):
        out["status"] = value["status"]
    out["window_start"] = _iso_timestamp(value.get("window_start"))
    out["captured_at"] = _iso_timestamp(value.get("captured_at"))
    rows = value.get("scopes")
    if not isinstance(rows, list):
        return out
    grouped: dict[tuple, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset, horizon = row.get("asset"), row.get("horizon_minutes")
        venue, quote = row.get("venue"), row.get("quote")
        if (asset not in ASSETS or type(horizon) is not int
                or horizon not in HORIZONS or not isinstance(venue, str)
                or re.fullmatch(r"[a-z0-9_-]{2,28}", venue) is None
                or not isinstance(quote, str)
                or re.fullmatch(r"[A-Z]{2,8}", quote) is None):
            continue
        key = (asset, horizon)
        agg = grouped.setdefault(key, {
            "asset": asset, "horizon": horizon,
            "forecast_rows": 0, "decisive_forecast_rows": 0,
            "entry_rows": 0, "closed_exit_rows": 0, "missed_exit_rows": 0,
            "elapsed_unmatched_decisive_rows": 0,
            "latest_entry_at": None, "latest_exit_at": None, "venues": [],
        })
        for field in ("forecast_rows", "decisive_forecast_rows", "entry_rows",
                      "closed_exit_rows", "missed_exit_rows",
                      "elapsed_unmatched_decisive_rows"):
            count = _nonnegative(row.get(field))
            if count is not None:
                agg[field] += count
        for source, target in (("latest_entry_at", "latest_entry_at"),
                               ("latest_exit_at", "latest_exit_at")):
            stamp = _iso_timestamp(row.get(source))
            if stamp is not None and (agg[target] is None or stamp > agg[target]):
                agg[target] = stamp
        agg["venues"].append({
            "venue": venue, "quote": quote,
            "entry_rows": _nonnegative(row.get("entry_rows")),
            "closed_exit_rows": _nonnegative(row.get("closed_exit_rows")),
            "missed_exit_rows": _nonnegative(row.get("missed_exit_rows")),
        })
    out["scopes"] = sorted(
        ({**agg, "venues": sorted(agg["venues"], key=lambda v: v["venue"])}
         for agg in grouped.values()),
        key=lambda r: (r["asset"], r["horizon"]))[:5]
    return out


def _xmarket_watchdog(value: object) -> dict:
    """Issuance watchdog: census, boot, silence incidents per scope."""
    out = {"status": "unavailable", "operational_status": "unverified",
           "census_status": None, "scope_checked_at": None,
           "scope_age_seconds": None, "watchdog_checked_at": None,
           "boot_started_at": None, "fitted_scopes": None,
           "issuing_scopes": None, "silent_scopes": None,
           "open_silence_incidents": None, "resolved_silence_incidents": None,
           "scopes": [], "does_not_prove_prediction_edge": True}
    if not isinstance(value, dict):
        return out
    if value.get("status") in ("fresh", "stale_or_boot_unverified",
                               "not_recorded", "read_unavailable"):
        out["status"] = value["status"]
    if value.get("operational_status") in ("healthy", "degraded", "pending",
                                           "unverified"):
        out["operational_status"] = value["operational_status"]
    if value.get("census_status") in ("recorded", "not_initialized"):
        out["census_status"] = value["census_status"]
    out["scope_checked_at"] = _iso_timestamp(value.get("scope_checked_at"))
    out["scope_age_seconds"] = _nonnegative(value.get("scope_age_seconds"))
    out["watchdog_checked_at"] = _iso_timestamp(value.get("watchdog_checked_at"))
    out["boot_started_at"] = _iso_timestamp(value.get("boot_started_at"))
    for field in ("fitted_scopes", "issuing_scopes", "silent_scopes",
                  "open_silence_incidents", "resolved_silence_incidents"):
        out[field] = _nonnegative(value.get(field))
    for row in value.get("scopes", [])[:12]:
        if not isinstance(row, dict):
            continue
        asset, horizon = row.get("asset"), row.get("horizon_minutes")
        if (asset not in ASSETS or type(horizon) is not int
                or horizon not in HORIZONS
                or row.get("status") not in (
                    "invalid_import_clock", "import_grace",
                    "insufficient_incumbents", "issuing", "silent")):
            continue
        out["scopes"].append({
            "asset": asset, "horizon": horizon, "status": row["status"],
            "shadow_rows": _nonnegative(row.get("shadow_rows")),
        })
    out["scopes"] = sorted(out["scopes"],
                           key=lambda r: (r["asset"], r["horizon"]))
    return out


def summarize_host(report: dict | None) -> tuple[dict, dict]:
    """Only persisted, bounded host aggregates, with unknown distinct from 0."""
    host = {
        "status": "unavailable", "forecast_status": "read_unavailable",
        "learning_status": "read_unavailable", "forecast_issued": None,
        "learning_settled": None, "xmarket_fresh_inputs": None,
        "xmarket_expected_inputs": None, "xmarket_shadow_rows": None,
        "xmarket_imported_scopes": None,
        "xmarket_stage": "read_unavailable", "xmarket_exports": None,
        "xmarket_scope_progress": None,
        "model_artifacts": _model_artifacts(None, None, None),
        "research_loop": _research_loop(None),
        "paper_proof": _paper_proof(None),
        "xmarket_quotes": _xmarket_quotes(None),
        "xmarket_watchdog": _xmarket_watchdog(None),
        "kucoin_gap_recovery": {
            "status": "unavailable", "assets_observed": None,
            "queued_exact_gaps_14d": None, "due_exact_gaps_14d": None,
            "attempted_exact_gaps_14d": None,
            "bulk_upstream_empty_assets": None,
        },
        "pending_backlog": {
            "status": "unavailable", "pending_paths_14d": None,
            "due_pending_paths_14d": None, "unique_due_targets_14d": None,
        },
        "cycle_diagnostics": {
            "status": "unavailable", "cycles_24h": None,
            "mean_selected_streams_24h": None,
            "latest_selected_streams": None,
            "mean_target_expired_24h": None,
            "latest_target_expired": None,
            "mean_duplicate_forecasts_24h": None,
            "latest_duplicate_forecasts": None,
            "mean_insufficient_training_24h": None,
            "latest_insufficient_training": None,
            "mean_stale_last_candle_24h": None,
            "latest_stale_last_candle": None,
            "mean_short_history_24h": None,
            "latest_short_history": None,
            "latest_issued_forecasts": None,
        },
        "live_call_delivery": {
            "status": "unavailable", "active_channels": None,
            "recent_accepted_delivery_rows": None,
            "advance_model": None,
        },
        "recent_forward_calls": _recent_forecast_summary(None),
    }
    db = {
        "status": "unavailable", "weight_updates_observed": None,
        "compared_streams": None, "qualified_paper_scopes": None,
        "evaluated_paper_scopes": None,
    }
    if not isinstance(report, dict):
        return host, db
    forecast, learning = report.get("forecast"), report.get("learning")
    forecast = forecast if isinstance(forecast, dict) else {}
    learning = learning if isinstance(learning, dict) else {}
    host["status"] = (
        "verified" if report.get("outcome") == "verified"
        and forecast.get("status") == "fresh"
        and learning.get("status") == "fresh"
        and learning.get("math_ok") is True
        and learning.get("model_review_ok") is True else "unverified"
    )
    host["forecast_status"] = str(forecast.get("status", "read_unavailable"))
    host["learning_status"] = str(learning.get("status", "read_unavailable"))
    host["forecast_issued"] = _nonnegative(forecast.get("issued"))
    host["learning_settled"] = _nonnegative(learning.get("settled"))

    xmarket = report.get("xmarket")
    if isinstance(xmarket, dict) and xmarket.get("status") in XMARKET_STAGES:
        host["xmarket_stage"] = xmarket["status"]
        host["xmarket_exports"] = _export_summary(xmarket.get("export_coverage"))
        host["xmarket_scope_progress"] = _xmarket_scope_progress(
            xmarket.get("scope_progress"))
    if isinstance(xmarket, dict) and xmarket.get("status") != "read_unavailable":
        cov = xmarket.get("source_coverage")
        if isinstance(cov, dict):
            host["xmarket_fresh_inputs"] = _nonnegative(cov.get("fresh_scopes"))
            host["xmarket_expected_inputs"] = _nonnegative(cov.get("expected_scopes"))
        host["xmarket_shadow_rows"] = _nonnegative(xmarket.get("shadow_rows_24h"))
        if isinstance(xmarket.get("imported_scopes"), list):
            host["xmarket_imported_scopes"] = len(xmarket["imported_scopes"])

    # Only safe, bounded aggregate recovery counts leave the private host.
    # A gap queued or attempted is not proof that KuCoin returned the candle.
    paths = report.get("path_targets")
    recovery = paths.get("kucoin_gap_recovery") if isinstance(paths, dict) else None
    if isinstance(recovery, dict) and recovery.get("status") == "recorded":
        assets = recovery.get("assets")
        if isinstance(assets, list) and len(assets) <= 4:
            seen = set()
            valid = True
            for row in assets:
                if (not isinstance(row, dict)
                        or row.get("asset") not in ASSETS
                        or row["asset"] in seen
                        or not all(_nonnegative(row.get(key)) is not None
                                   for key in ("queued_exact_gaps_14d",
                                               "due_exact_gaps_14d",
                                               "attempted_exact_gaps_14d"))):
                    valid = False
                    break
                seen.add(row["asset"])
            if valid:
                host["kucoin_gap_recovery"] = {
                    "status": "recorded",
                    "assets_observed": len(assets),
                    "queued_exact_gaps_14d": sum(
                        row["queued_exact_gaps_14d"] for row in assets),
                    "due_exact_gaps_14d": sum(
                        row["due_exact_gaps_14d"] for row in assets),
                    "attempted_exact_gaps_14d": sum(
                        row["attempted_exact_gaps_14d"] for row in assets),
                    "bulk_upstream_empty_assets": sum(
                        row.get("bulk_status") == "upstream_empty"
                        for row in assets),
                }

    # The pending-paths backlog is a capacity watch, not a performance
    # claim: counts only, never raw rows or identities. A due path is one
    # whose target window has opened; unique targets collapse the two
    # correlated long/short sides of one target into one count. Unknown
    # stays unknown: a missing or negative count leaves the block
    # unavailable instead of publishing a zero.
    if isinstance(paths, dict):
        pending = _nonnegative(paths.get("pending_paths_14d"))
        due = _nonnegative(paths.get("due_pending_paths_14d"))
        unique = _nonnegative(paths.get("unique_due_targets_14d"))
        if pending is not None and due is not None and unique is not None:
            host["pending_backlog"] = {
                "status": "recorded",
                "pending_paths_14d": pending,
                "due_pending_paths_14d": due,
                "unique_due_targets_14d": unique,
            }

    # Forward-cycle stream diagnostics answer whether issuance is
    # stream-limited: 24h means plus the latest cycle row. Counts only,
    # never raw rows. Unknown stays unknown: any missing, negative, or
    # non-finite value leaves the block unavailable instead of publishing
    # a zero or a guess.
    if isinstance(paths, dict):
        diag = paths.get("cycle_diagnostics")
        if isinstance(diag, dict) and diag.get("status") == "recorded":
            mean_keys = ("mean_selected_streams_24h",
                         "mean_target_expired_24h",
                         "mean_duplicate_forecasts_24h",
                         "mean_insufficient_training_24h",
                         "mean_stale_last_candle_24h",
                         "mean_short_history_24h")
            int_keys = ("latest_selected_streams", "latest_target_expired",
                        "latest_duplicate_forecasts",
                        "latest_insufficient_training",
                        "latest_stale_last_candle", "latest_short_history",
                        "latest_issued_forecasts")
            cycles = _nonnegative(diag.get("cycles_24h"))
            means = {}
            for key in mean_keys:
                value = _finite_number(diag.get(key), limit=10_000_000)
                means[key] = value if value is not None and value >= 0 else None
            latest = {key: _nonnegative(diag.get(key)) for key in int_keys}
            if (cycles is not None
                    and all(value is not None for value in means.values())
                    and all(value is not None for value in latest.values())):
                host["cycle_diagnostics"] = {
                    "status": "recorded", "cycles_24h": cycles,
                    **means, **latest,
                }

    # The channel opt-in and API-accepted message receipts are observable,
    # unlike whether a Discord user actually saw the call. Never publish IDs.
    advance = report.get("advance_forecasts")
    if isinstance(advance, dict):
        cards = advance.get("channel_cards")
        delivered = advance.get("recent_deliveries")
        if (isinstance(cards, dict) and cards.get("status") == "recorded"
                and _nonnegative(cards.get("active")) is not None
                and isinstance(delivered, list) and len(delivered) <= 100):
            model = advance.get("model")
            host["live_call_delivery"] = {
                "status": "recorded",
                "active_channels": cards["active"],
                "recent_accepted_delivery_rows": len(delivered),
                "advance_model": (model if isinstance(model, str)
                                  and re.fullmatch(r"[a-z][a-z0-9-]{1,79}", model)
                                  else None),
            }

    host["recent_forward_calls"] = _recent_forecast_summary(
        report.get("recent_forward_calls")
    )
    host["model_artifacts"] = _model_artifacts(
        report, host["forecast_status"], host["xmarket_scope_progress"])
    host["research_loop"] = _research_loop(report.get("research_loop"))
    host["paper_proof"] = _paper_proof(report.get("forward_proof"))
    host["xmarket_quotes"] = _xmarket_quotes(
        report.get("xmarket_quote_progress"))
    host["xmarket_watchdog"] = _xmarket_watchdog(
        report.get("xmarket_watchdog"))
    lineage = report.get("actual_learning")
    if isinstance(lineage, dict) and lineage.get("status") not in (
        "read_unavailable", "unavailable", None
    ):
        db["status"] = "verified_read"
        db["weight_updates_observed"] = _nonnegative(
            lineage.get("streams_with_verified_weight_updates")
        )
        db["compared_streams"] = _nonnegative(lineage.get("streams_with_before_after"))
    paper = report.get("forward_proof")
    if isinstance(paper, dict) and paper.get("status") == "recorded":
        db["qualified_paper_scopes"] = _nonnegative(paper.get("qualified_paper_scopes"))
        db["evaluated_paper_scopes"] = _nonnegative(paper.get("evaluated_scopes"))
    return host, db


def read_host() -> dict | None:
    """Existing authenticated Discloud console action executes only the host's
    already shipped read-only receipt command. Never log the shell response."""
    token, app_id = os.getenv("DISCLOUD_TOKEN"), os.getenv("DISCLOUD_APP_ID")
    if not token or not app_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        return None
    url = "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console"
    raw = _url_json(
        url, method="PUT", body=json.dumps({"command": CMD}).encode("utf-8"),
        headers={"api-token": token, "Content-Type": "application/json"},
        timeout=110, limit=3_000_000,
    )
    stdout = raw.get("apps", {}).get("shell", {}).get("stdout", "")
    return parse_receipt(str(stdout))


def _read_host_console(command: str) -> dict | None:
    token, app_id = os.getenv("DISCLOUD_TOKEN"), os.getenv("DISCLOUD_APP_ID")
    if not token or not app_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        return None
    url = "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console"
    response = _url_json(
        url, method="PUT", body=json.dumps({"command": command}).encode("utf-8"),
        headers={"api-token": token, "Content-Type": "application/json"},
        timeout=110, limit=3_000_000,
    )
    apps = response.get("apps")
    shell = apps.get("shell") if isinstance(apps, dict) else None
    return shell if isinstance(shell, dict) else None


def read_paired_performance() -> dict:
    """Read only a bounded report; no host-side scripts, migrations or writes."""
    unavailable = {"status": "unavailable", "scopes": []}
    shell = _read_host_console(PAIR_CMD)
    if not shell:
        return unavailable
    for line in reversed(str(shell.get("stdout", "")).splitlines()):
        if line.startswith(PAIR_MARKER):
            parsed = json.loads(line[len(PAIR_MARKER):])
            if isinstance(parsed, dict) and parsed.get("status") in (
                "observed", "partial", "unavailable", "no_pairs"
            ) and isinstance(parsed.get("scopes"), list):
                return parsed
    return unavailable


def read_intelligence_host() -> dict:
    """Discloud application status only; 'online' does not prove AI inference."""
    app_id = os.getenv("DISCLOUD_LLM_APP_ID", "")
    token = os.getenv("DISCLOUD_TOKEN", "")
    result = {"status": "unavailable", "inference_verified": False}
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        return result
    url = "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/status"
    response = _url_json(url, headers={"api-token": token}, timeout=18, limit=60_000)
    info = response.get("apps")
    if (response.get("status") != "ok" or not isinstance(info, dict)
            or str(info.get("id", "")) != app_id):
        return result
    normalized = str(info.get("container", "")).strip().lower()
    if normalized in ("online", "offline"):
        result["status"] = normalized
    return result


LLM_METRICS_MARKER = "RIVAL_LLM_METRICS_RECEIPT="
LLM_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,79}:[A-Za-z0-9_.-]{1,40}$")
LLM_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LLM_REPAIR_STATES = {
    "healthy", "warming", "backend_health_check_failed", "cooldown",
    "quarantined", "repairing", "repair_failed",
}
# The gateway's authenticated /v1/status (services/rival-llm/service.py)
# returns model, ready, source_sha, repair_state, repair_count,
# runtime_context_tokens and a cgroup memory snapshot. It does NOT measure
# generation latency or tokens per second, so those stay null unless the
# host probe below emits them. DNS/upstream health and database pressure
# are still not measured by any host probe and stay null.
# The console probe below runs INSIDE the LFM3 container, reads the token
# and port from that container's own environment, and prints only this
# bounded marker. No secret ever leaves the container in the receipt.
#
# Generation probe: after the status read, the same program issues ONE real
# authenticated generation through the gateway's /v1/research route with the
# same synthetic, read-only body the release verifier uses (no historical
# claims, no quotes, no database writes, no orders, no user evidence). The
# response must be the safely-grounded shape (uncertain/low, insufficient
# evidence) or the generation is not counted as verified. Wall-clock
# latency is reported as both p50 and p95 (a single sample per cycle);
# tokens/second is completion_tokens from the gateway's usage block divided
# by that wall clock, and stays null when the backend reports no usage --
# never estimated. This is local LFM3 inference: zero API spend, so the AI
# budget lockdown (no automatic LLM spending) is untouched. Cadence is the
# receipt workflow itself (every 10 minutes): one 140-token generation per
# cycle. A 429 means the single-slot gateway is busy serving the bot, so
# the probe waits 10s and retries once, then reports the failure honestly
# instead of queuing behind bot traffic. A failed generation never poisons
# the status fields: they still publish, only inference_verified is False.
LLM_GENERATION_PROBE_PROGRAM = '''
import json, os, time, urllib.error, urllib.request

port = os.environ.get("RIVAL_LLM_PORT", "8765")
token = os.environ.get("RIVAL_LLM_SHARED_TOKEN", "")
root = "http://127.0.0.1:" + port
headers = {"Authorization": "Bearer " + token}
t0 = time.monotonic()
with urllib.request.urlopen(
    urllib.request.Request(root + "/v1/status", headers=headers),
    timeout=10,
) as resp:
    status = json.loads(resp.read(200000))
    status_http = resp.status
memory = status.get("memory") if isinstance(status.get("memory"), dict) else {}
marker = {
    "model": status.get("model"),
    "ready": status.get("ready"),
    "source_sha": status.get("source_sha"),
    "repair_state": status.get("repair_state"),
    "repair_count": status.get("repair_count"),
    "runtime_context_tokens": status.get("runtime_context_tokens"),
    "memory": memory,
    "status_latency_ms": round((time.monotonic() - t0) * 1000, 1),
    "status_http": status_http,
    "requests_sampled": 1,
    "generation_errors": 0,
}
probe = {"asset": "BTC", "evidence": {
    "asset": "BTC", "analysis": "unvalidated_research",
    "horizon_minutes": 5, "sources": []}}
generation = None
elapsed_s = 0.0
for attempt in (0, 1):
    if attempt:
        time.sleep(10)
    start = time.monotonic()
    req = urllib.request.Request(
        root + "/v1/research",
        data=json.dumps(probe).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req, timeout=45 if attempt == 0 else 30,
        ) as resp:
            generation = json.loads(resp.read(5000))
            elapsed_s = time.monotonic() - start
    except urllib.error.HTTPError as exc:
        if exc.code == 429 and attempt == 0:
            continue
        break
    except (urllib.error.URLError, TimeoutError, ValueError):
        break
    break
verified = (
    isinstance(generation, dict)
    and generation.get("model") == status.get("model")
    and generation.get("direction") == "uncertain"
    and generation.get("confidence") == "low"
    and generation.get("grounding_status") == "insufficient_evidence"
    and generation.get("grounded_by") == "deterministic_historical_evidence_policy"
    and generation.get("informational_only") is True
    and generation.get("uncalibrated") is True
)
if verified:
    marker["inference_verified"] = True
    marker["generation_probe"] = "authenticated_generation"
    marker["latency_ms_p50"] = round(elapsed_s * 1000, 1)
    marker["latency_ms_p95"] = round(elapsed_s * 1000, 1)
    usage = generation.get("usage")
    if isinstance(usage, dict):
        completion = usage.get("completion_tokens")
        if type(completion) is int and completion > 0 and elapsed_s > 0:
            marker["tokens_per_second"] = round(completion / elapsed_s, 3)
else:
    marker["inference_verified"] = False
    marker["generation_errors"] = 1
print("RIVAL_LLM_METRICS_RECEIPT=" + json.dumps(marker, separators=(",", ":")))
'''
# The console runs one shell command; the probe program is shipped base64
# (the same shape as the release verifier in verify_live.py) so the
# readable program above stays reviewable here instead of a one-liner.
# Worst case inside the 110s console timeout: status 10s + generation 45s
# + 10s backoff + retry 30s = 95s.
_LLM_PROBE_B64 = base64.b64encode(
    LLM_GENERATION_PROBE_PROGRAM.encode("utf-8")).decode("ascii")
LLM_METRICS_CMD = (
    "python -c \"import base64;exec(base64.b64decode('"
    + _LLM_PROBE_B64 + "'))\""
)


def _llm_memory(value: object) -> dict | None:
    """Bounded cgroup memory snapshot; unknown fields stay null, never zero."""
    if not isinstance(value, dict):
        return None
    keys = ("current_mb", "limit_mb", "peak_mb", "anon_mb", "file_mb",
            "file_mapped_mb", "gateway_rss_mb", "backend_rss_mb")
    out = {key: _nonnegative(value.get(key)) for key in keys}
    out["oom_kills"] = _nonnegative(value.get("oom_kills"))
    return out


def summarize_llm_metrics(raw: object) -> dict:
    """Validate a host-emitted LFM3 metrics dict; never fabricate health.

    A status-endpoint read proves authenticated backend readiness only.
    ``inference_verified`` stays False unless the host also proves an
    authenticated generation probe (``generation_probe`` marker); the
    current /v1/status probe never sets it. The marker itself is carried
    through in the output (validated to the exact string, else null):
    the board's collector re-checks it before trusting the verified
    flag, so stripping it here leaves the board Unverified even when
    the generation proved. Prompts, tokens, conversations,
    private endpoints and secrets are never accepted fields.
    """
    unavailable = {
        "status": "unavailable", "probe": "host_probe_not_emitted",
        "captured_at": None, "model": None, "ready": None,
        "inference_verified": False, "generation_probe": None,
        "source_sha": None,
        "repair_state": None, "repair_count": None,
        "runtime_context_tokens": None, "memory": None,
        "memory_pressure_pct": None, "status_latency_ms": None,
        "status_http": None, "latency_ms_p50": None,
        "latency_ms_p95": None, "tokens_per_second": None,
        "requests_sampled": None, "generation_errors": None,
        "fallback_count": None, "cpu_pct": None, "dns_ok": None,
        "upstream_ok": None, "db_pressure": None,
    }
    if not isinstance(raw, dict):
        return unavailable
    model = raw.get("model")
    if not isinstance(model, str) or LLM_MODEL_RE.fullmatch(model) is None:
        return unavailable
    ready = raw.get("ready")
    if type(ready) is not bool:
        return unavailable
    out = dict(unavailable)
    out["status"] = "observed"
    out["model"] = model
    out["ready"] = ready
    out["inference_verified"] = bool(
        raw.get("inference_verified") is True
        and raw.get("generation_probe") == "authenticated_generation"
    )
    out["generation_probe"] = (
        "authenticated_generation"
        if raw.get("generation_probe") == "authenticated_generation"
        else None
    )
    sha = raw.get("source_sha")
    out["source_sha"] = sha if isinstance(sha, str) and LLM_SHA_RE.fullmatch(sha) else None
    state = raw.get("repair_state")
    out["repair_state"] = state if state in LLM_REPAIR_STATES else None
    out["repair_count"] = _nonnegative(raw.get("repair_count"))
    out["runtime_context_tokens"] = _nonnegative(
        raw.get("runtime_context_tokens"))
    memory = _llm_memory(raw.get("memory"))
    out["memory"] = memory
    current, limit = (memory or {}).get("current_mb"), (memory or {}).get("limit_mb")
    if (type(current) is int and type(limit) is int and limit > 0):
        out["memory_pressure_pct"] = round(min(current / limit * 100, 1000), 1)
    latency = raw.get("status_latency_ms")
    out["status_latency_ms"] = (
        round(float(latency), 1)
        if type(latency) in (int, float) and 0 <= latency <= 120_000 else None)
    http = raw.get("status_http")
    if http is not None:
        if http != 200:
            return unavailable
        out["status_http"] = 200
    for key, maximum in (("latency_ms_p50", 600_000), ("latency_ms_p95", 600_000)):
        value = raw.get(key)
        out[key] = (round(float(value), 1) if type(value) in (int, float)
                    and 0 <= value <= maximum else None)
    tps = raw.get("tokens_per_second")
    out["tokens_per_second"] = (
        round(float(tps), 3) if type(tps) in (int, float)
        and 0 <= tps <= 100_000 else None)
    for key in ("requests_sampled", "generation_errors", "fallback_count"):
        out[key] = _nonnegative(raw.get(key))
    cpu = raw.get("cpu_pct")
    out["cpu_pct"] = (round(float(cpu), 1) if type(cpu) in (int, float)
                      and 0 <= cpu <= 100 else None)
    for key in ("dns_ok", "upstream_ok"):
        out[key] = raw.get(key) if type(raw.get(key)) is bool else None
    pressure = raw.get("db_pressure")
    out["db_pressure"] = pressure if pressure in (
        "none", "elevated", "saturated") else None
    out["captured_at"] = _iso_timestamp(raw.get("captured_at"))
    return out


def _read_llm_console(command: str) -> dict | None:
    """Run one read-only console command inside the LFM3 container."""
    token, app_id = os.getenv("DISCLOUD_TOKEN"), os.getenv("DISCLOUD_LLM_APP_ID")
    if not token or not app_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        return None
    url = "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console"
    response = _url_json(
        url, method="PUT", body=json.dumps({"command": command}).encode("utf-8"),
        headers={"api-token": token, "Content-Type": "application/json"},
        timeout=110, limit=3_000_000,
    )
    apps = response.get("apps")
    shell = apps.get("shell") if isinstance(apps, dict) else None
    return shell if isinstance(shell, dict) else None


def read_intelligence_metrics() -> dict:
    """Authenticated LFM3 health via the host's own /v1/status probe.

    The publisher never holds the LFM3 bearer token: the console command
    runs inside the container and only the bounded marker line is parsed.
    Until the deployed host emits that marker, every metric is unavailable
    rather than a fabricated healthy value. The same console program also
    issues one real authenticated /v1/research generation per cycle, so a
    verified generation flips inference_verified; a failed generation
    leaves it False while the status fields still publish.
    """
    unavailable = summarize_llm_metrics(None)
    shell = _read_llm_console(LLM_METRICS_CMD)
    if not shell:
        return unavailable
    for line in str(shell.get("stdout", "")).splitlines():
        if not line.startswith(LLM_METRICS_MARKER):
            continue
        try:
            parsed = json.loads(line[len(LLM_METRICS_MARKER):])
        except ValueError:
            return unavailable
        clean = summarize_llm_metrics(parsed)
        if clean["status"] != "observed":
            return unavailable
        clean["captured_at"] = datetime.now(UTC).isoformat()
        clean["probe"] = "llm_console_v1_status"
        return clean
    return unavailable


def _oauth_access_token() -> str:
    parts = {
        "client_id": os.getenv("RIVAL_DRIVE_OAUTH_CLIENT_ID", ""),
        "client_secret": os.getenv("RIVAL_DRIVE_OAUTH_CLIENT_SECRET", ""),
        "refresh_token": os.getenv("RIVAL_DRIVE_OAUTH_REFRESH_TOKEN", ""),
        "grant_type": "refresh_token",
    }
    if not all(parts.values()):
        raise ValueError("drive_credentials_not_configured")
    result = _url_json(
        "https://oauth2.googleapis.com/token", method="POST",
        body=parse.urlencode(parts).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20, limit=50_000,
    )
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("drive_access_token_unavailable")
    return token


def _drive_json(path: str, token: str, *, limit: int = 400_000) -> dict:
    return _url_json(
        "https://www.googleapis.com/drive/v3/" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        timeout=30, limit=limit,
    )


def summarize_xmarket_report(doc: dict, modified_at: str | None,
                             artifact_prefix: str | None = None) -> dict | None:
    """Validate the model contract before making per-scope claims."""
    if not isinstance(doc, dict) or doc.get("schema") != "rival-xmarket-model-v1":
        return None
    asset, horizon = doc.get("asset"), doc.get("horizon_minutes")
    if asset not in ASSETS or horizon not in HORIZONS:
        return None
    checks = doc.get("data_checks")
    checks = checks if isinstance(checks, dict) else {}
    pm = checks.get("polymarket")
    kalshi = checks.get("kalshi")
    pm = pm if isinstance(pm, dict) else {}
    kalshi = kalshi if isinstance(kalshi, dict) else {}
    reason = pm.get("blocked_reason") or kalshi.get("blocked_reason")
    if reason not in XMARKET_REASONS:
        failures = doc.get("data_errors") or []
        reason = (
            "http_429" if isinstance(failures, list) and any(
                isinstance(e, dict) and e.get("failure_code") == "http_429"
                for e in failures
            ) else "unverified"
        )
    status = doc.get("training_status")
    if doc.get("evaluation_status") == "held-out evaluated":
        status = "accepted" if doc.get("gates_passed") is True else "rejected"
        reason = "offline_gates_passed" if status == "accepted" else "offline_gates_failed"
    elif reason == "unverified" and isinstance(doc.get("gate_failures"), list):
        failures = doc["gate_failures"]
        if any(isinstance(f, str) and f.startswith("split too small:") for f in failures):
            reason = "insufficient_training_split"
        elif any(isinstance(f, str) and f.startswith("not enough labelled overlap rows:")
                 for f in failures):
            reason = "insufficient_labelled_rows"
    if status not in ("blocked", "fitted", "accepted", "rejected"):
        status = "unverified"
    window = doc.get("effective_window")
    # A validated source report may identify an actual fitted artifact. A
    # blocked report has a model family but NO model version to claim as fit.
    family = "xmarket_v1" if doc.get("family") == "xmarket_v1" else None
    claimed_version = doc.get("model_version")
    version = (claimed_version if family and status in (
        "fitted", "accepted", "rejected"
    ) and isinstance(claimed_version, str)
        and re.fullmatch(r"xmarket-[a-z0-9-]{1,79}", claimed_version)
        else None)
    failures = doc.get("gate_failures")
    # Four short failures bound the signed receipt (80KB collector cap):
    # 80 chars keeps real failure messages whole ("split too small:
    # train=0 val=0 test=12" is 38 chars) while the 12-report Drive
    # section stays under ~8.5KB even at maximum length.
    gate_failures = ([
        failure[:80] for failure in failures[:4]
        if isinstance(failure, str) and failure
    ] if isinstance(failures, list) else [])
    return {
        "asset": asset, "horizon": horizon, "status": status,
        "family": family, "model_version": version,
        "artifact_sha256_prefix": _sha_prefix(artifact_prefix),
        "gate_failures": gate_failures,
        "pm_status": pm.get("status") if pm.get("status") in (
            "verified", "unverified", "blocked"
        ) else "unknown",
        "kalshi_status": kalshi.get("status") if kalshi.get("status") in (
            "verified", "unverified", "blocked"
        ) else "unknown",
        "pm_pairs": _nonnegative(pm.get("paired_points")),
        "kalshi_pairs": _nonnegative(kalshi.get("paired_points")),
        "eligible_rows": _nonnegative(window.get("rows")) if isinstance(window, dict) else None,
        "gate_passed": doc.get("gates_passed") is True,
        "report_modified_at": modified_at, "reason": reason,
    }


def read_drive() -> dict:
    """Read only the named reports folder, not the Drive root or arbitrary files."""
    out = {
        "status": "unavailable", "reports_considered": None,
        "latest_report_at": None, "reports": [],
    }
    folder = os.getenv("RIVAL_DRIVE_REPORTS_FOLDER_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", folder):
        return out
    token = _oauth_access_token()
    query = f"'{folder}' in parents and trashed = false and name contains 'xmarket-v1-'"
    found: dict[tuple[str, int], dict] = {}
    page_token = ""
    fully_listed = False
    for _ in range(5):
        params = {
            "q": query, "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "100", "orderBy": "modifiedTime desc",
        }
        if page_token:
            params["pageToken"] = page_token
        page = _drive_json("files?" + parse.urlencode(params), token)
        for f in page.get("files", []):
            if not isinstance(f, dict):
                continue
            name = f.get("name")
            match = REPORT_NAME.fullmatch(name) if isinstance(name, str) else None
            if not match or f.get("mimeType") not in ("application/json", "text/plain"):
                continue
            key = (match.group(1), int(match.group(2)))
            if key not in found:
                found[key] = f
        page_token = page.get("nextPageToken") or ""
        if not page_token:
            fully_listed = True
            break
    out["status"] = "verified_metadata" if fully_listed else "unavailable"
    if not fully_listed:
        return out
    out["reports_considered"] = len(found)
    if found:
        out["latest_report_at"] = max(
            (f.get("modifiedTime") or "" for f in found.values()), default=None
        ) or None
    all_parsed = True
    for (asset, horizon), info in sorted(found.items()):
        try:
            file_id = info["id"]
            if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", file_id):
                raise ValueError("invalid_drive_id")
            document = _drive_json(
                "files/" + parse.quote(file_id, safe="") + "?alt=media",
                token, limit=100_000,
            )
            item = summarize_xmarket_report(
                document, info.get("modifiedTime"),
                _artifact_prefix(document))
            if item is None or (item["asset"], item["horizon"]) != (asset, horizon):
                raise ValueError("mismatched_report_identity")
            out["reports"].append(item)
        except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
            all_parsed = False
    if all_parsed and len(out["reports"]) == len(found):
        out["status"] = "verified_reports"
    return out


def _safe_receiver_error(code: int, raw: bytes) -> str:
    """Only fixed labels from our own receiver; never echo raw HTTP bodies."""
    allowed = {
        "unverified_github_identity", "invalid_receipt",
        "authorization_required", "payload_too_large",
        "not_found_or_method_denied",
    }
    reasons = {
        "claim_mismatch", "signature_mismatch", "unsupported_signature",
        "signing_key_unknown", "issuer_keys_unavailable",
        "invalid_token", "malformed_token", "invalid_schema",
        "invalid_capture_timestamp", "invalid_receipt",
    }
    label = f"receiver_http_{int(code)}"
    try:
        body = json.loads(raw[:1500])
        if isinstance(body, dict):
            kind = body.get("error")
            reason = body.get("reason")
            if kind in allowed:
                label += f"_{kind}"
            if reason in reasons:
                label += f"_{reason}"
    except (ValueError, TypeError):
        pass
    return label



def _receiver_http_error(exc: error.HTTPError) -> str:
    """Distinguish receiver JSON denials from a proxy/challenge 403.

    The identity contract is not relaxed, and we never print a response
    body, challenge HTML, bearer token, URL or private source material.
    """
    content_type = str(exc.headers.get("Content-Type", "")).lower() if exc.headers else ""
    code = int(exc.code)
    if "application/json" not in content_type:
        return f"receiver_http_{code}_non_json_upstream"
    raw = exc.read(1500)
    label = _safe_receiver_error(code, raw)
    return label if label != f"receiver_http_{code}" else f"receiver_http_{code}_unrecognized_json"


def receiver_preflight() -> None:
    """Safe, anonymous check on the actual GitHub-runner egress path.

    /health must be served by our intended Worker, and its /ingest route
    must refuse anonymous POST with 401. This is deliberately before
    collecting any private Drive/host reports or requesting an OIDC
    bearer; no permanent data is created by this probe.
    """
    headers = {"Accept": "application/json", "User-Agent": "RivaL-Evidence-Board/1.0"}
    try:
        health = _url_json(
            AUDIENCE + "/health", headers=headers, timeout=12, limit=2048)
    except error.HTTPError as exc:
        raise ValueError("receiver_health_" + _receiver_http_error(exc)) from None
    if health.get("receiver") != "rival-evidence-collector" or health.get("ok") is not True:
        raise ValueError("receiver_health_identity_mismatch")
    try:
        _url_json(INGEST_URL, method="POST", body=b"{}",
                  headers={**headers, "Content-Type": "application/json"},
                  timeout=12, limit=1200)
    except error.HTTPError as exc:
        label = _receiver_http_error(exc)
        if label != "receiver_http_401_authorization_required":
            raise ValueError("receiver_anonymous_" + label) from None
    else:
        raise ValueError("receiver_anonymous_write_not_rejected")
    print("Evidence-board receiver preflight: identity_verified anonymous_write_rejected")


def _oidc_contract_summary(jwt: str) -> str:
    """Compare public GitHub OIDC claim *names/values*, never echo the JWT.

    This is a local diagnostic only, not a signature verification: the
    Cloudflare receiver remains the sole source of authenticated trust.
    """
    import base64

    try:
        encoded = jwt.split(".")[1]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(decoded)
        expected = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": AUDIENCE,
            "repository": "UglyGameFace/RivaL",
            "ref": "refs/heads/main",
            "workflow_ref": (
                "UglyGameFace/RivaL/.github/workflows/"
                "evidence-board-receipt.yml@refs/heads/main"
            ),
            "run_id": os.getenv("GITHUB_RUN_ID", ""),
            "event_name": os.getenv("GITHUB_EVENT_NAME", ""),
        }
        if not isinstance(claims, dict):
            return "local_oidc_invalid"
        missing = [key for key, value in expected.items()
                   if not value or str(claims.get(key, "")) != value]
        return "local_oidc_contract_match" if not missing else (
            "local_oidc_mismatch_" + "_".join(sorted(missing)))
    except (ValueError, KeyError, TypeError, IndexError):
        return "local_oidc_invalid"


def _submit_signed_receipt(payload: dict, jwt: str) -> dict:
    try:
        return _url_json(
            INGEST_URL, method="POST",
            body=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + jwt,
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": "RivaL-Evidence-Board/1.0"},
            timeout=20, limit=15000,
        )
    except error.HTTPError as exc:
        # Only allowlisted diagnostics escape. The on-runner claim check
        # neither verifies nor weakens the receiver's RSA signature gate.
        label = _receiver_http_error(exc)
        if exc.code == 403:
            label += "_" + _oidc_contract_summary(jwt)
        raise ValueError(label) from None


def _github_oidc() -> str:
    url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    token = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url.startswith("https://") or not token:
        raise ValueError("github_oidc_not_available")
    joiner = "&" if "?" in url else "?"
    answer = _url_json(
        url + joiner + parse.urlencode({"audience": AUDIENCE}),
        headers={"Authorization": "Bearer " + token}, timeout=15, limit=20000,
    )
    value = answer.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError("github_oidc_token_missing")
    return value


def classify_read_error(exc: BaseException) -> tuple[str, str | None]:
    """Map one upstream read failure to a publishable (status, reason).

    Only fixed labels leave the runner. Service unreachable, auth failure,
    stale data, no records, failed read and genuinely measured zero stay
    distinct: an auth failure must never be reported as "unavailable" with
    no further detail, and a failed read must never become a zero.
    """
    if isinstance(exc, ValueError) and str(exc) in READ_ERROR_LABELS:
        return READ_ERROR_LABELS[str(exc)]
    if isinstance(exc, error.HTTPError):
        if exc.code in (401, 403):
            return "auth_failed", f"auth_http_{exc.code}"
        if exc.code == 429:
            return "failed_read", "rate_limited_http_429"
        if exc.code >= 500:
            return "unreachable", "upstream_http_5xx"
        return "failed_read", "upstream_http_error"
    if isinstance(exc, TimeoutError):
        return "unreachable", "timeout"
    if isinstance(exc, (error.URLError, OSError)):
        return "unreachable", "network_error"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "failed_read", "invalid_response"
    return "failed_read", "read_failed"


def _source_detail(status: str, reason: str | None = None,
                   checked_at: str | None = None) -> dict:
    return {
        "status": status if status in SOURCE_STATUSES else "failed_read",
        "reason": reason if reason is None or ATTEMPT_REASON.fullmatch(reason) else "read_failed",
        "checked_at": _iso_timestamp(checked_at) or datetime.now(UTC).isoformat(),
    }


def _error_detail(exc: BaseException | None, checked_at: str) -> dict:
    if exc is None:
        return _source_detail("failed_read", "read_failed", checked_at)
    status, reason = classify_read_error(exc)
    return _source_detail(status, reason, checked_at)


def _collection_block(*, coalesced: bool = False) -> dict:
    """Provenance for this collection attempt: trigger, run, attempt.

    The collector stores this with the signed receipt, so the dashboard can
    show the last scheduled attempt separately from the last successful
    signed publication, and a failure reason when they differ.
    """
    event = os.getenv("GITHUB_EVENT_NAME", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    attempt = os.getenv("GITHUB_RUN_ATTEMPT", "")
    return {
        "event": event if event in COLLECTION_EVENTS else "unknown",
        "run_id": run_id if re.fullmatch(r"\d{5,18}", run_id) else None,
        "run_attempt": (int(attempt) if attempt.isdigit()
                        and 1 <= int(attempt) <= 100 else None),
        "attempted_at": datetime.now(UTC).isoformat(),
        "coalesced": coalesced,
    }


def _recent_receipt_is_fresh(max_age_seconds: int = COALESCE_FRESH_SECONDS) -> bool:
    """True when the collector already serves a receipt younger than the window.

    Public GET-only read of the signed snapshot. Any failure fails open to a
    full collection: a broken freshness check must never suppress evidence.
    """
    try:
        latest = _url_json(COLLECTOR_LATEST_URL, timeout=8, limit=20_000)
        at = _iso_timestamp(latest.get("captured_at"))
        if not at:
            return False
        age = (datetime.now(UTC) - datetime.fromisoformat(at)).total_seconds()
        return 0 <= age < max_age_seconds
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        return False


def _report_attempt(jwt: str, outcome: str, reason: str | None) -> None:
    """Best-effort signed attempt record (failed / coalesced runs).

    Uses the same GitHub OIDC identity as a receipt, so only a real
    main-branch run can claim an attempt; the collector never lets an
    attempt overwrite the last good snapshot. Failures here are swallowed:
    attempt reporting must never turn a failed run into a second failure.
    """
    if outcome not in ATTEMPT_OUTCOMES or not jwt:
        return
    block = _collection_block(coalesced=outcome == "coalesced")
    body = {"schema_version": 1, "kind": "collection-attempt",
            "event": block["event"], "run_id": block["run_id"],
            "run_attempt": block["run_attempt"],
            "attempted_at": block["attempted_at"], "outcome": outcome,
            "reason": reason if reason is None or ATTEMPT_REASON.fullmatch(reason) else "read_failed"}
    try:
        _url_json(
            INGEST_ATTEMPT_URL, method="POST",
            body=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + jwt,
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": "RivaL-Evidence-Board/1.0"},
            timeout=15, limit=5_000,
        )
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        pass


def _attempt_reason(exc: BaseException) -> str:
    label = str(exc)
    if label.startswith(("receiver_http_", "oidc_provider_http_",
                         "receiver_health_", "receiver_anonymous_")) \
            and len(label) <= 80 and ATTEMPT_REASON.fullmatch(label):
        return label
    return "publication_failed"


def _host_source_details(host: dict, db: dict, errors: dict,
                         checked_at: str) -> dict:
    """Per-source status for the Discloud host and PostgreSQL reads.

    Forecast/learning freshness comes from the host receipt itself, so a
    stale forecast is reported stale (not failed), and a host read with no
    recorded forecast is no_records (not zero, not failed).
    """
    forecast_status = host.get("forecast_status")
    learning_status = host.get("learning_status")
    forecast = {
        "fresh": ("ok", None),
        "stale_or_clock_invalid": ("stale", "stale_or_clock_invalid"),
        "not_recorded": ("no_records", "no_records_found"),
    }.get(forecast_status, ("failed_read", "host_field_unavailable"))
    learning = {
        "fresh": ("ok", None),
        "stale_or_clock_invalid": ("stale", "stale_or_clock_invalid"),
        "not_recorded": ("no_records", "no_records_found"),
    }.get(learning_status, ("failed_read", "host_field_unavailable"))
    details = {
        "discloud_forecast": _source_detail(*forecast, checked_at),
        "discloud_learning": _source_detail(*learning, checked_at),
    }
    if "host" in errors:
        host_error = _error_detail(errors["host"], checked_at)
        details["discloud_forecast"] = host_error
        details["discloud_learning"] = host_error
    if db.get("status") == "verified_read":
        details["postgres"] = _source_detail("ok", None, checked_at)
    elif "database" in errors:
        details["postgres"] = _error_detail(errors["database"], checked_at)
    else:
        details["postgres"] = _source_detail(
            "no_records", "no_records_found", checked_at)
    return details


def _drive_source_detail(drive: dict, errors: dict, checked_at: str) -> dict:
    if drive.get("status") in ("verified_reports", "verified_metadata"):
        if drive.get("reports_considered") == 0:
            return _source_detail("no_records", "no_records_found", checked_at)
        return _source_detail("ok", None, checked_at)
    if "drive" in errors:
        return _error_detail(errors["drive"], checked_at)
    folder = os.getenv("RIVAL_DRIVE_REPORTS_FOLDER_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", folder):
        return _source_detail("not_configured", "credentials_not_configured", checked_at)
    return _source_detail("failed_read", "incomplete_listing", checked_at)


def _snapshot_source_detail(value: dict, errors: dict, key: str,
                            checked_at: str, *, ok_status: str) -> dict:
    """GitHub / Intelligence detail from a snapshot dict."""
    if value.get("status") == ok_status:
        return _source_detail("ok", None, checked_at)
    if key in errors:
        return _error_detail(errors[key], checked_at)
    if key in ("github", "intelligence") and not os.getenv("GITHUB_TOKEN", ""):
        return _source_detail("not_configured", "credentials_not_configured", checked_at)
    return _source_detail("failed_read", "read_failed", checked_at)


def build_source_details(host: dict, db: dict, drive: dict, github: dict,
                         intelligence: dict, intelligence_host: dict,
                         performance: dict, errors: dict,
                         checked_at: str | None = None) -> dict:
    """One reliability entry per upstream, each with its freshness bar."""
    at = checked_at or datetime.now(UTC).isoformat()
    details = _host_source_details(host, db, errors, at)
    details["drive"] = _drive_source_detail(drive, errors, at)
    details["github"] = _snapshot_source_detail(
        github, errors, "github", at, ok_status="verified")
    details["intelligence"] = _snapshot_source_detail(
        intelligence, errors, "intelligence", at, ok_status="verified")
    host_status = intelligence_host.get("status")
    if host_status in ("online", "offline"):
        # Offline is a genuine observation of the Intelligence app, not a
        # failed read; the value itself carries the bad news.
        details["intelligence_host"] = _source_detail("ok", None, at)
    elif "intelligence_host" in errors:
        details["intelligence_host"] = _error_detail(errors["intelligence_host"], at)
    elif not os.getenv("DISCLOUD_TOKEN", "") or not os.getenv("DISCLOUD_LLM_APP_ID", ""):
        details["intelligence_host"] = _source_detail(
            "not_configured", "credentials_not_configured", at)
    else:
        details["intelligence_host"] = _source_detail("failed_read", "read_failed", at)
    if performance.get("status") in ("observed", "partial"):
        details["performance"] = _source_detail("ok", None, at)
    elif performance.get("status") == "no_pairs":
        details["performance"] = _source_detail(
            "measured_zero", "no_pairs_measured", at)
    elif "performance" in errors:
        details["performance"] = _error_detail(errors["performance"], at)
    else:
        details["performance"] = _source_detail("failed_read", "read_failed", at)
    for name in SOURCE_NAMES:
        details[name]["freshness_seconds"] = SOURCE_FRESHNESS_SECONDS[name]
    return details


def publish() -> int:
    checked_at = datetime.now(UTC).isoformat()
    event = os.getenv("GITHUB_EVENT_NAME", "")
    # Redundant flood triggers coalesce onto a receipt younger than 5 min
    # instead of re-hitting Discloud, Drive and the receiver. Schedule runs
    # always collect: they are the cadence backbone.
    if event in ("workflow_run", "push") and _recent_receipt_is_fresh():
        try:
            jwt = _github_oidc()
        except (error.HTTPError, error.URLError, OSError, TimeoutError,
                ValueError, KeyError, TypeError):
            jwt = ""
        if jwt:
            _report_attempt(jwt, "coalesced", None)
        print(json.dumps({"publication": "coalesced",
                          "reason": "recent_receipt_within_300s",
                          "source": "read-only",
                          "no_trades_or_db_writes": True}, sort_keys=True))
        return 0
    errors: dict[str, BaseException] = {}
    host_report: dict | None = None
    try:
        host_report = read_host()
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["host"] = exc
    try:
        host, db = summarize_host(host_report)
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["host"] = exc
        host, db = summarize_host(None)
    try:
        drive = read_drive()
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["drive"] = exc
        drive = {"status": "unavailable", "reports_considered": None,
                 "latest_report_at": None, "reports": []}
    try:
        github = read_github_snapshot(os.getenv("GITHUB_TOKEN", ""))
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["github"] = exc
        github = {"status": "unavailable", "fetched_at": None,
                  "prs": [], "runs": []}
    try:
        intelligence = read_public_intelligence(os.getenv("GITHUB_TOKEN", ""))
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["intelligence"] = exc
        intelligence = {"status": "unavailable", "prs": [], "runs": []}
    try:
        intelligence_host = read_intelligence_host()
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["intelligence_host"] = exc
        intelligence_host = {"status": "unavailable", "inference_verified": False}
    try:
        intelligence_metrics = read_intelligence_metrics()
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        intelligence_metrics = summarize_llm_metrics(None)
    try:
        performance = read_paired_performance()
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        errors["performance"] = exc
        performance = {"status": "unavailable", "scopes": []}
    if host_report is None:
        # read_host returns None only when Discloud credentials are absent
        # or malformed; keep that distinct from a network/auth failure.
        errors["host"] = ValueError("host_credentials_not_configured")
    now = datetime.now(UTC).isoformat()
    sources = build_source_details(
        host, db, drive, github, intelligence, intelligence_host,
        performance, errors, checked_at)
    payload = {
        "schema_version": 1, "captured_at": now,
        "collection": _collection_block(),
        "sources": sources,
        "host": host, "database": db, "drive": drive,
        "github": github, "intelligence": intelligence,
        "intelligence_host": intelligence_host,
        "intelligence_metrics": intelligence_metrics,
        "performance": performance,
    }
    try:
        jwt = _github_oidc()
    except error.HTTPError as exc:
        raise ValueError(f"oidc_provider_http_{int(exc.code)}") from None
    try:
        result = _submit_signed_receipt(payload, jwt)
    except ValueError as exc:
        _report_attempt(jwt, "failed", _attempt_reason(exc))
        raise
    if result.get("status") not in ("accepted", "stale_ignored"):
        _report_attempt(jwt, "failed", "receipt_not_accepted")
        raise ValueError("receipt_not_accepted")
    # The only logged output is a bounded status. Never print provider output.
    print(json.dumps({
        "publication": result["status"], "host": host["status"],
        "database": db["status"], "drive": drive["status"],
        "drive_scopes": drive["reports_considered"],
        "github": github["status"], "intelligence": intelligence["status"],
        "intelligence_metrics": intelligence_metrics["status"],
        "llm_inference_verified": intelligence_metrics["inference_verified"],
        "performance": performance["status"],
        "sources": {name: details["status"]
                    for name, details in sources.items()},
        "source": "read-only", "no_trades_or_db_writes": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(publish())
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        name = str(exc) if (isinstance(exc, ValueError)
                            and str(exc).startswith(("oidc_provider_http_",
                                                     "receiver_http_",
                                                     "receiver_health_",
                                                     "receiver_anonymous_"))) else type(exc).__name__
        print("Evidence-board publication unavailable:", name)
        sys.exit(1)
