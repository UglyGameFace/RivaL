"""Read-only production proof of the persisted forecast and learning cycles.

No model inference, market orders, table creation or schema migration. The
receipts are read through the same functions used by the Discord scorecard.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from parlay_bot.config import Settings
from parlay_bot.discord_app.home import _performance_cycles
from parlay_bot.perpetuals.family_quotes import SUPPORTED_QUOTE_SOURCES
from parlay_bot.perpetuals.proof_progress import proof_progress
from parlay_bot.storage.hot import PostgresHotStore

MARKER = "RIVAL_LEARNING_CYCLE_RECEIPT="


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
        unavailable = 0
        for venue, currency in sorted(SUPPORTED_QUOTE_SOURCES):
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                result = proof_progress(store, asset=asset, venue=venue, now=now)
                unavailable += int(result["status"] == "unavailable")
                for item in result["evaluations"]:
                    candidate = item["candidate"]
                    if candidate is None or not candidate.get("valid"):
                        continue
                    if item["quote"] != currency:
                        continue
                    snapshots.append({
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
                    })
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
                "does_not_prove_real_fills": True,
            }
        return {
            "status": "unavailable" if unavailable else "no_evaluations",
            "evaluated_scopes": 0, "fresh_scopes": 0,
            "qualified_paper_scopes": 0, "best_exact_source": None,
            "does_not_prove_real_fills": True,
        }
    except Exception:  # noqa: BLE001 - optional research cannot mask a fresh heartbeat
        return {
            "status": "unavailable", "evaluated_scopes": 0,
            "fresh_scopes": 0, "qualified_paper_scopes": 0,
            "best_exact_source": None, "does_not_prove_real_fills": True,
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
        return {
            "outcome": "verified" if both_fresh else "unverified",
            "forecast": forecast,
            "learning": learning,
            "forward_proof": _future_proof(store, now),
            "polymarket_contest": _contest_progress(store, now),
            "research_loop": _research_progress(store, settings),
            "does_not_prove_prediction_edge": True,
        }
    finally:
        store.close()


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
                "COUNT(DISTINCT d.slug) AS pinned_for_display "
                "FROM rival_poly_contest_rounds r "
                "LEFT JOIN rival_poly_contest_forecasts f ON f.slug=r.slug "
                "LEFT JOIN rival_poly_contest_display d ON d.slug=r.slug "
                "WHERE r.issued_at>=? AND r.issued_at<=? GROUP BY r.asset,r.horizon ORDER BY r.asset,r.horizon",
                (FORMULA, policy["started_at"], now.isoformat()),
            ).fetchall()
        return {"status": "recorded", "policy": POLICY, "started_at": policy["started_at"],
                "captured": sum(r["captured"] for r in rows),
                "with_forecasts": sum(r["with_forecasts"] for r in rows),
                "scopes": [dict(r) for r in rows], "does_not_prove_prediction_edge": True}
    except Exception:  # noqa: BLE001 - absent optional tables cannot mask worker health
        return {"status": "unavailable"}


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
