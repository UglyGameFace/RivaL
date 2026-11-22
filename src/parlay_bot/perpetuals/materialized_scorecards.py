"""Slice 1 scorecard materialization: the worker's Job 1 and the canary.

The rival-worker service runs :func:`run_scorecard_cycle` every five
minutes: for every scorecard key it calls the EXISTING uncached aggregate
functions verbatim -- same code, same SQL, same accumulation order -- and
upserts the result dicts into ``rival_materialized_reads``. Nothing here
reimplements an aggregate; byte-identity with the bot's live reads holds
by construction and is proven by the shadow canary
(:func:`run_canary_report` / ``tools/materialized_reads_canary.py``).

The Polymarket payload is the status builder's own assembly
(``learner.summary`` plus the ``"pooled"`` comparison merged in, with the
builder's exact failure semantics), shared by the worker, the canary, and
the bot's fallback path so the three can never drift apart.

Descriptive only: no issuance, settlement, promotion, or admission path
consumes anything in this module.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage.materialized_reads import (
    KIND_ACTIVE_LEARNING,
    KIND_FORWARD_CALIBRATION,
    KIND_FORWARD_SUMMARY,
    KIND_POLYMARKET_SUMMARY,
    canonical_json,
    materialized_reads,
    payload_sha256,
    shadow_kind,
)

_LOG = logging.getLogger(__name__)

CYCLE_SECONDS = 300
# Fire two and a half minutes after each five-minute boundary: off the
# bot's candle-boundary work and off its :07/:37 benefit warm.
CYCLE_OFFSET_SECONDS = 150


def scorecard_keys() -> list[tuple[str, str, int]]:
    """Every (kind, asset, horizon_minutes) Job 1 computes, in write order."""
    keys: list[tuple[str, str, int]] = []
    for asset in ASSETS:
        keys.append((KIND_FORWARD_SUMMARY, asset, 0))
        for horizon in HORIZONS:
            keys.append((KIND_FORWARD_CALIBRATION, asset, horizon))
            keys.append((KIND_ACTIVE_LEARNING, asset, horizon))
            keys.append((KIND_POLYMARKET_SUMMARY, asset, horizon))
    return keys


def polymarket_status_payload(learner: PolymarketOutcomeLearner, *,
                              asset: str, horizon: int) -> dict:
    """The Polymarket status builder's payload, assembled exactly once.

    Mirrors the Discord client's ``polymarket_learning_status``: the
    learner summary with the pooled comparison merged in; a pooled failure
    is logged and the summary is returned without it.
    """
    result = learner.summary(asset=asset, horizon=horizon)
    try:
        result["pooled"] = learner.pooled_summary(asset=asset, horizon=horizon)
    except Exception:  # noqa: BLE001 - optional comparison line
        _LOG.warning("Pooled Polymarket scorecard unavailable")
    return result


class ScorecardComputer:
    """One store's verbatim scorecard computations (worker and canary)."""

    def __init__(self, store) -> None:
        self.store = store
        self.market = PerpetualMarketStore(store)
        self.monitor = ForwardAccuracyMonitor(self.market)
        # The bot's status builder calls polymarket_research.initialize()
        # before touching the learner (it owns tables the learner's
        # aggregates join); mirror that precondition exactly.
        self.research = PolymarketResearch(store)
        self.research.initialize()
        self.learner = self.research.learner

    def compute(self, kind: str, asset: str, horizon: int, *,
                now: datetime):
        if kind == KIND_FORWARD_SUMMARY:
            return self.monitor._summary_uncached(asset=asset, now=now)
        if kind == KIND_FORWARD_CALIBRATION:
            return self.monitor._calibration_uncached(
                asset=asset, horizon=horizon, now=now)
        if kind == KIND_ACTIVE_LEARNING:
            return self.monitor._active_learning_evidence_uncached(
                asset=asset, horizon_minutes=horizon, now=now)
        if kind == KIND_POLYMARKET_SUMMARY:
            # The learner's aggregates read the wall clock themselves;
            # there is no clock seam, and the worker/canary both call
            # them at their own wall time exactly as the bot does.
            return polymarket_status_payload(
                self.learner, asset=asset, horizon=horizon)
        raise ValueError(f"unknown scorecard kind: {kind}")


