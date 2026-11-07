"""Pinned offline-model fits (autonomous loop D14).

For the offline families (``archive_v1``, ``gbm_v1``) the exact artifact
under Route B evaluation holds the shadow slot until it is promoted or
rejected-with-finding. A newly trained fit waits in a visible queue and,
when admitted, starts its own 21 days / 150 targets from zero.

Rules, stated once (spec §11, D14):
- ``active()`` returns the pinned artifact, not the newest usable one.
  The pin is per (family, asset, horizon); the newest usable artifact is
  pinned only when no pin exists, or when the current pin has a verdict
  that releases the slot (``rejected``, or ``promoted`` and no longer
  serving anywhere).
- The graded-count query and ``evaluate()``'s shadow-row selection filter
  by the pinned artifact's document hash (``training_sha256`` on shadow
  rows), so a new fit never inherits evidence it did not earn.
- A pinned fit has the full lookback to earn a promotion. Its clock starts
  with its first issued forecast, or at pinning if no forecast can issue.
- A ``promoted`` pin keeps the slot while the family holds a fresh
  ``promoted_paper`` row anywhere in the asset/horizon; once it stops
  serving, the next queued fit is admitted.
- ``flow_v3`` / ``liq_v1`` are online learners with no discrete fit to
  pin; they keep the continuity check and never appear in this table.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

PINS_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_offline_model_pins (
    family TEXT NOT NULL, asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
    model_sha256 TEXT NOT NULL, pinned_at TEXT NOT NULL,
    verdict TEXT, verdict_at TEXT,
    PRIMARY KEY(family, asset, horizon_minutes, model_sha256)
)"""

VERDICT_PROMOTED = "promoted"
VERDICT_REJECTED = "rejected"


def initialize(store) -> None:
    with store.connect() as conn:
        conn.execute(PINS_SCHEMA)


def current_pin(store, family: str, asset: str, horizon: int) -> dict | None:
    """Latest pinned row for one (family, asset, horizon), or None."""
    initialize(store)
    with store.connect() as conn:
        row = conn.execute(
            """SELECT model_sha256, pinned_at, verdict, verdict_at
               FROM rival_offline_model_pins
               WHERE family=? AND asset=? AND horizon_minutes=?
               ORDER BY pinned_at DESC LIMIT 1""",
            (family, asset, int(horizon)),
        ).fetchone()
    if row is None:
        return None
    return {
        "model_sha256": str(row["model_sha256"]),
        "pinned_at": str(row["pinned_at"]),
        "verdict": row["verdict"],
        "verdict_at": row["verdict_at"],
    }


def rejected_shas(store, family: str, asset: str, horizon: int) -> set[str]:
    """Document hashes this scope has already rejected."""
    initialize(store)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT model_sha256 FROM rival_offline_model_pins
               WHERE family=? AND asset=? AND horizon_minutes=?
               AND verdict=?""",
            (family, asset, int(horizon), VERDICT_REJECTED),
        ).fetchall()
    return {str(r["model_sha256"]) for r in rows}


def pin(store, family: str, asset: str, horizon: int, model_sha256: str, *,
        now: datetime | None = None) -> int:
    """Pin one artifact for the scope; 0 when already pinned."""
    initialize(store)
    stamp = (now or datetime.now(UTC)).isoformat()
    with store.connect() as conn:
        row = conn.execute(
            """INSERT INTO rival_offline_model_pins
               (family,asset,horizon_minutes,model_sha256,pinned_at,verdict,verdict_at)
               VALUES (?,?,?,?,?,NULL,NULL) ON CONFLICT DO NOTHING""",
            (family, asset, int(horizon), model_sha256, stamp),
        )
    return row.rowcount


def set_verdict(store, family: str, asset: str, horizon: int, model_sha256: str,
                verdict: str, *, now: datetime | None = None) -> None:
    """Record a verdict for a pinned artifact (promoted / rejected)."""
    initialize(store)
    stamp = (now or datetime.now(UTC)).isoformat()
    with store.connect() as conn:
        conn.execute(
            """UPDATE rival_offline_model_pins SET verdict=?, verdict_at=?
               WHERE family=? AND asset=? AND horizon_minutes=? AND model_sha256=?""",
            (verdict, stamp, family, asset, int(horizon), model_sha256),
        )


def serving_anywhere(store, family: str, asset: str, horizon: int, *,
                     now: datetime | None = None, fresh_hours: int = 48) -> bool:
    """True while any scope in this asset/horizon holds a fresh promoted row."""
    stamp = now or datetime.now(UTC)
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM rival_family_promotion
                   WHERE asset=? AND horizon_minutes=? AND selected_family=?
                   AND status='promoted_paper' AND evaluated_at>=?""",
                (asset, int(horizon), family,
                 (stamp - timedelta(hours=fresh_hours)).isoformat()),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no promotion table means not serving
        return False
    return int(row["n"]) > 0 if row is not None else False


