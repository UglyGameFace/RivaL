"""Closed-loop model promotion: a challenger earns the live signal path.

The multi-venue fused challenger used to be shadow-only by a hardcoded
log line: its results could never become live signals, and nothing in
the signal path read governance. Promotion closes that loop with two
paired proofs, both on the challenger's own settled history:

- Paired forward Brier: the challenger's fused probability against the
  incumbent live probability on the SAME unique target windows, screened
  by the same governance rules as review_model (100 targets, 7 days,
  minimum gain, 80% positive days, positive day-block stress bound).
- Paired after-cost proof: same-venue quote-shadow windows where both
  the challenger and the incumbent priced the same book. Promotion needs
  at least 20 paired exits with the challenger no worse than the
  incumbent on gross basis points. Costs hit both sides of the same book
  equally, so the gross difference is the net difference.

Promotion is per (asset, horizon): six independent slots. A slot serves
the challenger only while its latest promotion row says "promoted", for
the current challenger model version, and fresh (evaluated within 48h).
Anything else serves the incumbent live model. A stale or failed
evaluation therefore fails safe to the incumbent, never to a challenger.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.multi_exchange import (
    ASSETS,
    HORIZONS,
)
from parlay_bot.perpetuals.multi_exchange import (
    MODEL as MULTI_MODEL,
)

_LOG = logging.getLogger(__name__)

MIN_QUOTE_WINDOWS = 20
PROMOTION_FRESHNESS = timedelta(hours=48)


def initialize(store) -> None:
    with store.connect() as conn:
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS perp_model_promotion (
                 asset TEXT NOT NULL,
                 horizon_minutes INTEGER NOT NULL,
                 engine TEXT NOT NULL,
                 model_version TEXT NOT NULL,
                 evaluated_at TEXT NOT NULL,
                 unique_target_windows INTEGER NOT NULL,
                 calendar_days INTEGER NOT NULL,
                 paired_gain DOUBLE PRECISION,
                 challenger_brier DOUBLE PRECISION,
                 incumbent_brier DOUBLE PRECISION,
                 quote_windows INTEGER NOT NULL,
                 challenger_gross_bps DOUBLE PRECISION,
                 incumbent_gross_bps DOUBLE PRECISION,
                 status TEXT NOT NULL,
                 reason TEXT NOT NULL,
                 PRIMARY KEY (asset, horizon_minutes, evaluated_at)
               );
               CREATE INDEX IF NOT EXISTS idx_perp_model_promotion_latest
                 ON perp_model_promotion (asset, horizon_minutes, evaluated_at);
            """
        )


def _utc(value: str) -> datetime:
    instant = datetime.fromisoformat(str(value))
    if instant.utcoffset() is None:
        raise ValueError("promotion clock requires timezone")
    return instant.astimezone(UTC)


def evaluate_promotion(
    store,
    *,
    asset: str,
    horizon: int,
    now: datetime | None = None,
) -> dict:
    """Evaluate one slot. Returns the promotion row that was recorded."""
    if asset not in ASSETS or horizon not in HORIZONS:
        raise ValueError("unknown promotion market")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("promotion clock requires timezone")
    stamp = stamp.astimezone(UTC)
    initialize(store)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT target_opened_at,challenger_brier,incumbent_brier
               FROM perp_multi_forward
               WHERE asset=? AND horizon_minutes=? AND model_version=?
                 AND settled_at IS NOT NULL
                 AND issued_at>=? AND issued_at<=?
               ORDER BY target_opened_at""",
            (
                asset,
                horizon,
                MULTI_MODEL,
                (stamp - timedelta(days=14)).isoformat(),
                stamp.isoformat(),
            ),
        ).fetchall()
        quotes = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(q.challenger_gross_bps) AS challenger_sum,
                      SUM(q.incumbent_gross_bps) AS incumbent_sum
               FROM perp_multi_quote_shadow AS q
               JOIN perp_multi_forward AS e
                 ON e.asset=q.asset AND e.horizon_minutes=q.horizon_minutes
                AND e.target_opened_at=q.target_opened_at
               WHERE e.asset=? AND e.horizon_minutes=?
                 AND e.model_version=? AND q.state='EXIT_QUOTED'
                 AND e.issued_at>=? AND e.issued_at<=?""",
            (
                asset,
                horizon,
                MULTI_MODEL,
                (stamp - timedelta(days=7)).isoformat(),
                stamp.isoformat(),
            ),
        ).fetchone()
        previous = conn.execute(
            """SELECT status FROM perp_model_promotion
               WHERE asset=? AND horizon_minutes=?
               ORDER BY evaluated_at DESC LIMIT 1""",
            (asset, horizon),
        ).fetchone()
    recent = [
        (
            str(row["target_opened_at"]),
            float(row["challenger_brier"]),
            float(row["incumbent_brier"]),
        )
        for row in rows
        if _utc(row["target_opened_at"]) >= stamp - timedelta(days=7)
    ]
    try:
        audit = score_paired_forward(recent)
    except (ValueError, TypeError, OverflowError) as exc:
        audit = None
        audit_reason = f"corrupt forward scores: {type(exc).__name__}"
    quote_windows = int(quotes["n"] or 0)
    challenger_gross = float(quotes["challenger_sum"]) / quote_windows if quote_windows else None
    incumbent_gross = float(quotes["incumbent_sum"]) / quote_windows if quote_windows else None
    quote_ok = (
        quote_windows >= MIN_QUOTE_WINDOWS
        and challenger_gross is not None
        and incumbent_gross is not None
        and challenger_gross >= incumbent_gross
    )
    was_promoted = previous is not None and str(previous["status"]) == "promoted"
    if audit is not None and audit.status == "qualified_shadow" and quote_ok:
        status, engine = "promoted", "multi"
        reason = (
            f"{audit.reason}; quote_windows={quote_windows} "
            f"challenger_gross={challenger_gross:.1f}bps "
            f"incumbent_gross={incumbent_gross:.1f}bps"
        )
    elif audit is not None and audit.status == "qualified_shadow":
        status, engine = "held", "live"
        reason = (
            f"Brier qualified but quote proof insufficient: "
            f"windows={quote_windows} (need {MIN_QUOTE_WINDOWS})"
            + (
                f" challenger_gross={challenger_gross:.1f}bps "
                f"incumbent_gross={incumbent_gross:.1f}bps"
                if challenger_gross is not None
                else ""
            )
        )
    elif was_promoted:
        status, engine = "demoted", "live"
        reason = audit.reason if audit is not None else audit_reason
    else:
        status, engine = "held", "live"
        reason = audit.reason if audit is not None else audit_reason
    row = {
        "asset": asset,
        "horizon_minutes": horizon,
        "engine": engine,
        "model_version": MULTI_MODEL,
        "evaluated_at": stamp.isoformat(),
        "unique_target_windows": audit.targets if audit is not None else 0,
        "calendar_days": audit.days if audit is not None else 0,
        "paired_gain": audit.paired_gain if audit is not None else None,
        "challenger_brier": audit.model_brier if audit is not None else None,
        "incumbent_brier": audit.baseline_brier if audit is not None else None,
        "quote_windows": quote_windows,
        "challenger_gross_bps": challenger_gross,
        "incumbent_gross_bps": incumbent_gross,
        "status": status,
        "reason": reason,
    }
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_model_promotion
               (asset,horizon_minutes,engine,model_version,evaluated_at,
                unique_target_windows,calendar_days,paired_gain,
                challenger_brier,incumbent_brier,quote_windows,
                challenger_gross_bps,incumbent_gross_bps,status,reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            tuple(
                row[key]
                for key in (
                    "asset",
                    "horizon_minutes",
                    "engine",
                    "model_version",
                    "evaluated_at",
                    "unique_target_windows",
                    "calendar_days",
                    "paired_gain",
                    "challenger_brier",
                    "incumbent_brier",
                    "quote_windows",
                    "challenger_gross_bps",
                    "incumbent_gross_bps",
                    "status",
                    "reason",
                )
            ),
        )
    if status != (previous["status"] if previous else None):
        _LOG.info(
            "RivaL promotion asset=%s horizon=%d status=%s engine=%s "
            "targets=%d days=%d paired_gain=%s quote_windows=%d",
            asset,
            horizon,
            status,
            engine,
            row["unique_target_windows"],
            row["calendar_days"],
            f"{row['paired_gain']:.5f}" if row["paired_gain"] is not None else "none",
            quote_windows,
        )
    return row


