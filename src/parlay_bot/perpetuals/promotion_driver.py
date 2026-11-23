"""Slice 2 of the out-of-process scope: the proof-review driver.

The 20-minute after-cost proof review (``FamilyPromotion.evaluate``) used
to run inside the bot's perp cycle (``ForwardAccuracyMonitor._run_once``):
one fair source-specific scope per 20-minute slot, selected by
``family_proof_review_slot`` / ``select_family_proof_scope`` from the
cycle's stream selection, evaluated with ``now`` = the cycle stamp. It is
the largest single periodic reader in the bot process (it streams the
3 GB shadow-forecast table), and its output already lives in a
materialization -- ``rival_family_promotion`` -- that every Discord
reader point-reads. Moving the *driver* therefore changes no reader and
no evaluation semantics: this module calls the same selection functions
with the same inputs and ``evaluate()`` itself, verbatim, from the
rival-worker service.

Slot replication is exact by construction:

* the slot is the same integer the bot derives,
  ``family_proof_review_slot(stamp)`` with ``stamp`` pinned to the
  20-minute boundary (the bot's cycle stamp falls inside the same
  ``minute % 20 < 5`` window and derives the same slot);
* the eligible set is ``ShadowLearningMonitor._eligible_streams`` with
  the same ``limit=40`` at the same stamp, passed through the same
  ``select_live_streams`` rotation the cycle builds (the rotation only
  reorders -- ``select_family_proof_scope`` consumes the set);
* ``evaluate`` is called with ``now`` = the slot boundary, so
  ``evaluated_at`` is the slot stamp and the trial clock's freshness is
  unchanged.

Behind ``RIVAL_PROMOTION_WORKER_DRIVEN`` (Settings, default off) the bot
cycle skips its in-process review and this driver is the only writer.
The write itself was already an idempotent upsert, so even an overlap
window is safe.

Shadow mode is the pre-flip proof (see ``tools/family_promotion_canary.py``):
the driver computes the slot's evaluation with the pinned stamp, hashes
the exact row ``evaluate`` wrote, then restores the pre-run state
(promotion row, receipts, offline-pin rows) so the bot remains the only
real writer. If a shadow evaluation ever *selects* a family, nothing is
restored and the cycle reports ``PROMOTION_DETECTED`` instead -- a
genuine first promotion must never be silently rolled back by a canary.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import (
    family_proof_review_slot,
    select_family_proof_scope,
    select_live_streams,
)
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.markets import asset_for_contract
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.storage.materialized_reads import canonical_json, payload_sha256

_LOG = logging.getLogger(__name__)

SLOT_SECONDS = 1200
# Fire seven and a half minutes after each 20-minute boundary: inside the
# same slot window the bot's first 5-minute cycle derives, but staggered
# off the scorecard job's +150 s start on the worker's shared pool. The
# stamp passed to evaluate() is the boundary regardless of this offset.
CYCLE_OFFSET_SECONDS = 450

# The offline families evaluate() passes to offline_model_pins.resolve_pin
# on every call (family_governance.py); the shadow snapshot covers exactly
# these families' pin rows for the evaluated asset/horizon.
PIN_FAMILIES = ("archive_v1", "gbm_v1", "regime_v2", "volcal_v1", "xmarket_v1")

PROMOTION_COLUMNS = (
    "venue", "contract", "quote", "asset", "horizon_minutes",
    "evaluated_at", "selected_family", "status", "reason", "evidence_json",
)
RECEIPT_COLUMNS = (
    "family", "venue", "contract", "quote", "horizon_minutes", "kind",
    "evaluated_at", "receipt_text", "evidence_json", "recorded_at",
)
RECEIPT_KEY = ("family", "venue", "contract", "quote", "horizon_minutes", "kind")
PIN_COLUMNS = (
    "family", "asset", "horizon_minutes", "model_sha256", "pinned_at",
    "verdict", "verdict_at",
)


# ---------------------------------------------------------------------------
# The bot-side stop flag (mirrors storage.materialized_reads.reads_enabled).


_ENABLED_LOCK = threading.Lock()
_ENABLED: bool | None = None


def worker_driven() -> bool:
    """The RIVAL_PROMOTION_WORKER_DRIVEN flag (Settings, default off).

    Resolved once per process: on Discloud an env change only reaches the
    container on a deploy/restart anyway, so caching cannot serve a flag
    value the operator did not deploy.
    """
    global _ENABLED
    if _ENABLED is None:
        with _ENABLED_LOCK:
            if _ENABLED is None:
                try:
                    from parlay_bot.config import Settings

                    _ENABLED = bool(Settings().rival_promotion_worker_driven)
                except Exception:  # noqa: BLE001 - flag unreadable = off
                    _ENABLED = False
    return _ENABLED


def reset_worker_driven_cache() -> None:
    """Test hook: forget the resolved flag so the next read re-resolves it."""
    global _ENABLED
    with _ENABLED_LOCK:
        _ENABLED = None


# ---------------------------------------------------------------------------
# Slot math and scope selection -- the bot cycle's inputs, replicated.


def slot_for(now: datetime) -> int:
    """The 20-minute proof slot containing ``now`` (UTC epoch // 1200)."""
    return int(utc(now).timestamp() // SLOT_SECONDS)


def slot_stamp(slot: int) -> datetime:
    """The pinned evaluation stamp for a slot: its UTC boundary."""
    return datetime.fromtimestamp(slot * SLOT_SECONDS, tz=UTC)


def seconds_until_slot_run(now: datetime) -> float:
    """Delay until this slot's driver run (boundary + offset); 0 if due."""
    moment = utc(now)
    target = slot_stamp(slot_for(moment)) + timedelta(
        seconds=CYCLE_OFFSET_SECONDS)
    return max(0.0, (target - moment).total_seconds())


def select_proof_scope(store, stamp: datetime):
    """Recompute the bot cycle's proof scope for ``stamp`` standalone.

    The exact three calls ``ForwardAccuracyMonitor._run_once`` makes:
    the freshness-checked eligible streams at the cycle stamp, the
    cycle's stream selection, then the deterministic scope rotation.
    Returns ``(proof_slot, scope)``; ``scope`` is None when no supported
    source scope exists (the bot's deferred case).
    """
    monitor = ShadowLearningMonitor(store)
    monitor.initialize()
    eligible = monitor._eligible_streams(now=stamp, limit=40)
    selection = select_live_streams(
        eligible, slot=int(stamp.timestamp() // 300))
    proof_slot = family_proof_review_slot(stamp)
    if proof_slot is None:
        return None, None
    return proof_slot, select_family_proof_scope(
        selection, hour_slot=proof_slot)


# ---------------------------------------------------------------------------
# Row access and hashing. The promotion row is the evaluation record:
# hashing every column (evaluated_at included -- identical under the
# pinned stamp) is the byte-identity check for the whole evaluation.


def read_promotion_row(store, *, venue: str, contract: str, quote: str,
                       horizon: int) -> dict | None:
    try:
        with store.connect() as conn:
            row = conn.execute(
                "SELECT venue,contract,quote,asset,horizon_minutes,"
                "evaluated_at,selected_family,status,reason,evidence_json "
                "FROM rival_family_promotion WHERE venue=? AND contract=? "
                "AND quote=? AND horizon_minutes=?",
                (venue, contract, quote, horizon),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no table yet: no row
        return None
    if row is None:
        return None
    return {column: row[column] for column in PROMOTION_COLUMNS}


def promotion_row_sha256(row: dict | None) -> str | None:
    if row is None:
        return None
    return payload_sha256(canonical_json(row))


def _safe_rows(store, sql: str, params: tuple) -> list[dict] | None:
    """All rows for a query, or None when the table does not exist yet.

    A separate connection per call: on Postgres a missing-table error
    aborts the transaction, and the snapshot must never poison the
    caller's connection.
    """
    try:
        with store.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except Exception:  # noqa: BLE001 - absent table is a valid snapshot
        return None


class PromotionSnapshot:
    """Pre-run state a shadow evaluation must restore exactly."""

    def __init__(self, *, promotion_row, receipts, pins):
        self.promotion_row = promotion_row
        self.receipts = receipts  # None = table absent at snapshot time
        self.pins = pins          # None = table absent at snapshot time


def snapshot_promotion_state(store, *, venue: str, contract: str,
                             quote: str, asset: str,
                             horizon: int) -> PromotionSnapshot:
    receipts = _safe_rows(
        store,
        "SELECT family,venue,contract,quote,horizon_minutes,kind,"
        "evaluated_at,receipt_text,evidence_json,recorded_at "
        "FROM rival_family_promotion_receipts WHERE venue=? AND contract=? "
        "AND quote=? AND horizon_minutes=?",
        (venue, contract, quote, horizon))
    if receipts is not None:
        receipts = [{c: row[c] for c in RECEIPT_COLUMNS} for row in receipts]
    pins = _safe_rows(
        store,
        "SELECT family,asset,horizon_minutes,model_sha256,pinned_at,"
        "verdict,verdict_at FROM rival_offline_model_pins "
        "WHERE asset=? AND horizon_minutes=?",
        (asset, horizon))
    if pins is not None:
        pins = [{c: row[c] for c in PIN_COLUMNS}
                for row in pins if row["family"] in PIN_FAMILIES]
    return PromotionSnapshot(
        promotion_row=read_promotion_row(
            store, venue=venue, contract=contract, quote=quote,
            horizon=horizon),
        receipts=receipts, pins=pins)


def restore_promotion_state(store, snapshot: PromotionSnapshot, *,
                            venue: str, contract: str, quote: str,
                            asset: str, horizon: int) -> None:
    """Undo a shadow evaluation's writes, byte for byte.

    Only ever called when the post-run promotion row is provably the
    shadow run's own write (or unchanged), so a concurrent bot write is
    never reverted -- the caller checks that and skips restore on a race.
    """
    with store.connect() as conn:
        conn.execute(
            "DELETE FROM rival_family_promotion WHERE venue=? "
            "AND contract=? AND quote=? AND horizon_minutes=?",
            (venue, contract, quote, horizon))
        if snapshot.promotion_row is not None:
            row = snapshot.promotion_row
            conn.execute(
                """INSERT INTO rival_family_promotion
                  (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                   selected_family,status,reason,evidence_json)
                  VALUES (?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(venue,contract,quote,horizon_minutes)
                  DO UPDATE SET evaluated_at=excluded.evaluated_at,
                   selected_family=excluded.selected_family,
                   status=excluded.status, reason=excluded.reason,
                   evidence_json=excluded.evidence_json""",
                tuple(row[c] for c in PROMOTION_COLUMNS))
    if snapshot.receipts is not None:
        keep = {tuple(row[c] for c in RECEIPT_KEY)
                for row in snapshot.receipts}
        with store.connect() as conn:
            current = conn.execute(
                "SELECT family,venue,contract,quote,horizon_minutes,kind "
                "FROM rival_family_promotion_receipts WHERE venue=? "
                "AND contract=? AND quote=? AND horizon_minutes=?",
                (venue, contract, quote, horizon)).fetchall()
            for row in current:
                key = tuple(row[c] for c in RECEIPT_KEY)
                if key not in keep:
                    conn.execute(
                        "DELETE FROM rival_family_promotion_receipts "
                        "WHERE family=? AND venue=? AND contract=? "
                        "AND quote=? AND horizon_minutes=? AND kind=?", key)
            for row in snapshot.receipts:
                conn.execute(
                    """INSERT INTO rival_family_promotion_receipts
                      (family,venue,contract,quote,horizon_minutes,kind,
                       evaluated_at,receipt_text,evidence_json,recorded_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(family,venue,contract,quote,
                                  horizon_minutes,kind) DO NOTHING""",
                    tuple(row[c] for c in RECEIPT_COLUMNS))
    else:
        # The receipts table did not exist at snapshot time; any scope
        # rows now present were written by the shadow run itself.
        try:
            with store.connect() as conn:
                conn.execute(
                    "DELETE FROM rival_family_promotion_receipts "
                    "WHERE venue=? AND contract=? AND quote=? "
                    "AND horizon_minutes=?",
                    (venue, contract, quote, horizon))
        except Exception:  # noqa: BLE001 - still absent: nothing to undo
            _LOG.debug("receipts table absent at restore", exc_info=True)
    if snapshot.pins is not None:
        with store.connect() as conn:
            placeholders = ",".join("?" for _ in PIN_FAMILIES)
            conn.execute(
                f"DELETE FROM rival_offline_model_pins WHERE family IN "
                f"({placeholders}) AND asset=? AND horizon_minutes=?",
                (*PIN_FAMILIES, asset, horizon))
            for row in snapshot.pins:
                conn.execute(
                    """INSERT INTO rival_offline_model_pins
                      (family,asset,horizon_minutes,model_sha256,pinned_at,
                       verdict,verdict_at) VALUES (?,?,?,?,?,?,?)
                      ON CONFLICT(family,asset,horizon_minutes,model_sha256)
                      DO NOTHING""",
                    tuple(row[c] for c in PIN_COLUMNS))


# ---------------------------------------------------------------------------
# The driver cycle.


def _evaluate_scope(store, *, asset: str, scope, stamp: datetime,
                    promotion: FamilyPromotion | None = None) -> dict:
    venue, contract, currency, horizon = scope
    gate = promotion or FamilyPromotion(store)
    return gate.evaluate(
        asset=asset, venue=venue, contract=contract, quote=currency,
        horizon=horizon, now=stamp)


def _run_slot(store, slot: int, *, shadow: bool,
              promotion: FamilyPromotion | None = None) -> dict:
    """One slot's proof review. In shadow mode the evaluation is
    snapshot-isolated and its row hash is the report's payload."""
    stamp = slot_stamp(slot)
    report: dict = {"slot": slot, "stamp": stamp.isoformat(),
                    "shadow": shadow, "scope": None, "deferred": False,
                    "status": None, "selected_family": None,
                    "row_sha256": None, "raced": False,
                    "promotion_detected": False, "error": None}
    try:
        proof_slot, scope = select_proof_scope(store, stamp)
        if proof_slot != slot:
            raise ValueError(
                f"proof slot mismatch: {proof_slot} != {slot}")
    except Exception as exc:  # noqa: BLE001 - report, never crash the loop
        _LOG.exception("proof scope selection failed for slot %s", slot)
        report["error"] = type(exc).__name__
        return report
    if scope is None:
        _LOG.info(
            "RivaL after-cost proof deferred: no supported source "
            "with observable matched bid/ask quotes")
        report["deferred"] = True
        return report
    venue, contract, currency, horizon = scope
    asset = asset_for_contract(contract)
    if asset is None:
        report["deferred"] = True
        return report
    report["scope"] = scope
    if not shadow:
        try:
            result = _evaluate_scope(
                store, asset=asset, scope=scope, stamp=stamp,
                promotion=promotion)
        except Exception as exc:  # noqa: BLE001 - optional shadow governance
            _LOG.exception("RivaL source-specific family gate unavailable")
            report["error"] = type(exc).__name__
            return report
        _LOG.info(
            "RivaL family promotion gate venue=%s asset=%s horizon=%s "
            "status=%s family=%s",
            venue, asset, horizon, result["status"],
            result["selected_family"] or "none")
        report["status"] = result["status"]
        report["selected_family"] = result["selected_family"]
        report["row_sha256"] = promotion_row_sha256(read_promotion_row(
            store, venue=venue, contract=contract, quote=currency,
            horizon=horizon))
        return report
    # Shadow: evaluate pinned, hash the written row, restore exactly.
    snapshot = snapshot_promotion_state(
        store, venue=venue, contract=contract, quote=currency, asset=asset,
        horizon=horizon)
    result = None
    error = None
    try:
        result = _evaluate_scope(
            store, asset=asset, scope=scope, stamp=stamp,
            promotion=promotion)
    except Exception as exc:  # noqa: BLE001 - restore what we provably wrote
        _LOG.exception("shadow proof evaluation failed for slot %s", slot)
        error = type(exc).__name__
    written = read_promotion_row(
        store, venue=venue, contract=contract, quote=currency,
        horizon=horizon)
    report["row_sha256"] = promotion_row_sha256(written)
    if result is not None:
        report["status"] = result["status"]
        report["selected_family"] = result["selected_family"]
    report["error"] = error
    if result is not None and result["selected_family"] is not None:
        # A genuine first promotion: the row (and its receipt) is the
        # real record the bot would have written. Never roll it back.
        report["promotion_detected"] = True
        _LOG.error(
            "shadow proof evaluation SELECTED %s for slot %s scope=%s; "
            "state left in place, human review required",
            result["selected_family"], slot, scope)
        return report
    ours = (written is not None
            and written["evaluated_at"] == stamp.isoformat())
    if ours or written == snapshot.promotion_row:
        restore_promotion_state(
            store, snapshot, venue=venue, contract=contract,
            quote=currency, asset=asset, horizon=horizon)
    else:
        # The bot wrote this scope after our snapshot (its own slot run):
        # leave its row untouched and void this slot's comparison.
        report["raced"] = True
        _LOG.warning(
            "shadow proof evaluation raced the bot's write for slot %s "
            "scope=%s; state left untouched", slot, scope)
    return report


def run_promotion_cycle(store, *, now: datetime | None = None,
                        shadow: bool = False,
                        promotion: FamilyPromotion | None = None) -> dict:
    """Drive the proof review for the slot containing ``now``."""
    return _run_slot(
        store, slot_for(now or datetime.now(UTC)), shadow=shadow,
        promotion=promotion)


def run_shadow_canary(store, *, slot: int | None = None,
                      expected_sha256: str | None = None,
                      now: datetime | None = None,
                      promotion: FamilyPromotion | None = None) -> dict:
    """Pinned-input proof for one slot, mirroring the Slice 1 canary.

    Recomputes the slot's evaluation in shadow mode (snapshot-isolated,
    so the live table is untouched) and compares the full promotion
    row's SHA-256 against ``expected_sha256`` -- the hash the worker's
    shadow cycle logged for the same slot. MATCH is byte-identity of
    the entire evaluation record under identical inputs.
    """
    moment = utc(now or datetime.now(UTC))
    if slot is None:
        slot = slot_for(moment)
        if (moment - slot_stamp(slot)).total_seconds() \
                < CYCLE_OFFSET_SECONDS + 60:
            # This slot's worker shadow run has not fired yet; prove the
            # most recent completed one instead.
            slot -= 1
    report = _run_slot(store, slot, shadow=True, promotion=promotion)
    report["expected_sha256"] = expected_sha256
    if report["promotion_detected"]:
        report["canary_status"] = "PROMOTION_DETECTED"
    elif report["error"] is not None:
        report["canary_status"] = "ERROR"
    elif report["deferred"]:
        report["canary_status"] = "NO_SCOPE"
    elif report["raced"]:
        report["canary_status"] = "RACED"
    elif expected_sha256 is None:
        report["canary_status"] = "COMPUTED"
    elif report["row_sha256"] == expected_sha256:
        report["canary_status"] = "MATCH"
    else:
        report["canary_status"] = "DIFF"
    return report


def run_forever(store, *, shadow: bool = False,
                max_cycles: int | None = None, sleep=time.sleep,
                now_fn=None) -> None:
    """The worker's Job 2 loop: one driven slot per 20-minute boundary,
    failures logged, never fatal. A restart derives the current slot
    from UTC and drives it, exactly like the bot's cycle does."""
    clock = now_fn or (lambda: datetime.now(UTC))
    driven = 0
    last_slot: int | None = None
    while max_cycles is None or driven < max_cycles:
        moment = clock()
        slot = slot_for(moment)
        if slot == last_slot:
            nxt = slot_stamp(slot + 1) + timedelta(
                seconds=CYCLE_OFFSET_SECONDS)
            sleep(max(1.0, (nxt - moment).total_seconds()))
            continue
        wait = seconds_until_slot_run(moment)
        if wait > 0:
            sleep(wait)
            moment = clock()
            slot = slot_for(moment)
            if slot == last_slot:
                continue
        try:
            report = run_promotion_cycle(
                store, now=moment, shadow=shadow)
            _LOG.info(
                "promotion cycle slot=%s scope=%s status=%s shadow=%s "
                "row_sha256=%s raced=%s promotion_detected=%s",
                report["slot"], report["scope"], report["status"], shadow,
                report["row_sha256"], report["raced"],
                report["promotion_detected"])
        except Exception:  # noqa: BLE001 - the next slot retries
            _LOG.exception("promotion cycle failed")
        last_slot = slot
        driven += 1
    _LOG.debug("promotion loop stopped after %d cycles", driven)
