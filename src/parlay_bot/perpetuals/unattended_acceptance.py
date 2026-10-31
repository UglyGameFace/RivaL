"""Fail-closed, read-only acceptance receipt from actual persisted model events.

A passing local fixture never substitutes for a passing hosted receipt. This
query accepts only a single causally aligned incumbent/challenger outcome and
matched source book, with genuine model identity and paired Brier scores.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from parlay_bot.storage.hot import RelationalHotStore


def unattended_acceptance_receipt(
    store: RelationalHotStore, *, now: datetime | None = None,
) -> dict:
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("acceptance requires an aware clock")
    stamp = stamp.astimezone(UTC)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT f.venue,f.contract,f.quote,f.horizon_minutes,
                      f.model_version,f.reference_opened_at,
                      f.issued_at,f.target_opened_at,f.settled_at,
                      f.outcome_up,f.model_brier AS incumbent_brier,
                      s.family,s.training_sha256,s.probability_up,
                      s.model_brier AS challenger_brier,
                      s.incumbent_brier AS paired_incumbent_brier,
                      s.settled_at AS challenger_settled_at,
                      q.state AS quote_state,q.entry_at,q.exit_at,
                      q.entry_bid,q.entry_ask,q.exit_bid,q.exit_ask
               FROM perp_forward_audit AS f
               JOIN rival_model_shadow_forecasts AS s
                ON s.venue=f.venue AND s.contract=f.contract
               AND s.quote=f.quote AND s.asset=f.asset
               AND s.horizon_minutes=f.horizon_minutes
               AND s.reference_opened_at=f.reference_opened_at
               AND s.issued_at=f.issued_at
               AND s.target_opened_at=f.target_opened_at
               LEFT JOIN rival_family_quote_shadow AS q
                ON q.venue=s.venue AND q.contract=s.contract
               AND q.quote=s.quote AND q.horizon_minutes=s.horizon_minutes
               AND q.family=s.family
               AND q.reference_opened_at=s.reference_opened_at
               WHERE f.settled_at IS NOT NULL AND s.settled_at IS NOT NULL
                 AND f.issued_at>=? AND f.issued_at<=?
               ORDER BY f.issued_at DESC LIMIT 200""",
            ((stamp-timedelta(days=21)).isoformat(),stamp.isoformat()),
        ).fetchall()
    issued = settled = paired = quoted = 0
    samples = []
    for row in rows:
        try:
            issued_at = datetime.fromisoformat(str(row["issued_at"]))
            target = datetime.fromisoformat(str(row["target_opened_at"]))
            resolved = datetime.fromisoformat(str(row["settled_at"]))
            challenger_resolved = datetime.fromisoformat(
                str(row["challenger_settled_at"])
            )
            p = float(row["probability_up"])
            model_brier = float(row["challenger_brier"])
            incumbent_brier = float(row["paired_incumbent_brier"])
            original_brier = float(row["incumbent_brier"])
            if (any(x.utcoffset() is None for x in (
                issued_at, target, resolved, challenger_resolved
            )) or not issued_at < target+timedelta(minutes=5) < resolved <= stamp
                    or challenger_resolved != resolved
                    or row["outcome_up"] not in (0, 1)
                    or len(str(row["training_sha256"])) != 64
                    or not str(row["family"])
                    or not str(row["model_version"])
                    or not all(math.isfinite(x) and 0<=x<=1 for x in (
                        p, model_brier, incumbent_brier, original_brier
                    ))
                    or abs(model_brier-(p-int(row["outcome_up"]))**2)>1e-8
                    or abs(incumbent_brier-original_brier)>1e-8):
                continue
            issued += 1
            settled += 1
            paired += 1
            if row["quote_state"] != "EXIT_QUOTED":
                continue
            entry = datetime.fromisoformat(str(row["entry_at"]))
            exit_at = datetime.fromisoformat(str(row["exit_at"]))
            book = tuple(float(row[key]) for key in (
                "entry_bid", "entry_ask", "exit_bid", "exit_ask",
            ))
            if (entry.utcoffset() is None or exit_at.utcoffset() is None
                    or not issued_at <= entry < target+timedelta(minutes=5)
                    < exit_at <= stamp
                    or not all(math.isfinite(x) and x>0 for x in book)
                    or not book[0] < book[1] or not book[2] < book[3]):
                continue
            quoted += 1
            samples.append({
                "market": "/".join((
                    str(row["venue"]), str(row["contract"]), str(row["quote"]),
                )),
                "family": str(row["family"]),
                "model_version": str(row["model_version"]),
                "issued_at": issued_at.isoformat(),
                "resolved_at": resolved.isoformat(),
                "challenger_brier": model_brier,
                "incumbent_brier": incumbent_brier,
            })
        except (TypeError, ValueError, OverflowError):
            continue
    # Operational proof, NOT the 150-target/21-day model approval test.
    return {
        "status": "HOST_EVIDENCE_PRESENT" if quoted else "UNVERIFIED",
        "matched_issued": issued,
        "matched_settled": settled,
        "paired_scored": paired,
        "source_quoted": quoted,
        "examples": samples[:3],
        "model_approved": False,
        "real_fills_verified": False,
        "restart_replay_verified": False,
        "note": (
            "A hosted snapshot does not prove restart idempotence. "
            "Run restart-injection acceptance on SQLite/Postgres separately."
        ),
    }
