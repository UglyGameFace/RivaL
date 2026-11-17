"""Read-only production proof of the persisted forecast and learning cycles.

No model inference, market orders, table creation or schema migration. The
receipts are read through the same functions used by the Discord scorecard.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from parlay_bot.config import Settings
from parlay_bot.discord_app.home import _performance_cycles
from parlay_bot.perpetuals.advance_forecasts import read_advance_receipt
from parlay_bot.perpetuals.dashboard_forward_receipt import read_forward_calls
from parlay_bot.perpetuals.family_quotes import SUPPORTED_QUOTE_SOURCES
from parlay_bot.perpetuals.proof_progress import proof_progress
from parlay_bot.perpetuals.xmarket_quote_receipt import read_xmarket_quotes
from parlay_bot.perpetuals.xmarket_watchdog_receipt import read_xmarket_watchdog
from parlay_bot.storage.hot import PostgresHotStore

MARKER = "RIVAL_LEARNING_CYCLE_RECEIPT="
INCUMBENT_MODEL_VERSION = "prospective-logistic-persistent-v3"


def _sha_prefix(value: object) -> str | None:
    """A 12-hex-char artifact identifier; never a full digest or weights."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value[:12].lower()
    return None


def _iso_or_none(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.astimezone(UTC).isoformat() if stamp.utcoffset() is not None else None


def _status(value, now: datetime) -> dict:
    if value is None:
        return {"status": "not_recorded"}
    if value.get("status") != "recorded":
        return {"status": "read_unavailable"}
    age = int((now - value["checked_at"].astimezone(UTC)).total_seconds())
    result = {
        "status": "fresh" if 0 <= age <= 1200 else "stale_or_clock_invalid",
        "age_seconds": age,
        "issued": value["issued"],
    }
    if "math_ok" in value:
        result["math_ok"] = value["math_ok"]
        result["model_review_ok"] = value["model_review_ok"]
        result["settled"] = value["settled"]
    else:
        result["expired"] = value["expired"]
    return result


def _future_proof(store, now: datetime) -> dict:
    """Read only the authoritative source-specific 21-day paper proof rows.

    No SQL writes, model inference, synthetic quote fills or invented results.
    A missing, stale or invalid family evaluation never counts as qualified.
    """
    try:
        snapshots = []
        xmarket_scopes = []
        unavailable = 0
        for venue, currency in sorted(SUPPORTED_QUOTE_SOURCES):
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                result = proof_progress(store, asset=asset, venue=venue, now=now)
                unavailable += int(result["status"] == "unavailable")
                for item in result["evaluations"]:
                    if item.get("quote") != currency:
                        continue
                    # Each source's persisted promotion verdict contains every
                    # candidate, not merely the highest-ranked family. Expose
                    # XMarket's own 21-day/quote/cost gates without rerunning
                    # governance or touching the forecast/quote ledgers.
                    xmarket = item.get("xmarket_v1")
                    if isinstance(xmarket, dict):
                        scoped = {
                            "asset": asset, "venue": venue, "quote": currency,
                            "horizon_minutes": item["horizon"],
                            "evaluated_at": item.get("evaluated_at"),
                            "evaluation_age_seconds": item.get("age_seconds"),
                            "fresh": bool(item["fresh"]),
                            "status": ("recorded" if xmarket.get("valid") is True
                                       else "invalid_saved_evidence"),
                            "qualified_paper": bool(
                                item["fresh"] and xmarket.get("qualified_paper")
                                and xmarket.get("valid") is True
                            ),
                        }
                        if xmarket.get("valid") is True:
                            scoped.update({
                                "targets": xmarket["targets"],
                                "days": xmarket["days"],
                                "eligible_quotes": xmarket["eligible_quotes"],
                                "closed_quotes": xmarket["closed_quotes"],
                                "quote_coverage": xmarket["quote_coverage"],
                                "missing_quote_windows": xmarket["missing_quote_windows"],
                                "checks": dict(xmarket["checks"]),
                            })
                            # Optional scoring detail, only when the saved
                            # verdict actually carries it; never defaulted.
                            if type(xmarket.get("trades")) is int:
                                scoped["trades"] = xmarket["trades"]
                            for key in ("gain", "severe_lower_bps"):
                                value = xmarket.get(key)
                                if type(value) in (int, float):
                                    scoped[key] = value
                        if item.get("promotion_status") in (
                                "promoted_paper", "demoted", "held"):
                            scoped["promotion_status"] = item["promotion_status"]
                        xmarket_scopes.append(scoped)
                    candidate = item["candidate"]
                    if candidate is None or not candidate.get("valid"):
                        continue
                    snapshot = {
                        "asset": asset, "venue": venue, "quote": currency,
                        "horizon_minutes": item["horizon"],
                        "fresh": item["fresh"],
                        "targets": candidate["targets"],
                        "days": candidate["days"],
                        "eligible_quotes": candidate["eligible_quotes"],
                        "closed_quotes": candidate["closed_quotes"],
                        "quote_coverage": candidate["quote_coverage"],
                        "qualified_paper": item["paper_qualified"],
                        "checks": candidate["checks"],
                    }
                    if type(candidate.get("trades")) is int:
                        snapshot["trades"] = candidate["trades"]
                    for key in ("gain", "severe_lower_bps"):
                        value = candidate.get(key)
                        if type(value) in (int, float):
                            snapshot[key] = value
                    if item.get("promotion_status") in (
                            "promoted_paper", "demoted", "held"):
                        snapshot["promotion_status"] = item["promotion_status"]
                    snapshots.append(snapshot)
        xmarket_proof = {
            "status": ("recorded" if xmarket_scopes else
                       "unavailable" if unavailable else "no_evaluations"),
            "evaluated_scopes": len(xmarket_scopes),
            "fresh_scopes": sum(
                row["fresh"] and row["status"] == "recorded"
                for row in xmarket_scopes
            ),
            "qualified_paper_scopes": sum(
                row["qualified_paper"] for row in xmarket_scopes
            ),
            "scopes": sorted(xmarket_scopes, key=lambda row: (
                row["asset"], row["horizon_minutes"], row["venue"],
            )),
            "does_not_prove_real_fills": True,
        }
        if snapshots:
            # Report an actual single exact-source candidate, never mix the
            # best days of one venue with the quotes of another candidate.
            best = max(snapshots, key=lambda r: (
                int(r["qualified_paper"]), int(r["fresh"]),
                r["days"], r["targets"], r["closed_quotes"], r["venue"],
                r["asset"], r["horizon_minutes"],
            ))
            return {
                "status": "recorded",
                "evaluated_scopes": len(snapshots),
                "fresh_scopes": sum(r["fresh"] for r in snapshots),
                "qualified_paper_scopes": sum(r["qualified_paper"] for r in snapshots),
                "best_exact_source": best,
                "xmarket_v1": xmarket_proof,
                "does_not_prove_real_fills": True,
            }
        return {
            "status": "unavailable" if unavailable else "no_evaluations",
            "evaluated_scopes": 0, "fresh_scopes": 0,
            "qualified_paper_scopes": 0, "best_exact_source": None,
            "xmarket_v1": xmarket_proof,
            "does_not_prove_real_fills": True,
        }
    except Exception:  # noqa: BLE001 - optional research cannot mask a fresh heartbeat
        return {
            "status": "unavailable", "evaluated_scopes": 0,
            "fresh_scopes": 0, "qualified_paper_scopes": 0,
            "best_exact_source": None, "xmarket_v1": {
                "status": "unavailable", "evaluated_scopes": 0,
                "fresh_scopes": 0, "qualified_paper_scopes": 0,
                "scopes": [], "does_not_prove_real_fills": True,
            }, "does_not_prove_real_fills": True,
        }




def _xmarket_forward_handoff(progress: dict, source_proof: dict) -> dict:
    """Reconcile issued scopes with stored source-exact cost evaluations.

    This is a read-only correspondence check, not a second cost evaluator.
    Unsupported quote venues and absent/stale source evaluations remain
    UNKNOWN, never zero percent coverage or a failed trading result.
    """
    scope_rows = progress.get("scope_progress")
    evaluated = source_proof.get("scopes")
    if (source_proof.get("status") == "unavailable"
            or not isinstance(scope_rows, list)
            or not isinstance(evaluated, list)):
        return {"status": "unavailable", "does_not_prove_edge": True}

    fitted = [
        row for row in scope_rows
        if int(row.get("stored_gate_passing_artifacts") or 0) > 0
    ]
    reported = []
    for fitted_scope in fitted:
        asset = fitted_scope["asset"]
        horizon = int(fitted_scope["horizon_minutes"])
        issued = int(fitted_scope.get("shadow_rows_24h") or 0)
        sources = [
            row for row in evaluated
            if row.get("asset") == asset
            and row.get("horizon_minutes") == horizon
        ]
        fresh_valid = [
            row for row in sources
            if row.get("fresh") is True and row.get("status") == "recorded"
        ]
        if not issued:
            status = "not_issuing"
        elif fresh_valid:
            status = "source_evaluation_recorded"
        elif sources:
            status = "stale_or_invalid_source_evaluation"
        else:
            status = "no_supported_source_evaluation"
        reported.append({
            "asset": asset,
            "horizon_minutes": horizon,
            "issued_24h": issued,
            "status": status,
            "source_evaluations": len(sources),
            "fresh_valid_source_evaluations": len(fresh_valid),
            "quote_gate_passing_sources": sum(
                row.get("checks", {}).get("quote_coverage") is True
                for row in fresh_valid
            ),
            "qualified_paper_sources": sum(
                row.get("qualified_paper") is True for row in fresh_valid
            ),
        })
    reported.sort(key=lambda row: (row["asset"], row["horizon_minutes"]))
    return {
        "status": "recorded",
        "fitted_scopes": len(reported),
        "issuing_scopes": sum(row["issued_24h"] > 0 for row in reported),
        "issuing_scopes_with_fresh_source_evaluation": sum(
            row["issued_24h"] > 0 and row["fresh_valid_source_evaluations"] > 0
            for row in reported
        ),
        "issuing_scopes_missing_supported_source_evaluation": sum(
            row["status"] == "no_supported_source_evaluation" for row in reported
        ),
        "scopes": reported,
        "does_not_prove_edge": True,
    }


def _actual_learning_progress(store, now: datetime) -> dict:
    """Check stored *changes*, not whether the job heartbeat said 'learning'.

    A lineage record proves which weights actually issued a forecast. A
    growing per-stream training counter AND changed weight digest across
    two observed forecasts is stronger evidence that the online learner
    updated. It still does not demonstrate better out-of-sample accuracy.
    Query the existing issued-at index and bound the comparison to recent
    records; never fit a model or write data from this monitoring job.
    """
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,contract,quote,horizon_minutes,issued_at,
                          online_training_updates,weights_sha256,
                          historical_seed_sha256
                   FROM rival_forward_model_lineage
                   WHERE issued_at>=? AND issued_at<=?
                   ORDER BY issued_at DESC LIMIT 400""",
                ((now - timedelta(hours=6)).isoformat(), now.isoformat()),
            ).fetchall()
            # The Lab has its own persisted online learner checkpoint.
            # Check table existence before an optional read: a missing-table
            # error would abort PostgreSQL's current transaction.
            if store.database.backend == "postgres":
                exists = conn.execute(
                    "SELECT to_regclass('rival_lab_learners') AS name"
                ).fetchone()
            else:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='rival_lab_learners'"
                ).fetchone()
            lab = (conn.execute(
                """SELECT updates,trained_at FROM rival_lab_learners
                   ORDER BY trained_at DESC LIMIT 100"""
            ).fetchall() if exists and exists["name"] else None)
    except Exception:  # noqa: BLE001 - old/unavailable lineage table
        return {"status": "read_unavailable", "proves_improvement": False}

    newest: dict[tuple, object] = {}
    compared: set[tuple] = set()
    changed: set[tuple] = set()
    for row in rows:  # latest first, for each unique model stream
        key = (row["venue"], row["contract"], row["quote"], int(row["horizon_minutes"]))
        head = newest.setdefault(key, row)
        if head is row:
            continue
        compared.add(key)
        if (int(head["online_training_updates"]) > int(row["online_training_updates"])
                and head["weights_sha256"] != row["weights_sha256"]):
            changed.add(key)
    snapshots = list(newest.values())
    lab_status = ({"status": "not_initialized"} if lab is None
                  else {"status": "recorded" if lab else "no_saved_learners",
                        "sampled_streams": len(lab),
                        "streams_with_training_updates":
                        sum(int(row["updates"]) > 0 for row in lab),
                        "latest_checkpoint_at": lab[0]["trained_at"] if lab else None})
    latest = snapshots[0] if snapshots else None
    return {
        "status": ("weight_updates_observed" if changed else
                   "no_recent_weight_change_proven" if compared else
                   "insufficient_comparable_snapshots" if rows else "no_lineage"),
        # The lineage rows that issued these forecasts identify the exact
        # serving artifact: model family/version plus a truncated weight
        # digest and seed digest. A digest prefix identifies the artifact;
        # it is not the weights and cannot reconstruct them.
        "model_version": INCUMBENT_MODEL_VERSION,
        "latest_weights_sha256_prefix": (
            _sha_prefix(latest["weights_sha256"]) if latest is not None else None),
        "latest_seed_sha256_prefix": (
            _sha_prefix(latest["historical_seed_sha256"])
            if latest is not None else None),
        "sampled_forecasts": len(rows),
        "sampled_model_streams": len(newest),
        "streams_with_before_after": len(compared),
        "streams_with_verified_weight_updates": len(changed),
        "sampled_historically_seeded_forecasts":
            sum(bool(row["historical_seed_sha256"]) for row in rows),
        "latest_online_training_updates":
            max((int(row["online_training_updates"]) for row in snapshots), default=None),
        "lab": lab_status,
        "proves_improvement": False,
        "proves_profitable_edge": False,
    }


def read_receipt() -> dict:
    settings = Settings()
    if settings.rival_storage_backend != "postgres" or settings.rival_database_url is None:
        return {"outcome": "unverified", "reason": "postgres_not_configured"}
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    try:
        now = datetime.now(UTC)
        cycles = _performance_cycles(SimpleNamespace(store=store))
        forecast = _status(cycles["forecast"], now)
        learning = _status(cycles["learning"], now)
        both_fresh = (
            forecast["status"] == "fresh"
            and learning["status"] == "fresh"
            and learning.get("math_ok") is True
            and learning.get("model_review_ok") is True
        )
        xmarket = _xmarket_progress(store, now)
        forward_proof = _future_proof(store, now)
        forward_proof["xmarket_v1"]["scope_handoff"] = (
            _xmarket_forward_handoff(xmarket, forward_proof["xmarket_v1"])
        )
        return {
            "outcome": "verified" if both_fresh else "unverified",
            "forecast": forecast,
            "learning": learning,
            "actual_learning": _actual_learning_progress(store, now),
            "recent_forward_calls": read_forward_calls(store, now),
            "advance_forecasts": read_advance_receipt(store, now),
            "forward_proof": forward_proof,
            "path_targets": _path_target_progress(store, now),
            "polymarket_contest": _contest_progress(store, now),
            "xmarket": xmarket,
            "xmarket_watchdog": read_xmarket_watchdog(store, now),
            "xmarket_quote_progress": read_xmarket_quotes(store, now, xmarket),
            "research_loop": _research_progress(store, settings),
            "runtime_diagnostics": _runtime_diagnostics(store, now),
            "does_not_prove_prediction_edge": True,
        }
    finally:
        store.close()




def _path_settlement_sample(store, now: datetime) -> dict:
    """Diagnose a bounded sample of due paths without changing their evidence.

    The old and newest pending source paths are sampled per horizon, not the
    whole backlog. One long/short pair is ONE source path. These observations
    are deliberately not a replacement for the worker's full causal checks.
    """
    import math
    from collections import Counter

    stamp = now.astimezone(UTC)
    reasons: Counter[str] = Counter()
    examples = []
    sampled = set()
    try:
        with store.connect() as conn:
            for horizon in (15, 60):
                for order in ("ASC", "DESC"):
                    # state/target_opened_at is indexed. The LIMIT applies
                    # before the source reads, so a six-figure backlog never
                    # becomes an unbounded diagnostic query.
                    candidates = conn.execute(
                        """SELECT venue,contract,quote,horizon_minutes,side,
                                  reference_opened_at,issued_at,target_opened_at,
                                  reference_close,sigma_horizon
                           FROM rival_forward_paths
                           WHERE state='pending' AND horizon_minutes=?
                             AND target_opened_at>=? AND target_opened_at<=?
                           ORDER BY target_opened_at """ + order + " LIMIT 4",
                        (horizon, (stamp-timedelta(days=14)).isoformat(),
                         (stamp-timedelta(minutes=5)).isoformat()),
                    ).fetchall()
                    for path in candidates:
                        key = (path["venue"], path["contract"], path["quote"],
                               horizon, path["reference_opened_at"],
                               path["issued_at"], path["target_opened_at"])
                        if key in sampled:
                            continue
                        sampled.add(key)
                        reason = "ready_for_settlement"
                        missing_candle_opens: list[str] = []
                        try:
                            reference = datetime.fromisoformat(
                                str(path["reference_opened_at"])).astimezone(UTC)
                            issued = datetime.fromisoformat(
                                str(path["issued_at"])).astimezone(UTC)
                            target = datetime.fromisoformat(
                                str(path["target_opened_at"])).astimezone(UTC)
                            steps = horizon // 5
                            if (not issued < reference+timedelta(minutes=10)
                                    or target != reference+timedelta(minutes=5*(steps+1))
                                    or issued > stamp):
                                reason = "invalid_forecast_clock"
                            else:
                                bars = conn.execute(
                                    """SELECT opened_at,closed_at,first_observed_at,
                                              high_price,low_price,close_price
                                       FROM perp_market_candles
                                       WHERE venue=? AND contract=? AND quote=?
                                         AND interval_minutes=5 AND opened_at>?
                                         AND opened_at<=?
                                       ORDER BY opened_at""",
                                    (path["venue"],path["contract"],path["quote"],
                                     (reference+timedelta(minutes=5)).isoformat(),
                                     target.isoformat()),
                                ).fetchall()
                                if len(bars) != steps:
                                    # Supply the exact source/time gaps for archival
                                    # repair. This read-only observer does not fetch,
                                    # backdate, rewrite or settle missing outcomes.
                                    expected = {
                                        reference + timedelta(minutes=5*(index+2))
                                        for index in range(steps)
                                    }
                                    observed = {
                                        datetime.fromisoformat(
                                            str(bar["opened_at"])).astimezone(UTC)
                                        for bar in bars
                                    }
                                    missing_candle_opens = [
                                        stamp.isoformat()
                                        for stamp in sorted(expected - observed)
                                    ]
                                    reason = ("missing_source_candles" if
                                              missing_candle_opens else
                                              "source_grid_mismatch")
                                else:
                                    for index, bar in enumerate(bars):
                                        opened = datetime.fromisoformat(
                                            str(bar["opened_at"])).astimezone(UTC)
                                        closed = datetime.fromisoformat(
                                            str(bar["closed_at"])).astimezone(UTC)
                                        observed = datetime.fromisoformat(
                                            str(bar["first_observed_at"])).astimezone(UTC)
                                        if opened != reference+timedelta(minutes=5*(index+2)):
                                            reason = "source_grid_mismatch"
                                        elif observed <= issued:
                                            reason = "source_seen_before_issuance"
                                        elif observed > stamp:
                                            reason = "source_not_yet_observed"
                                        elif closed >= stamp:
                                            reason = "source_not_yet_closed"
                                        elif closed > observed:
                                            reason = "source_seen_before_close"
                                        elif not all(
                                            math.isfinite(float(bar[field]))
                                            for field in ("high_price","low_price","close_price")
                                        ) or float(bar["close_price"]) <= 0:
                                            reason = "invalid_source_prices"
                                        if reason != "ready_for_settlement":
                                            break
                                    if reason == "ready_for_settlement":
                                        ref_price = float(path["reference_close"])
                                        sigma = float(path["sigma_horizon"])
                                        if not (math.isfinite(ref_price) and ref_price > 0
                                                and math.isfinite(sigma) and sigma > 0):
                                            reason = "invalid_forecast_values"
                        except (ValueError, TypeError, OverflowError, KeyError):
                            reason = "invalid_source_or_forecast_values"
                        reasons[reason] += 1
                        examples.append({
                            "venue": str(path["venue"]),
                            "contract": str(path["contract"]),
                            "quote": str(path["quote"]),
                            "horizon_minutes": horizon,
                            "target_opened_at": str(path["target_opened_at"]),
                            "reason": reason,
                            "missing_candle_opened_at": missing_candle_opens,
                        })
        return {
            "status": "sampled" if sampled else "no_due_pending_paths",
            "sampled_unique_paths": len(sampled),
            "by_reason": dict(sorted(reasons.items())),
            "examples": examples,
            "sample_only_not_backlog_census": True,
            "sides_are_correlated": True,
            "read_only": True,
        }
    except Exception:  # noqa: BLE001 - optional, bounded diagnostic only
        return {"status": "read_unavailable", "sample_only_not_backlog_census": True}



def _kucoin_gap_recovery_progress(store, now: datetime) -> dict:
    """Read independent KuCoin source-gap retries, not guessed path wins.

    A missing bulk page and a missing singleton path candle use separate
    backoffs. A successful deployment must show a source row and declining
    due paths before anyone can call the repair accepted in production.
    """
    try:
        with store.connect() as conn:
            positions = conn.execute(
                """SELECT asset,status,next_opened_at,retry_after,attempted_at
                   FROM perp_history_recovery WHERE venue='kucoin'
                     AND asset IN ('BTC','ETH','SOL','XRP')
                   ORDER BY asset"""
            ).fetchall()
            gaps = conn.execute(
                """SELECT asset,COUNT(*) AS queued,
                          SUM(CASE WHEN attempted_at IS NOT NULL THEN 1 ELSE 0 END)
                              AS attempted,
                          SUM(CASE WHEN retry_after<=? THEN 1 ELSE 0 END) AS due,
                          MIN(opened_at) AS oldest_opened_at
                   FROM perp_history_gap_repair WHERE venue='kucoin'
                     AND asset IN ('BTC','ETH','SOL','XRP')
                     AND opened_at>=? AND opened_at<=?
                   GROUP BY asset""",
                (now.isoformat(), (now-timedelta(days=14)).isoformat(),
                 now.isoformat()),
            ).fetchall()
        by_asset = {r["asset"]: dict(r) for r in gaps}
        rows = []
        for position in positions:
            asset = position["asset"]
            gap = by_asset.get(asset, {})
            rows.append({
                "asset": asset,
                "bulk_status": position["status"],
                "bulk_next_opened_at": position["next_opened_at"],
                "bulk_retry_after": position["retry_after"],
                "last_bulk_attempt_at": position["attempted_at"],
                "queued_exact_gaps_14d": int(gap.get("queued") or 0),
                "attempted_exact_gaps_14d": int(gap.get("attempted") or 0),
                "due_exact_gaps_14d": int(gap.get("due") or 0),
                "oldest_exact_gap_at": gap.get("oldest_opened_at"),
            })
        return {
            "status": "recorded" if rows else "not_initialized",
            "assets": rows, "read_only": True,
            "source_candles_not_independent_targets": True,
            "does_not_prove_gap_recovery": True,
        }
    except Exception:  # noqa: BLE001 - optional read must not hide healthy cycles
        return {
            "status": "read_unavailable", "assets": [], "read_only": True,
            "does_not_prove_gap_recovery": True,
        }


def _path_target_progress(store, now: datetime) -> dict:
    """Bounded read-only check of independently scored path target heads.

    Never initialize a table or infer successful model performance from a
    settlement count. Some hosts may not have restarted with the new schema.
    """
    name = "rival_forward_path_targets"
    try:
        with store.connect() as conn:
            if store.database.backend == "postgres":
                exists = conn.execute(
                    "SELECT to_regclass(?) AS name", (name,),
                ).fetchone()["name"]
            else:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()
                exists = row["name"] if row else None
            if not exists:
                return {"status": "not_initialized", "shadow_only": True}
            rows = conn.execute(
                """SELECT horizon_minutes,COUNT(*) AS settled,
                          COUNT(DISTINCT venue || ':' || contract || ':' || quote)
                              AS source_scopes,MAX(settled_at) AS latest_settled_at
                   FROM rival_forward_path_targets
                   WHERE state='settled' AND target_opened_at>=?
                     AND target_opened_at<=? AND settled_at<=?
                   GROUP BY horizon_minutes ORDER BY horizon_minutes""",
                ((now-timedelta(days=1)).isoformat(),now.isoformat(),now.isoformat()),
            ).fetchall()
            # Count the issuing ledger separately from its close-only output
            # heads. Zero settled heads alone cannot distinguish no issuance
            # from a blocked settlement worker. Existing state/target index;
            # raw rows include correlated sides and are not independent proof.
            lifecycle = conn.execute(
                """SELECT horizon_minutes,
                          SUM(CASE WHEN issued_at>=? THEN 1 ELSE 0 END) AS issued_24h,
                          SUM(CASE WHEN state IN ('settled','ambiguous')
                                    AND settled_at>=? AND settled_at<=?
                                   THEN 1 ELSE 0 END) AS resolved_24h,
                          SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending_14d,
                          SUM(CASE WHEN state='pending' AND target_opened_at<=?
                                   THEN 1 ELSE 0 END) AS due_pending_14d,
                          MIN(CASE WHEN state='pending' AND target_opened_at<=?
                                   THEN target_opened_at END) AS oldest_due_target_at,
                          MAX(issued_at) AS latest_issued_at
                   FROM rival_forward_paths
                   WHERE state IN ('pending','settled','ambiguous')
                     AND target_opened_at>=? AND target_opened_at<=? AND issued_at<=?
                   GROUP BY horizon_minutes ORDER BY horizon_minutes""",
                ((now-timedelta(days=1)).isoformat(),
                 (now-timedelta(days=1)).isoformat(),now.isoformat(),
                 (now-timedelta(minutes=5)).isoformat(),
                 (now-timedelta(minutes=5)).isoformat(),
                 (now-timedelta(days=14)).isoformat(),
                 (now+timedelta(minutes=65)).isoformat(),now.isoformat()),
            ).fetchall()
        issued = sum(int(row["issued_24h"]) for row in lifecycle)
        due = sum(int(row["due_pending_14d"]) for row in lifecycle)
        oldest = [row["oldest_due_target_at"] for row in lifecycle
                  if row["oldest_due_target_at"] is not None]
        return {
            "status": ("observed" if rows else "settlement_backlog" if due
                       else "waiting_for_forward_outcomes" if issued
                       else "no_recent_issuance"),
            "horizons": [dict(row) for row in rows],
            "settled_close_targets_24h": sum(int(row["settled"]) for row in rows),
            "path_forecasts_issued_24h": issued,
            # Parent paths can predate the close-only companion heads. Show
            # actual processing separately from recent-target qualification;
            # draining old barriers alone does not validate current heads.
            "resolved_path_sides_24h": sum(int(row["resolved_24h"]) for row in lifecycle),
            "pending_paths_14d": sum(int(row["pending_14d"]) for row in lifecycle),
            "due_pending_paths_14d": due,
            "oldest_due_target_at": min(oldest) if oldest else None,
            "lifecycle_by_horizon": [dict(row) for row in lifecycle],
            "settlement_sample": (_path_settlement_sample(store, now) if due else
                                  {"status": "no_due_pending_paths",
                                   "sampled_unique_paths": 0, "read_only": True}),
            "kucoin_gap_recovery": _kucoin_gap_recovery_progress(store, now),
            "counts_are_independent_targets": False,
            "read_only": True,
            "does_not_prove_prediction_edge": True,
            "after_cost_qualified": False,
            "shadow_only": True,
        }
    except Exception:  # noqa: BLE001 - optional observational receipt
        return {"status": "read_unavailable", "shadow_only": True}


def _runtime_diagnostics(store, now: datetime) -> dict:
    """Existing durable aggregates only; the receipt never collects plans or writes."""
    try:
        with store.connect() as conn:
            rows = conn.execute("""SELECT component,checked_at,payload_json
                FROM rival_runtime_diagnostics
                WHERE component IN ('database','research_loop','model_evaluation',
                                    'quote_streams')""").fetchall()
        result = {}
        for row in rows:
            result[row['component']] = {
                'checked_at': row['checked_at'],
                'age_seconds': max(0, int((now - datetime.fromisoformat(row['checked_at']))
                                         .total_seconds())),
                'evidence': json.loads(row['payload_json']),
            }
        return {'status': 'recorded' if result else 'not_recorded', 'components': result}
    except Exception:  # noqa: BLE001 - old deployments have not created the table yet
        return {'status': 'unavailable'}


def _research_progress(store, settings) -> dict:
    result = {
        "enabled": bool(getattr(settings, "rival_research_loop_enabled", False)),
        "drive_folder_ids_present": bool(
            getattr(settings, "rival_drive_models_folder_id", None)
            and getattr(settings, "rival_drive_reports_folder_id", None)
        ),
    }
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT status, outcome_json FROM rival_research_experiments
                   WHERE proposer='scheduled' ORDER BY created_at DESC LIMIT 16"""
            ).fetchall()
        by_status = {}
        submitted = 0
        for row in rows:
            status = row["status"]
            by_status[status] = by_status.get(status, 0) + 1
            submitted += any(event.get("stage") == "offline-request"
                             for event in json.loads(row["outcome_json"]))
        return {**result, "status": "recorded", "recent_by_status": by_status,
                "recent_submitted_to_drive": submitted}
    except Exception:  # noqa: BLE001 - an optional receipt cannot mask worker health
        return {**result, "status": "unavailable"}


