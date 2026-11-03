"""Read-only, source-exact progress toward prospective model proof.

The authoritative family evaluator persists its 30-day verdict separately.
This screen only reads that verdict; it cannot promote, train, settle or
reconstruct missing quotes from historical candles.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_governance import (
    FRESH_FOR,
    MIN_COVERAGE,
    MIN_DAYS,
    MIN_TRADES,
    MIN_WINDOWS,
)


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _candidate_summary(name: str, row: dict, *, selected: bool) -> dict:
    """Do not turn a stale/corrupt saved summary into a passing model."""
    try:
        targets = int(row["targets"])
        days = int(row["days"])
        eligible = int(row["eligible_quotes"])
        closed = int(row["closed_quotes"])
        trades = int(row["challenger_trades"])
        invalid = int(row["invalid_rows"])
        coverage = _finite(row["quote_coverage"])
        severe = row["cost_proof"]["severe"]
        conservative = row["cost_proof"]["conservative"]
        gain = _finite(row["forward_gain"])
        calibrated = row["calibration_status"] == "research_screen_passed"
        if (min(targets, days, eligible, closed, trades, invalid) < 0
                or coverage is None or not 0 <= coverage <= 1
                or eligible < closed or closed < trades
                or abs(coverage - (closed / eligible if eligible else 0)) > 1e-7
                or not isinstance(row["qualified"], bool)):
            raise ValueError("invalid saved proof counters")
        severe_ok = bool(severe["qualified"]) and (
            _finite(severe["candidate_net_lower_bps"]) is not None
            and _finite(severe["candidate_net_lower_bps"]) > 0
            and _finite(severe["paired_lower_bps"]) is not None
            and _finite(severe["paired_lower_bps"]) > 0
        )
        conservative_ok = bool(conservative["qualified"])
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return {"name": name, "valid": False, "reason": "Stored evidence is invalid"}
    checks = {
        "future_outcomes": targets >= MIN_WINDOWS and days >= MIN_DAYS,
        "beats_baselines": gain is not None and gain > 0 and calibrated,
        "quote_coverage": (
            eligible >= MIN_WINDOWS and closed >= MIN_WINDOWS
            and coverage >= MIN_COVERAGE and trades >= MIN_TRADES
        ),
        "after_cost_paper": severe_ok and conservative_ok,
        "valid_records": invalid == 0,
    }
    qualified = selected and row["qualified"] and all(checks.values())
    return {
        "name": name, "valid": True, "targets": targets, "days": days,
        "eligible_quotes": eligible, "closed_quotes": closed,
        "quote_coverage": coverage, "trades": trades, "gain": gain,
        "severe_lower_bps": _finite(severe["candidate_net_lower_bps"]),
        "checks": checks, "qualified_paper": qualified,
    }


def proof_progress(store, *, asset: str, venue: str,
                   now: datetime | None = None) -> dict:
    """Report latest recorded exact-source promotion evaluations, fail closed."""
    if asset not in ("BTC", "ETH", "SOL", "XRP") or not venue:
        raise ValueError("unsupported proof scope")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("proof clock requires timezone")
    stamp = stamp.astimezone(UTC)
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,contract,quote,horizon_minutes,evaluated_at,
                          selected_family,status,evidence_json
                   FROM rival_family_promotion
                   WHERE asset=? AND venue=?
                   ORDER BY evaluated_at DESC LIMIT 60""",
                (asset, venue),
            ).fetchall()
    except Exception:  # noqa: BLE001 - optional table may not exist on old hosts
        return {"status": "unavailable", "evaluations": [],
                "reason": "No readable model-proof evaluation table"}
    evaluations = []
    horizons = set()
    for record in rows:
        horizon = int(record["horizon_minutes"])
        if horizon in horizons or horizon not in (5, 15, 60):
            continue
        horizons.add(horizon)
        try:
            evaluated = datetime.fromisoformat(str(record["evaluated_at"]))
            if evaluated.utcoffset() is None:
                raise ValueError("unverified timestamp")
            age = stamp - evaluated.astimezone(UTC)
            current = timedelta(0) <= age <= FRESH_FOR
            evidence = json.loads(record["evidence_json"])
            if not isinstance(evidence, dict):
                raise ValueError("invalid evidence document")
            selected_name = str(record["selected_family"] or "")
            candidates = [
                _candidate_summary(
                    str(name), value,
                    selected=(current and record["status"] == "promoted_paper"
                              and selected_name == name),
                )
                for name, value in evidence.items()
                if isinstance(value, dict)
            ]
            candidates.sort(
                key=lambda x: (
                    not x.get("qualified_paper", False),
                    -x.get("targets", 0), -x.get("days", 0), x["name"],
                )
            )
            candidate = candidates[0] if candidates else None
            if candidate is None:
                reason = "No eligible challenger evaluated"
            elif not candidate["valid"]:
                reason = "Saved evaluation is invalid"
            elif not current:
                reason = "Evaluation expired or timestamp is invalid"
            elif candidate["qualified_paper"]:
                reason = "Source-exact practice-only research proof passed"
            else:
                missing = [label.replace("_", " ") for label, ok
                           in candidate["checks"].items() if not ok]
                reason = "Still missing: " + (", ".join(missing) or "approval")
            evaluations.append({
                "horizon": horizon, "venue": str(record["venue"]),
                "contract": str(record["contract"]), "quote": str(record["quote"]),
                "fresh": current, "candidate": candidate, "reason": reason,
                "paper_qualified": bool(candidate and candidate.get("qualified_paper")),
            })
        except (ValueError, TypeError, KeyError, OverflowError):
            evaluations.append({
                "horizon": horizon, "venue": venue, "fresh": False,
                "candidate": None, "reason": "Evaluation evidence cannot be verified",
                "paper_qualified": False,
            })
    evaluations.sort(key=lambda x: x["horizon"])
    return {
        "status": "recorded" if evaluations else "no_evaluations",
        "evaluations": evaluations,
        "reason": ("Exact-source forward evaluations" if evaluations else
                   "No source-specific model qualification has been recorded"),
    }


def proof_progress_text(proof: dict) -> str:
    """Discord-safe summary; never imply execution profits."""
    if not proof["evaluations"]:
        return (proof["reason"] + ". RivaL needs new, genuinely future results; "
                "archived candles cannot substitute for them.")
    lines = []
    for evidence in proof["evaluations"]:
        candidate = evidence.get("candidate")
        horizon = evidence["horizon"]
        if not candidate or not candidate.get("valid"):
            lines.append(f"**{horizon}m:** {evidence['reason']}")
            continue
        coverage = candidate["quote_coverage"]
        lines.append(
            f"**{horizon}m · {evidence['venue']}/{evidence['contract']}**\n"
            f"Model: {candidate['name'][:46]}\n"
            f"Future outcomes: {candidate['targets']}/{MIN_WINDOWS}, "
            f"days: {candidate['days']}/{MIN_DAYS}. "
            f"Matched book quotes: {candidate['closed_quotes']}/"
            f"{candidate['eligible_quotes']} ({coverage:.0%}, need {MIN_COVERAGE:.0%}).\n"
            f"Decision: {evidence['reason'][:170]}"
        )
    return ("\n".join(lines) + "\nPaper cost tests use assumed fees/slippage/funding; "
            "real fills and real profitability are NOT verified.")[:1024]