def promoted_engine(
    store,
    *,
    asset: str,
    horizon: int,
    now: datetime | None = None,
) -> str:
    """Which engine serves this slot: 'multi' only on a fresh promotion."""
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("promotion clock requires timezone")
    stamp = stamp.astimezone(UTC)
    initialize(store)
    with store.connect() as conn:
        row = conn.execute(
            """SELECT engine,model_version,evaluated_at,status
               FROM perp_model_promotion
               WHERE asset=? AND horizon_minutes=?
               ORDER BY evaluated_at DESC LIMIT 1""",
            (asset, horizon),
        ).fetchone()
    if row is None:
        return "live"
    if (
        str(row["status"]) != "promoted"
        or str(row["engine"]) != "multi"
        or str(row["model_version"]) != MULTI_MODEL
        or stamp - _utc(row["evaluated_at"]) > PROMOTION_FRESHNESS
    ):
        return "live"
    return "multi"


def multi_summary(
    store,
    *,
    asset: str,
    horizon: int,
    now: datetime | None = None,
) -> dict:
    """Tap-time proof for the fused challenger, same shape as venue proof."""
    if asset not in ASSETS or horizon not in HORIZONS:
        raise ValueError("unknown multi market")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("promotion clock requires timezone")
    stamp = stamp.astimezone(UTC)
    initialize(store)
    with store.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS issued,
                      SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled,
                      SUM(CASE WHEN settled_at IS NOT NULL
                                AND probability_up<>0.5 THEN 1 ELSE 0 END) AS decided,
                      SUM(CASE WHEN settled_at IS NOT NULL
                                AND ((probability_up>0.5 AND outcome_up=1)
                                OR (probability_up<0.5 AND outcome_up=0))
                               THEN 1 ELSE 0 END) AS correct,
                      SUM(CASE WHEN settled_at IS NOT NULL
                               THEN challenger_brier ELSE 0 END) AS challenger_sum,
                      SUM(CASE WHEN settled_at IS NOT NULL
                               THEN incumbent_brier ELSE 0 END) AS incumbent_sum
               FROM perp_multi_forward
               WHERE asset=? AND horizon_minutes=? AND model_version=?
                 AND issued_at>=? AND issued_at<=?""",
            (
                asset,
                horizon,
                MULTI_MODEL,
                (stamp - timedelta(days=7)).isoformat(),
                stamp.isoformat(),
            ),
        ).fetchone()
    n = int(row["settled"] or 0)
    decided = int(row["decided"] or 0)
    challenger = float(row["challenger_sum"]) / n if n else None
    incumbent = float(row["incumbent_sum"]) / n if n else None
    return {
        "issued": int(row["issued"]),
        "settled": n,
        "hit_rate": int(row["correct"] or 0) / decided if decided else None,
        "brier": challenger,
        "baseline_brier": incumbent,
        "challenger_brier": challenger,
        "incumbent_brier": incumbent,
        "model_beats_baseline": (challenger < incumbent if n >= 50 else None),
    }
