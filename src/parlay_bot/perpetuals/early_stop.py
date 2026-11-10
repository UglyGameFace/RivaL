"""Automatic early stop for test models that are clearly losing.

Promotion is slow on purpose (150 targets, 21 days, after-cost proof). Until
now a losing test model had no matching rule: it kept issuing shadow rows
until someone flipped its ``ISSUE_*`` switch by hand (flow_v1/v2 after 7 days,
flow_v3 after 53 graded outcomes). This module is that rule, predeclared:

A test model is stopped on ONE stream (venue, contract, quote, horizon) when,
over its graded shadow rows of the last 7 days on that stream,

- it has at least ``MIN_TARGETS`` graded targets across ``MIN_DAYS`` UTC days,
- its Brier score is worse than the main model's on the SAME targets by at
  least ``MIN_LOSS`` on average, and
- that average loss stays above zero after subtracting ``Z`` standard errors,
  with the variance inflated by horizon/5 because 15- and 60-minute targets
  issued every 5 minutes overlap.

The standard-error buffer is a heuristic conservative screen, like the
day-block screen in ``governance.score_paired_forward``. It is checked every
10 minutes on a rolling window, so it is not a formal confidence level.

What a stop does and does not do:

- It only stops NEW shadow rows for that family on that stream. Rows already
  issued keep settling and stay in the ledger. Nothing is deleted.
- It never touches the main model, promotion (Route B), ``BLOCKED_FAMILIES``
  or what serves live calls. A stopped test model was not serving.
- It is one-way. A stopped family never restarts by itself; re-admitting it
  means deleting its row from ``rival_family_early_stop`` on purpose, or
  shipping a successor under a new family name with a fresh record.
- A family that reuses the main model's own call (regime_v1, regime_wild_v1)
  has a paired loss of exactly zero and can never be stopped by this rule.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

_LOG = logging.getLogger(__name__)

# Kill switch for the rule itself. False: nothing new is stopped and existing
# stops are ignored at issuance (their rows stay as a record).
ENABLED = True
RULE_VERSION = "paired-loss-z258-v1"
MIN_TARGETS = 60
MIN_DAYS = 2
MIN_LOSS = 0.01
Z = 2.58

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_family_early_stop (
    venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL, family TEXT NOT NULL,
    stopped_at TEXT NOT NULL, rule_version TEXT NOT NULL,
    targets INTEGER NOT NULL, days INTEGER NOT NULL,
    model_brier DOUBLE PRECISION NOT NULL,
    incumbent_brier DOUBLE PRECISION NOT NULL,
    loss DOUBLE PRECISION NOT NULL, loss_lower DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (venue,contract,quote,horizon_minutes,family)
)"""


def verdict(score: dict) -> dict | None:
    """The stop evidence for one stream's 7-day paired score, or None.

    ``score`` carries n, days, model_score, incumbent_score, loss_square
    (the mean squared paired loss) and horizon_minutes. Anything missing,
    non-finite or out of range is no verdict, never a stop.
    """
    try:
        targets, days = int(score["n"]), int(score["days"])
        horizon = int(score["horizon_minutes"])
        model = float(score["model_score"])
        incumbent = float(score["incumbent_score"])
        square = float(score["loss_square"])
    except (KeyError, TypeError, ValueError):
        return None
    if (targets < MIN_TARGETS or days < MIN_DAYS or horizon < 5
            or not all(math.isfinite(x) for x in (model, incumbent, square))
            or not (0 <= model <= 1 and 0 <= incumbent <= 1 and 0 <= square <= 1)):
        return None
    loss = model - incumbent
    if loss < MIN_LOSS:
        return None
    variance = max(0.0, square - loss * loss) * max(1.0, horizon / 5)
    lower = loss - Z * math.sqrt(variance / targets)
    if lower <= 0:
        return None
    return {"targets": targets, "days": days, "model_brier": model,
            "incumbent_brier": incumbent, "loss": loss, "loss_lower": lower}


def stopped_families(conn, *, venue, contract, quote, horizon) -> frozenset[str]:
    """Families stopped on this stream. Call on the issuing connection."""
    if not ENABLED:
        return frozenset()
    rows = conn.execute(
        """SELECT family FROM rival_family_early_stop
           WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?""",
        (venue, contract, quote, horizon),
    ).fetchall()
    return frozenset(str(row["family"]) for row in rows)


class EarlyStop:
    def __init__(self, store):
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.execute(SCHEMA)

    def record(self, scores, *, now: datetime | None = None,
               in_use_since: str | None = None) -> list[dict]:
        """Persist a stop for every stream whose score meets the rule.

        ``scores`` is the per-stream 7-day aggregate the health monitor
        already computes (one scan serves both). Only families still issuing
        (``last_issued`` at or after ``in_use_since``) are considered: a
        retired family needs no stop. Returns the stops that are new.
        """
        if not ENABLED:
            return []
        stamp = (now or datetime.now(UTC))
        if stamp.utcoffset() is None:
            raise ValueError("early stop requires a UTC-aware clock")
        clock = stamp.astimezone(UTC).isoformat()
        found = []
        for score in scores or ():
            if in_use_since is not None and str(score.get("last_issued") or "") < in_use_since:
                continue
            evidence = verdict(score)
            if evidence is not None:
                found.append((score, evidence))
        if not found:
            return []
        self.initialize()
        new = []
        with self.store.connect() as conn:
            for score, evidence in found:
                key = (str(score["venue"]), str(score["contract"]), str(score["quote"]),
                       int(score["horizon_minutes"]), str(score["family"]))
                saved = conn.execute(
                    """INSERT INTO rival_family_early_stop (
                         venue,contract,quote,horizon_minutes,family,stopped_at,
                         rule_version,targets,days,model_brier,incumbent_brier,
                         loss,loss_lower)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (*key, clock, RULE_VERSION, evidence["targets"], evidence["days"],
                     evidence["model_brier"], evidence["incumbent_brier"],
                     evidence["loss"], evidence["loss_lower"]),
                ).rowcount
                if saved:
                    new.append({"venue": key[0], "contract": key[1], "quote": key[2],
                                "horizon_minutes": key[3], "family": key[4], **evidence})
        for stop in new:
            # Model quality, not a host fault: warning level, like the
            # regression alerts.
            _LOG.warning(
                "RivaL early stop family=%s venue=%s contract=%s horizon=%s "
                "targets=%d days=%d brier=%.4f main=%.4f loss=%.4f lower=%.4f; "
                "no new shadow rows, existing rows keep settling",
                stop["family"], stop["venue"], stop["contract"],
                stop["horizon_minutes"], stop["targets"], stop["days"],
                stop["model_brier"], stop["incumbent_brier"], stop["loss"],
                stop["loss_lower"],
            )
        return new

    def stops(self) -> list[dict]:
        """Every recorded stop, newest first (for cards and reports)."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,contract,quote,horizon_minutes,family,stopped_at,
                          rule_version,targets,days,model_brier,incumbent_brier,
                          loss,loss_lower
                   FROM rival_family_early_stop
                   ORDER BY stopped_at DESC, family, venue, horizon_minutes""",
            ).fetchall()
        return [dict(row) for row in rows]