def pin_signature(store, family: str) -> tuple:
    """Pin rows for the reload signature: a verdict change must reload."""
    initialize(store)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT asset, horizon_minutes, model_sha256, verdict
               FROM rival_offline_model_pins WHERE family=?
               ORDER BY asset, horizon_minutes, pinned_at""",
            (family,),
        ).fetchall()
    return tuple(
        (str(r["asset"]), int(r["horizon_minutes"]), str(r["model_sha256"]),
         str(r["verdict"] or ""))
        for r in rows
    )


def select_pinned(store, family: str, asset: str, horizon: int, docs: list[dict], *,
                  now: datetime | None = None) -> dict | None:
    """The pinned artifact among usable ``docs`` (newest first), or None.

    Pins eagerly when no pin exists, advances past a rejected pin, and
    advances past a promoted pin that stopped serving anywhere. Never
    returns a rejected artifact. ``docs`` must be annotated with
    ``_sha256`` (the document hash, which is what shadow rows carry).
    """
    if not docs:
        return None
    stamp = now or datetime.now(UTC)
    pinned = current_pin(store, family, asset, horizon)
    rejected = rejected_shas(store, family, asset, horizon)
    if pinned is None:
        chosen = docs[0]
        pin(store, family, asset, horizon, chosen["_sha256"], now=stamp)
        return chosen
    sha = pinned["model_sha256"]
    verdict = pinned["verdict"]
    if verdict in (None, VERDICT_PROMOTED):
        for doc in docs:
            if doc["_sha256"] == sha:
                if verdict == VERDICT_PROMOTED and not serving_anywhere(
                        store, family, asset, horizon, now=stamp):
                    break  # promoted but no longer serving: advance below
                return doc
        # Pinned doc not usable anymore, or promoted and no longer
        # serving: admit the next queued fit.
        for doc in docs:
            if doc["_sha256"] not in rejected and doc["_sha256"] != sha:
                pin(store, family, asset, horizon, doc["_sha256"], now=stamp)
                return doc
        for doc in docs:
            if doc["_sha256"] == sha:
                return doc
        return None
    # verdict == "rejected": admit the next queued fit.
    for doc in docs:
        if doc["_sha256"] not in rejected:
            pin(store, family, asset, horizon, doc["_sha256"], now=stamp)
            return doc
    return None


def queue_depth(store, family: str, asset: str, horizon: int, *,
                usable_count: int) -> int:
    """Queued fits behind the pinned one, from a usable-doc count."""
    pin = current_pin(store, family, asset, horizon)
    if pin is None or usable_count <= 0:
        return 0
    return max(0, usable_count - 1)


def resolve_pin(store, family: str, asset: str, horizon: int, *, selected: bool,
                now: datetime | None = None, expected_sha: str | None = None,
                lookback: timedelta = timedelta(days=30)) -> dict | None:
    """Judge the current pin after an evaluation; sets verdicts only.

    Called from ``FamilyPromotion.evaluate()`` for the offline families.
    ``selected`` is whether this evaluation selected the family for the
    scope just evaluated. A pinned fit is promoted when selected. A fit
    that used its full collection window without selection releases its
    slot; pending rows can still settle through the existing ledger.
    Returns the current pin row, or None when there is no pin.
    """
    stamp = now or datetime.now(UTC)
    pin = current_pin(store, family, asset, horizon)
    if pin is None or pin["verdict"] is not None:
        return pin
    sha = pin["model_sha256"]
    if expected_sha is not None and sha != expected_sha:
        return pin
    if selected:
        set_verdict(store, family, asset, horizon, sha, VERDICT_PROMOTED, now=stamp)
        return current_pin(store, family, asset, horizon)
    try:
        with store.connect() as conn:
            first = conn.execute(
                """SELECT MIN(issued_at) AS v FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                   AND training_sha256=?""",
                (family, asset, int(horizon), sha),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no ledger yet means no verdict
        return pin
    try:
        anchor = datetime.fromisoformat(str((first and first["v"]) or pin["pinned_at"]))
        if anchor.utcoffset() is None:
            anchor = anchor.replace(tzinfo=UTC)
    except ValueError:
        return pin
    if stamp - anchor > lookback:
        set_verdict(store, family, asset, horizon, sha, VERDICT_REJECTED, now=stamp)
        return current_pin(store, family, asset, horizon)
    return pin