def _contest_progress(store, now: datetime) -> dict:
    """Read actual new-policy capture activity, without inference or writes."""
    from parlay_bot.perpetuals.model_league import FORMULA, POLICY

    try:
        with store.connect() as conn:
            policy = conn.execute("SELECT started_at FROM rival_poly_contest_policies WHERE policy=?",
                                  (POLICY,)).fetchone()
            if policy is None:
                return {"status": "not_started"}
            rows = conn.execute(
                "SELECT r.asset,r.horizon,COUNT(DISTINCT r.slug) AS captured,"
                "MAX(r.issued_at) AS latest_at,"
                "COUNT(DISTINCT CASE WHEN f.model IS NOT NULL THEN r.slug END) AS with_forecasts,"
                "COUNT(DISTINCT CASE WHEN f.model=? THEN r.slug END) AS formula_forecasts,"
                "COUNT(DISTINCT d.slug) AS pinned_for_display,"
                "COUNT(DISTINCT proof.slug) AS registered_cost_proofs "
                "FROM rival_poly_contest_rounds r "
                "LEFT JOIN rival_poly_contest_forecasts f ON f.slug=r.slug "
                "LEFT JOIN rival_poly_contest_display d ON d.slug=r.slug "
                "LEFT JOIN rival_poly_contest_cost_proof proof "
                "ON proof.slug=r.slug AND proof.issued_at=r.issued_at "
                "AND proof.evidence_sha256=r.evidence_sha256 "
                "WHERE r.issued_at>=? AND r.issued_at<=? GROUP BY r.asset,r.horizon ORDER BY r.asset,r.horizon",
                (FORMULA, policy["started_at"], now.isoformat()),
            ).fetchall()
        return {"status": "recorded", "policy": POLICY, "started_at": policy["started_at"],
                "captured": sum(r["captured"] for r in rows),
                "with_forecasts": sum(r["with_forecasts"] for r in rows),
                "registered_cost_proofs": sum(r["registered_cost_proofs"] for r in rows),
                "unverified_cost_contracts": sum(
                    int(r["captured"]) - int(r["registered_cost_proofs"]) for r in rows),
                "scopes": [dict(r) for r in rows],
                "does_not_prove_prediction_edge": True,
                "does_not_prove_executed_trades": True,
                "proof_rows_rechecked_by_contest_at_scoring": True}
    except Exception:  # noqa: BLE001 - absent optional tables cannot mask worker health
        return {"status": "unavailable"}