def run_scorecard_cycle(store, *, now: datetime | None = None,
                        shadow: bool = False,
                        computer: ScorecardComputer | None = None) -> dict:
    """Compute every key once and upsert it. One key's failure never
    blocks the others; the report records each key's outcome."""
    stamp = now or datetime.now(UTC)
    worker = computer or ScorecardComputer(store)
    reads = materialized_reads(store)
    written: list[str] = []
    failed: dict[str, str] = {}
    for kind, asset, horizon in scorecard_keys():
        label = f"{kind}:{asset}:{horizon}"
        try:
            payload = worker.compute(kind, asset, horizon, now=stamp)
            reads.upsert(shadow_kind(kind) if shadow else kind,
                         asset, horizon, payload, computed_at=stamp)
            written.append(label)
        except Exception as exc:  # noqa: BLE001 - per-key isolation
            _LOG.exception("scorecard computation failed for %s", label)
            failed[label] = type(exc).__name__
    return {"computed_at": stamp.isoformat(), "shadow": shadow,
            "written": written, "failed": failed}


def canary_entry(stored_json: str | None, live_payload) -> dict:
    """One key's byte comparison: stored canonical JSON vs live payload."""
    live_json = canonical_json(live_payload)
    entry = {"live_sha256": payload_sha256(live_json),
             "live_bytes": len(live_json.encode("utf-8")),
             "stored_sha256": None, "stored_bytes": None}
    if stored_json is None:
        entry["status"] = "MISSING"
        return entry
    entry["stored_sha256"] = payload_sha256(stored_json)
    entry["stored_bytes"] = len(stored_json.encode("utf-8"))
    entry["status"] = "MATCH" if stored_json == live_json else "DIFF"
    return entry


def run_canary_report(store, *, now: datetime | None = None,
                      computer: ScorecardComputer | None = None) -> dict:
    """Diff every shadow row against the live bot-path computation.

    Compares canonical JSON strings, so a MATCH is byte-identity of the
    payload the bot would serve. Payload contents are never included --
    only hashes and byte lengths.
    """
    stamp = now or datetime.now(UTC)
    worker = computer or ScorecardComputer(store)
    reads = materialized_reads(store)
    entries: dict[str, dict] = {}
    for kind, asset, horizon in scorecard_keys():
        label = f"{kind}:{asset}:{horizon}"
        row = reads.read_row(shadow_kind(kind), asset, horizon)
        stored_json = row[1] if row is not None else None
        try:
            live = worker.compute(kind, asset, horizon, now=stamp)
            entries[label] = canary_entry(stored_json, live)
        except Exception as exc:  # noqa: BLE001 - report, never crash
            entries[label] = {"status": "ERROR",
                              "error": type(exc).__name__,
                              "stored_sha256": (
                                  payload_sha256(stored_json)
                                  if stored_json is not None else None),
                              "stored_bytes": (
                                  len(stored_json.encode("utf-8"))
                                  if stored_json is not None else None),
                              "live_sha256": None, "live_bytes": None}
    counts: dict[str, int] = {}
    for entry in entries.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {"checked_at": stamp.isoformat(), "counts": counts,
            "entries": entries}


def seconds_until_next_cycle(now: datetime) -> float:
    """Delay to the next cycle slot (5-minute boundary + offset)."""
    epoch = now.timestamp()
    slot = (int(epoch // CYCLE_SECONDS) * CYCLE_SECONDS
            + CYCLE_OFFSET_SECONDS)
    if slot <= epoch:
        slot += CYCLE_SECONDS
    return slot - epoch


def run_forever(store, *, shadow: bool = False,
                max_cycles: int | None = None,
                sleep=time.sleep) -> None:
    """The worker loop: one cycle per slot, failures logged, never fatal."""
    computer = ScorecardComputer(store)
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        sleep(seconds_until_next_cycle(datetime.now(UTC)))
        try:
            report = run_scorecard_cycle(
                store, shadow=shadow, computer=computer)
            _LOG.info("scorecard cycle: %d written, %d failed",
                      len(report["written"]), len(report["failed"]))
        except Exception:  # noqa: BLE001 - the next slot retries
            _LOG.exception("scorecard cycle failed")
        cycles += 1
    _LOG.debug("scorecard loop stopped after %d cycles", cycles)
