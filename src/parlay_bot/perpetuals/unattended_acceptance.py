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
    # The newest 200 paired results are normally inside the last two days;
    # widen to 21 days only when they are not (the 21-day join took ~17 s).
    for lookback in (2, 21):
        rows = _paired_rows(store, stamp, lookback)
        if len(rows) >= 200:
            break
    return _receipt_from_rows(rows, stamp)


_ASSETS = ("BTC", "ETH", "SOL", "XRP")
_HORIZONS = (5, 15, 60)
_SAMPLE = 200


def _paired_rows(store, stamp, days):
    """Newest settled incumbent/challenger pairs, read cheaply.

    One unscoped ``issued_at`` range over the whole forecast table has no
    usable index and held a pooled connection for 4-17 s on the host. Each
    (asset, horizon) scope instead walks ``idx_perp_forward_asset_horizon``
    newest-first and stops after ``_SAMPLE`` pairs, so the work no longer
    grows with table size. Merging and re-limiting keeps the same newest-200
    sample as before.

    The newest ``_SAMPLE`` pairs of a scope come from at most ``_SAMPLE``
    audit rows that have a settled pair, so those audit rows are picked
    first (EXISTS stops at the first match) and only they are joined. The
    plain join + ORDER BY made Postgres pair and sort every row of the
    2-day window (~26k pairs, 3-5 s per scope, up to 47 s held for all 12).
    Same rows, same order.
    """
    start = (stamp - timedelta(days=days)).isoformat()
    end = stamp.isoformat()
    rows = []
    with store.connect() as conn:
        for asset in _ASSETS:
            for horizon in _HORIZONS:
                rows.extend(conn.execute(
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
                       FROM (SELECT a.venue,a.contract,a.quote,a.asset,
                                    a.horizon_minutes,a.model_version,
                                    a.reference_opened_at,a.issued_at,
                                    a.target_opened_at,a.settled_at,
                                    a.outcome_up,a.model_brier
                             FROM perp_forward_audit AS a
                             WHERE a.asset=? AND a.horizon_minutes=?
                               AND a.issued_at>=? AND a.issued_at<=?
                               AND a.settled_at IS NOT NULL
                               AND EXISTS (
                                SELECT 1 FROM rival_model_shadow_forecasts AS p
                                WHERE p.venue=a.venue AND p.contract=a.contract
                                  AND p.quote=a.quote
                                  AND p.horizon_minutes=a.horizon_minutes
                                  AND p.reference_opened_at=a.reference_opened_at
                                  AND p.asset=a.asset AND p.issued_at=a.issued_at
                                  AND p.target_opened_at=a.target_opened_at
                                  AND p.settled_at IS NOT NULL)
                             ORDER BY a.issued_at DESC LIMIT ?) AS f
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
                       WHERE s.settled_at IS NOT NULL
                       ORDER BY f.issued_at DESC LIMIT ?""",
                    (asset, horizon, start, end, _SAMPLE, _SAMPLE),
                ).fetchall())
    rows.sort(key=lambda r: str(r["issued_at"]), reverse=True)
    return rows[:_SAMPLE]


def _receipt_from_rows(rows, stamp):
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