def _xmarket_progress(store, now: datetime) -> dict:
    """Read-only cold-start chain: captured inputs -> imported fit -> actual rows.

    Inspect table names before optional reads, so a pre-upgrade host does
    not generate missing-table errors or cause migrations from a probe.
    """
    needed = ("rival_xmarket_observations", "rival_xmarket_models",
              "rival_model_shadow_forecasts", "rival_xmarket_exports")
    try:
        with store.connect() as conn:
            if store.database.backend == "postgres":
                tables = {r["table_name"] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
                    "AND table_name IN (?,?,?,?)", needed).fetchall()}
            else:
                tables = {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)", needed).fetchall()}
            if set(needed) - tables:
                return {"status": "not_initialized", "shadow_only": True}
            since = (now - timedelta(days=1)).isoformat()
            inputs = conn.execute("SELECT asset,source,COUNT(*) AS points,MAX(at) AS latest_source_at "
                "FROM rival_xmarket_observations WHERE asset IN ('BTC','ETH','SOL','XRP') "
                "AND source IN ('polymarket','kalshi') AND at>=? GROUP BY asset,source", (since,)).fetchall()
            fits = conn.execute("SELECT asset,horizon_minutes,model_sha256,"
                "trained_through,imported_at "
                "FROM rival_xmarket_models WHERE gates_passed=1 "
                "ORDER BY imported_at DESC LIMIT 60").fetchall()
            # One bounded read identifies the latest imported artifact per
            # scope (12-char digest prefix + training window end). The full
            # digest and the private Drive file ID never leave the host.
            artifact_by_scope: dict = {}
            for row in fits:
                key = (row["asset"], int(row["horizon_minutes"]))
                entry = artifact_by_scope.setdefault(key, {
                    "asset": row["asset"], "horizon_minutes": key[1],
                    "imported_artifacts": 0, "latest_imported_at": None,
                    "latest_artifact_sha256_prefix": None,
                    "latest_trained_through": None,
                })
                entry["imported_artifacts"] += 1
                imported = row["imported_at"]
                if (entry["latest_imported_at"] is None
                        or str(imported) >= str(entry["latest_imported_at"])):
                    entry["latest_imported_at"] = imported
                    entry["latest_artifact_sha256_prefix"] = _sha_prefix(
                        row["model_sha256"])
                    entry["latest_trained_through"] = _iso_or_none(
                        row["trained_through"])
            fits = [{"asset": entry["asset"],
                     "horizon_minutes": entry["horizon_minutes"],
                     "imported_artifacts": entry["imported_artifacts"],
                     "latest_imported_at": entry["latest_imported_at"]}
                    for entry in artifact_by_scope.values()]
            rows = conn.execute("SELECT asset,horizon_minutes,COUNT(*) AS issued,MAX(issued_at) AS latest_at "
                "FROM rival_model_shadow_forecasts WHERE asset IN ('BTC','ETH','SOL','XRP') "
                "AND horizon_minutes IN (5,15,60) AND family='xmarket_v1' AND issued_at>=? "
                "GROUP BY asset,horizon_minutes", (since,)).fetchall()
            exports = conn.execute(
                "SELECT asset,COUNT(*) AS exported_days,MAX(day) AS latest_exported_day "
                "FROM rival_xmarket_exports WHERE asset IN ('BTC','ETH','SOL','XRP') "
                "GROUP BY asset").fetchall()
            # Mirror XmarketInputs._pending_days' three-hour day-close cutoff.
            # Scan only a bounded recent window in the read-only host receipt.
            cutoff = (now - timedelta(hours=3)).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat()
            lookback = (now - timedelta(days=3)).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat()
            pending = conn.execute(
                "SELECT i.asset,COUNT(DISTINCT SUBSTR(i.at,1,10)) AS days "
                "FROM rival_xmarket_observations i "
                "LEFT JOIN rival_xmarket_exports e "
                "ON e.asset=i.asset AND e.day=SUBSTR(i.at,1,10) "
                "WHERE i.asset IN ('BTC','ETH','SOL','XRP') "
                "AND i.at>=? AND i.at<? AND e.day IS NULL GROUP BY i.asset",
                (lookback, cutoff)).fetchall()
        count = sum(int(r["issued"]) for r in rows)
        expected = {(asset, source) for asset in ("BTC", "ETH", "SOL", "XRP")
                    for source in ("polymarket", "kalshi")}
        present = {(r["asset"], r["source"]) for r in inputs}
        fresh = {(r["asset"], r["source"]) for r in inputs
                 if 0 <= (now - datetime.fromisoformat(r["latest_source_at"]))
                 .total_seconds() <= 1200}
        def scopes(values):
            return [{"asset": asset, "source": source} for asset, source in sorted(values)]
        pending_days = sum(int(r["days"]) for r in pending)
        # One global "shadow_issuing" can conceal eleven inactive horizons.
        # Reuse the already bounded, read-only aggregate queries to expose
        # each of the 12 asset/horizon handoffs without model documents,
        # private Drive IDs, inferred predictions, or further DB requests.
        fit_by_scope = artifact_by_scope
        shadow_by_scope = {(r["asset"], int(r["horizon_minutes"])): r for r in rows}
        pending_by_asset = {r["asset"]: int(r["days"]) for r in pending}
        scope_progress = []
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            fresh_sources = {source for a, source in fresh if a == asset}
            present_sources = {source for a, source in present if a == asset}
            for horizon in (5, 15, 60):
                fit = fit_by_scope.get((asset, horizon))
                shadow = shadow_by_scope.get((asset, horizon))
                stored = int(fit["imported_artifacts"]) if fit else 0
                issued = int(shadow["issued"]) if shadow else 0
                status = (
                    "shadow_issuing" if issued else
                    "imported_no_shadow_rows" if stored else
                    "waiting_for_completed_day_export"
                    if len(fresh_sources) == 2 and pending_by_asset.get(asset, 0) else
                    "waiting_for_fit" if len(fresh_sources) == 2 else
                    "waiting_for_inputs"
                )
                scope_progress.append({
                    "asset": asset, "horizon_minutes": horizon, "status": status,
                    # Presence of a validated historical import is not proof
                    # that the currently pinned model is usable or profitable.
                    "stored_gate_passing_artifacts": stored,
                    "shadow_rows_24h": issued,
                    "latest_imported_at": fit["latest_imported_at"] if fit else None,
                    "latest_artifact_sha256_prefix": (
                        fit["latest_artifact_sha256_prefix"] if fit else None),
                    "latest_trained_through": (
                        fit["latest_trained_through"] if fit else None),
                    "latest_shadow_at": shadow["latest_at"] if shadow else None,
                    "fresh_native_sources": len(fresh_sources),
                    "expected_native_sources": 2,
                    "missing_native_sources": sorted(
                        {"polymarket", "kalshi"} - present_sources),
                    "stale_native_sources": sorted(present_sources - fresh_sources),
                    "proves_live_edge": False,
                })
        return {"status": ("shadow_issuing" if count else "imported_no_shadow_rows" if fits
                           else "waiting_for_completed_day_export" if fresh == expected and pending_days
                           else "waiting_for_fit" if fresh == expected else "waiting_for_inputs"),
                "input_sources_24h": [dict(r) for r in inputs], "imported_scopes": [dict(r) for r in fits],
                "source_coverage": {"expected_scopes": len(expected), "present_scopes": len(present),
                                    "fresh_scopes": len(fresh), "freshness_seconds": 1200,
                                    "missing": scopes(expected - present), "stale": scopes(present - fresh)},
                "shadow_rows_24h": count, "shadow_scopes": [dict(r) for r in rows],
                "scope_progress": scope_progress,
                "export_coverage": {"published": [dict(r) for r in exports],
                                    "recent_completed_days_pending": pending_days,
                                    "pending_by_asset": [dict(r) for r in pending],
                                    "recent_window_days": 3,
                                    "eligible_before": cutoff,
                                    "does_not_prove_independent_drive_restore": True},
                "shadow_only": True, "does_not_prove_prediction_edge": True}
    except Exception:  # noqa: BLE001 - optional probe never masks the worker receipt
        return {"status": "read_unavailable", "shadow_only": True}


def main() -> int:
    try:
        report = read_receipt()
    except Exception as exc:  # noqa: BLE001 - never disclose DSN/SQL
        report = {"outcome": "unverified", "reason": "host_read_exception",
                  "error_class": type(exc).__name__}
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0 if report["outcome"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
