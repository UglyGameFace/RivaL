"""Autonomous read-only dual-track forecasts, settlement, and model governance.

The mathematical track runs after each five-minute collection cycle and
relearns solely from already-released labels. Historic optional AI forecasts
remain readable, while private LFM3 hypotheses use their own bounded worker.
Every score is from a forecast stored before its outcome.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import math
import threading
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.ai_types import ResearchUnavailable
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.event_intelligence import EventIntelligence
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.local_first_research import LocalFirstResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch

_LOG = logging.getLogger(__name__)


class StepStillRunning(TimeoutError):
    """A cycle step's previous run has not finished; this cycle skips it."""


def _consume_result(task: asyncio.Future) -> None:
    # A step whose wait already gave up still finishes later: read its
    # outcome so a late failure is logged once, never "never retrieved".
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOG.warning("RivaL cycle step finished late with an error: %r", exc)
SLOT_HOURS = 4

# Per-stage deadlines for cycle(). Each DB stage's deadline EXCEEDS the 60 s
# cycle statement timeout (mark_background in _perp_loop), so a statement
# parked on a lock aborts first, the thread returns promptly with
# QueryCanceled, and the deadline only catches non-DB hangs. A timed-out
# stage is logged, its ok flag cleared, and the cycle continues to the next
# stage; the outer PERP_FORECAST_TIMEOUT_SECONDS cap in _perp_loop bounds
# the whole cycle. All values are hang bounds, not performance targets.
# Auto-AI: one LLM ask can take 100 s per POST plus a retry, so its bound
# is 240 s. Events: official-source polls are 7 s per request, but several
# sources are checked in sequence.
# The prediction stage normally takes 31-92 s on production (cycle logs,
# 2026-11-10), longer just after a restart and at the top of the hour. At
# 90 s the wait gave up on runs that were about to finish: the thread kept
# working, its forecasts were stored, but the cycle was recorded as a failed
# stage (learning_failure alerts) and the next stages started on top of it.
# 180 s still exceeds the 60 s statement timeout, so a parked statement
# aborts first, and still fits under the 240 s cap on the whole cycle.
AUTONOMOUS_FORWARD_TIMEOUT_SECONDS = 180
AUTONOMOUS_MULTI_SETTLE_TIMEOUT_SECONDS = 90
AUTONOMOUS_MULTI_ISSUE_TIMEOUT_SECONDS = 90
AUTONOMOUS_AI_SETTLE_TIMEOUT_SECONDS = 90
AUTONOMOUS_REVIEW_TIMEOUT_SECONDS = 120
AUTONOMOUS_PROMOTION_TIMEOUT_SECONDS = 120
AUTONOMOUS_AUTO_AI_TIMEOUT_SECONDS = 240
AUTONOMOUS_SAVE_TIMEOUT_SECONDS = 90
AUTONOMOUS_EVENTS_TIMEOUT_SECONDS = 120


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timezone-aware UTC datetime required")
    return value.astimezone(UTC)


def _time(value: object) -> datetime:
    return _utc(datetime.fromisoformat(str(value)))


class AutonomousResearch:
    """Separates automatic, recorded research from anything tradable."""

    def __init__(
        self, market: PerpetualMarketStore,
        *, ai: LocalFirstResearch | None, venues: tuple[str, ...],
        assets: tuple[str, ...] = ASSETS,
        auto_ai: bool = True,
    ) -> None:
        self.market = market
        self.store = market.store
        self.forward = ForwardAccuracyMonitor(market)
        self.events = EventIntelligence(self.store)
        self.multi = MultiExchangeResearch(self.store)
        self.ai = ai
        self.venues = venues
        self.assets = assets
        self.auto_ai = auto_ai
        self._lock = asyncio.Lock()
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._step_threads: dict[str, asyncio.Future] = {}

    def initialize(self) -> None:
        # This was running CREATE TABLE/INDEX on every model settlement and
        # review, while other workers were inserting prediction rows. Avoid
        # read/write DDL lock inversions after the first successful setup.
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            self.forward.initialize()
            self.multi.initialize()
            self.events.initialize()
            with self.store.connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS perp_learning_cycle_health (
                        slot_utc TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        math_issued INTEGER NOT NULL,
                        math_settled INTEGER NOT NULL,
                        ai_issued INTEGER NOT NULL,
                        math_ok INTEGER NOT NULL,
                        ai_settlement_ok INTEGER NOT NULL,
                        model_review_ok INTEGER NOT NULL,
                        auto_ai_ok INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS perp_auto_ai_slots (
                        asset TEXT NOT NULL,
                        model TEXT NOT NULL,
                        slot_utc TEXT NOT NULL,
                        attempted_at TEXT NOT NULL,
                        PRIMARY KEY (asset,model,slot_utc)
                    );
                    CREATE TABLE IF NOT EXISTS perp_auto_ai_forecasts (
                        asset TEXT NOT NULL,
                        model TEXT NOT NULL,
                        evidence_sha256 TEXT NOT NULL,
                        issued_at TEXT NOT NULL,
                        venue TEXT NOT NULL,
                        contract TEXT NOT NULL,
                        quote TEXT NOT NULL,
                        reference_opened_at TEXT NOT NULL,
                        reference_price DOUBLE PRECISION NOT NULL,
                        target_opened_at TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        source_count INTEGER NOT NULL,
                        settled_at TEXT,
                        outcome_up INTEGER,
                        realized_close DOUBLE PRECISION,
                        correct INTEGER,
                        PRIMARY KEY (asset,model,evidence_sha256)
                    );
                    CREATE INDEX IF NOT EXISTS idx_perp_auto_ai_unsettled
                        ON perp_auto_ai_forecasts (settled_at,target_opened_at);
                    CREATE INDEX IF NOT EXISTS idx_perp_auto_ai_asset
                        ON perp_auto_ai_forecasts (asset,issued_at);
                    CREATE TABLE IF NOT EXISTS perp_model_governance (
                        asset TEXT NOT NULL,
                        horizon_minutes INTEGER NOT NULL,
                        model_version TEXT NOT NULL,
                        evaluated_at TEXT NOT NULL,
                        unique_target_windows INTEGER NOT NULL,
                        calendar_days INTEGER NOT NULL,
                        candidate_brier DOUBLE PRECISION,
                        baseline_brier DOUBLE PRECISION,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        PRIMARY KEY (asset,horizon_minutes,model_version,evaluated_at)
                    );
                    """
                )
                # Retired-provider cleanup (2026-11-06): the heartbeat column
                # recorded the retired Gemini auto-AI stage. Databases created
                # before this rename still carry gemini_ok; rename it in
                # place so old rows keep their history under the new name.
                # Fresh databases are created with auto_ai_ok above, so this
                # is a no-op for them. RENAME COLUMN is SQLite/PostgreSQL
                # compatible; column_names() is the portable introspection
                # helper the promotion/auto-hunt migrations already use.
                columns = self.store.database.column_names(
                    conn, "perp_learning_cycle_health",
                )
                if "gemini_ok" in columns and "auto_ai_ok" not in columns:
                    conn.execute(
                        "ALTER TABLE perp_learning_cycle_health "
                        "RENAME COLUMN gemini_ok TO auto_ai_ok"
                    )
            self._schema_ready = True

    def settle_ai(self, *, now: datetime | None = None) -> int:
        """Resolve ONLY same-contract future candles observed after issuance."""
        current = _utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            pending = conn.execute(
                """SELECT * FROM perp_auto_ai_forecasts
                   WHERE settled_at IS NULL
                     AND target_opened_at<?
                     AND target_opened_at>=?
                   ORDER BY target_opened_at ASC LIMIT 100""",
                ((current - timedelta(minutes=5)).isoformat(),
                 (current - timedelta(days=7)).isoformat()),
            ).fetchall()
        settled = 0
        for pred in pending:
            with self.store.connect() as conn:
                candle = conn.execute(
                    """SELECT opened_at,closed_at,first_observed_at,close_price
                       FROM perp_market_candles
                       WHERE venue=? AND contract=? AND quote=?
                         AND interval_minutes=5 AND opened_at=?""",
                    (pred["venue"], pred["contract"], pred["quote"],
                     pred["target_opened_at"]),
                ).fetchone()
            if candle is None:
                continue
            issuance = _time(pred["issued_at"])
            observed = _time(candle["first_observed_at"])
            if not (issuance < observed <= current and _time(candle["closed_at"]) < current):
                continue
            price = float(candle["close_price"])
            reference = float(pred["reference_price"])
            if not math.isfinite(price) or price <= 0 or not math.isfinite(reference) or reference <= 0:
                continue
            # Exact ties are a neutral result, not a forced win or loss.
            realized_up = int(price > reference) if price != reference else None
            direction = str(pred["direction"])
            correct = (
                int((direction == "up") == bool(realized_up))
                if realized_up is not None and direction in ("up", "down")
                else None
            )
            with self.store.connect() as conn:
                updated = conn.execute(
                    """UPDATE perp_auto_ai_forecasts
                       SET settled_at=?,outcome_up=?,realized_close=?,correct=?
                       WHERE asset=? AND model=? AND evidence_sha256=?
                         AND settled_at IS NULL""",
                    (current.isoformat(), realized_up, price, correct,
                     pred["asset"], pred["model"], pred["evidence_sha256"]),
                )
                settled += int(updated.rowcount == 1)
        return settled

    def _claim_slot(self, *, asset: str, model: str, now: datetime) -> bool:
        """Persist before any paid call, making crashes/restarts fail closed."""
        slot = now.replace(
            hour=now.hour // SLOT_HOURS * SLOT_HOURS,
            minute=0, second=0, microsecond=0,
        ).isoformat()
        with self.store.connect() as conn:
            result = conn.execute(
                """INSERT INTO perp_auto_ai_slots
                   (asset,model,slot_utc,attempted_at) VALUES (?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (asset, model, slot, now.isoformat()),
            )
        return result.rowcount == 1

    async def record_ai_forecast(self, *, asset: str, now: datetime) -> bool:
        """Generate only from valid point-in-time current evidence."""
        if self.ai is None or not self.ai.available:
            return False
        now = _utc(now)
        await asyncio.to_thread(self.initialize)
        evidence, digest = await asyncio.to_thread(
            build_evidence, self.market, asset=asset, venues=self.venues, now=now,
        )
        sources = evidence["sources"]
        # Anchor on a trustworthy same-venue contract for later scoring.
        reference = next(
            (
                x for x in sources
                if x.get("last_candle_close")
                and x.get("contract") and x.get("quote")
                and x.get("venue") and x.get("close")
                and timedelta(0) <= now - _time(x["last_candle_close"])
                <= timedelta(minutes=7)
            ), None,
        )
        if reference is None:
            return False
        if not await asyncio.to_thread(
            self._claim_slot, asset=asset, model=self.ai.model, now=now,
        ):
            return False
        result = await self.ai.ask(asset=asset, venues=self.venues, busy_retry=True)
        # Reject cache-only or delayed results that would be issued after
        # the intended target candle. No retrospective paper predictions.
        issued = datetime.now(UTC)
        if result.evidence_sha256 != digest or result.asset != asset:
            return False
        if not timedelta(0) <= issued - result.generated_at <= timedelta(minutes=5):
            return False
        target_open = (_time(reference["last_candle_close"]) +
                       timedelta(milliseconds=1))
        target_close = target_open + timedelta(minutes=5)
        if not (target_open <= issued < target_close):
            return False
        return await asyncio.to_thread(
            self._insert_ai_forecast,
            asset=asset, model=result.model, digest=result.evidence_sha256,
            issued=issued, reference=reference, target_open=target_open,
            direction=result.direction, count=len(sources),
        )

    def _insert_ai_forecast(
        self, *, asset: str, model: str, digest: str,
        issued: datetime, reference: dict, target_open: datetime,
        direction: str, count: int,
    ) -> bool:
        with self.store.connect() as conn:
            record = conn.execute(
                """INSERT INTO perp_auto_ai_forecasts
                   (asset,model,evidence_sha256,issued_at,venue,contract,quote,
                    reference_opened_at,reference_price,target_opened_at,
                    direction,source_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (
                    asset, model, digest, issued.isoformat(),
                    reference["venue"], reference["contract"], reference["quote"],
                    (target_open - timedelta(minutes=5)).isoformat(),
                    float(reference["close"]), target_open.isoformat(),
                    direction, count,
                ),
            )
        return record.rowcount == 1

    async def run_auto_ai(self, *, now: datetime | None = None) -> int:
        """One bounded slot, max 18 potential AI requests/day for 3 assets."""
        current = _utc(now or datetime.now(UTC))
        in_slot = current.hour % SLOT_HOURS == 0 and current.minute % 20 < 5
        if (
            not self.auto_ai or self.ai is None or not self.ai.available
            or not in_slot
        ):
            if in_slot:
                _LOG.info(
                    "RivaL retired auto AI slot blocked reason=%s",
                    "auto_ai_disabled" if not self.auto_ai else "ai_unavailable",
                )
            return 0
        # Auto-AI slots are spaced 15 minutes apart, so never dispatch BTC,
        # ETH and SOL back-to-back. Use UTC minute 00/20/40 and persist
        # one attempt per asset per four-hour slot across bot restarts.
        position = current.minute // 20
        if position >= len(self.assets):
            return 0
        successes = 0
        async with self._lock:
            for asset in self.assets[position:position + 1]:
                try:
                    if await self.record_ai_forecast(
                        asset=asset, now=datetime.now(UTC),
                    ):
                        successes += 1
                except (InsufficientEvidence, ResearchUnavailable, ValueError) as exc:
                    _LOG.info(
                        "RivaL optional legacy AI skipped asset=%s reason=%s",
                        asset, type(exc).__name__,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL legacy AI audit failed asset=%s", asset)
        return successes

    def review_model(self, *, now: datetime | None = None) -> int:
        """Evaluate shadow promotion on paired forward events, not bar volume.

        Correlated exchanges at the same target time count ONCE. Only scores
        produced by the exact forward model version can affect governance.
        The day-block stress rule screens unstable results but cannot prove
        a live betting edge, and never grants trading permission.
        """
        current = _utc(now or datetime.now(UTC))
        self.initialize()
        # Only the last 7 days of targets are scored below. Reading 14 days of
        # every asset in one unindexed scan (~220k rows, 119 slow-query
        # warnings a day) to drop half in Python was the cost. Ask per
        # (asset, horizon) through idx_perp_forward_asset_horizon, with the
        # same 7-day target filter in SQL. The issued_at bounds keep the
        # original 14-day window; a target can never precede its issuance.
        rows = []
        with self.store.connect() as conn:
            for asset in self.assets:
                for horizon in HORIZONS:
                    rows.extend(conn.execute(
                        """SELECT asset,horizon_minutes,target_opened_at,
                                  model_brier,baseline_brier
                           FROM perp_forward_audit
                           WHERE asset=? AND horizon_minutes=?
                             AND issued_at>=? AND issued_at<=?
                             AND model_version=? AND settled_at IS NOT NULL
                             AND model_brier IS NOT NULL AND baseline_brier IS NOT NULL
                             AND target_opened_at>=?
                           ORDER BY target_opened_at""",
                        (asset, horizon,
                         max(current - timedelta(days=14),
                             current - timedelta(days=7, hours=2)).isoformat(),
                         current.isoformat(), FORWARD_MODEL,
                         (current - timedelta(days=7)).isoformat()),
                    ).fetchall())
        count = 0
        for asset in self.assets:
            for horizon in HORIZONS:
                recent = [
                    (str(x["target_opened_at"]), float(x["model_brier"]),
                     float(x["baseline_brier"]))
                    for x in rows
                    if x["asset"] == asset
                    and int(x["horizon_minutes"]) == horizon
                    and _time(x["target_opened_at"]) >= current - timedelta(days=7)
                ]
                try:
                    audit = score_paired_forward(recent)
                except (ValueError, TypeError, OverflowError) as exc:
                    _LOG.warning(
                        "RivaL governance skipped corrupt forward scores "
                        "asset=%s horizon=%s error=%s",
                        asset, horizon, type(exc).__name__,
                    )
                    continue
                reason = (
                    f"{audit.reason}; paired_gain="
                    f"{audit.paired_gain:.5f}; day_stress_lower="
                    f"{audit.lower_day_bound:.5f}; positive_days="
                    f"{audit.positive_days}/{audit.days}"
                    if audit.paired_gain is not None else audit.reason
                )
                with self.store.connect() as conn:
                    previous = conn.execute(
                        """SELECT status,unique_target_windows,calendar_days,
                                  candidate_brier,baseline_brier,reason
                           FROM perp_model_governance
                           WHERE asset=? AND horizon_minutes=? AND model_version=?
                           ORDER BY evaluated_at DESC LIMIT 1""",
                        (asset, horizon, FORWARD_MODEL),
                    ).fetchone()
                    if previous and (
                        previous["status"] == audit.status
                        and int(previous["unique_target_windows"]) == audit.targets
                        and int(previous["calendar_days"]) == audit.days
                        and previous["reason"] == reason
                    ):
                        continue
                    conn.execute(
                        """INSERT INTO perp_model_governance
                           (asset,horizon_minutes,model_version,evaluated_at,
                            unique_target_windows,calendar_days,candidate_brier,
                            baseline_brier,status,reason)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT DO NOTHING""",
                        (asset, horizon, FORWARD_MODEL,
                         current.isoformat(), audit.targets, audit.days,
                         audit.model_brier, audit.baseline_brier,
                         audit.status, reason),
                    )
                    _LOG.info(
                        "RivaL governance asset=%s horizon=%s status=%s "
                        "paired_gain=%s days=%d targets=%d",
                        asset, horizon, audit.status,
                        f"{audit.paired_gain:.5f}"
                        if audit.paired_gain is not None else "none",
                        audit.days, audit.targets,
                    )
                    count += 1
        return count

    def evaluate_promotion(self, *, now: datetime | None = None) -> dict:
        """Hourly promotion check for every slot; never blocks the cycle.

        Runs beside review_model, not every five minutes: promotion
        evidence moves on a days scale, and hourly evaluation keeps the
        promotion record fresh enough for the 48h freshness guard while
        avoiding churn from thin intra-hour windows.
        """
        from parlay_bot.perpetuals.promotion import evaluate_promotion

        stamp = now or datetime.now(UTC)
        results = []
        for asset in self.assets:
            for horizon in HORIZONS:
                try:
                    results.append(evaluate_promotion(
                        self.store, asset=asset, horizon=horizon, now=stamp,
                    ))
                except Exception:  # noqa: BLE001 - one bad slot never blocks the rest
                    _LOG.exception(
                        "RivaL promotion evaluation failed asset=%s horizon=%d",
                        asset, horizon,
                    )
        promoted = sum(1 for row in results if row["status"] == "promoted")
        demoted = sum(1 for row in results if row["status"] == "demoted")
        _LOG.info(
            "RivaL promotion evaluated slots=%d promoted=%d demoted=%d",
            len(results), promoted, demoted,
        )
        return {"slots": len(results), "promoted": promoted, "demoted": demoted}

    def learning_heartbeat(self) -> dict | None:
        """Uncached persisted result of the last five-minute research cycle."""
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT slot_utc,started_at,completed_at,math_issued,
                          math_settled,ai_issued,math_ok,ai_settlement_ok,
                          model_review_ok,auto_ai_ok
                   FROM perp_learning_cycle_health
                   ORDER BY completed_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def _save_cycle(
        self, *, started: datetime, issued: int, settled: int, ai_count: int,
        math_ok: bool, ai_settlement_ok: bool, review_ok: bool,
        auto_ai_ok: bool,
    ) -> None:
        """A single durable UTC-slot heartbeat, not fabricated predictions."""
        ended = datetime.now(UTC)
        slot = started.replace(
            minute=started.minute // 5 * 5, second=0, microsecond=0,
        ).isoformat()
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_learning_cycle_health
                   (slot_utc,started_at,completed_at,math_issued,
                    math_settled,ai_issued,math_ok,ai_settlement_ok,
                    model_review_ok,auto_ai_ok)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slot_utc) DO UPDATE SET
                       completed_at=excluded.completed_at,
                       math_issued=excluded.math_issued,
                       math_settled=excluded.math_settled,
                       ai_issued=excluded.ai_issued,
                       math_ok=excluded.math_ok,
                       ai_settlement_ok=excluded.ai_settlement_ok,
                       model_review_ok=excluded.model_review_ok,
                       auto_ai_ok=excluded.auto_ai_ok
                   WHERE perp_learning_cycle_health.completed_at <=
                         excluded.completed_at""",
                (
                    slot, started.isoformat(), ended.isoformat(),
                    issued, settled, ai_count, int(math_ok),
                    int(ai_settlement_ok), int(review_ok), int(auto_ai_ok),
                ),
            )

    async def _in_thread(self, name: str, fn, *, timeout: float):  # noqa: ASYNC109 - a wait budget, not a cancel
        """Run one cycle step in a thread, never on top of its last run.

        A step that outlives its wait keeps running in its thread (Python
        cannot stop it), still holding its database connection. Without
        this guard the next cycle started a second copy alongside it, on
        the same ledger. The previous run is tracked; while it is still
        going, this step is skipped (raised as a timeout, which every caller
        already logs as "timed out, skipping").
        """
        threads = self.__dict__.setdefault("_step_threads", {})
        running = threads.get(name)
        if running is not None and not running.done():
            _LOG.warning(
                "RivaL %s is still running from an earlier cycle; "
                "skipping it this cycle", name,
            )
            raise StepStillRunning(name)
        task = asyncio.ensure_future(asyncio.to_thread(fn))
        task.add_done_callback(_consume_result)
        threads[name] = task
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def cycle(self) -> tuple[int, int, int]:
        """Five-minute automatic research, with durable proof of each stage."""
        started = datetime.now(UTC)
        issued = settled = ai_count = 0
        math_ok = settlement_ok = review_ok = auto_ai_ok = True
        stages: dict[str, float] = {}
        mark = time.perf_counter()

        def lap(name: str) -> None:
            nonlocal mark
            stages[name] = time.perf_counter() - mark
            mark = time.perf_counter()

        try:
            issued, settled = await self._in_thread(
                "forward.run_once", self.forward.run_once,
                timeout=AUTONOMOUS_FORWARD_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            math_ok = False
            # Name the slow phase: the thread is still running, so this is
            # a snapshot of where its time had gone when the wait gave up.
            _LOG.warning(
                "RivaL forward research stage timed out after %ds, skipping "
                "(seconds per phase so far: %s)",
                AUTONOMOUS_FORWARD_TIMEOUT_SECONDS,
                " ".join(f"{phase}={seconds:.0f}" for phase, seconds in dict(
                    getattr(self.forward, "last_timing", None) or {}).items())
                or "unavailable",
            )
        except Exception:  # noqa: BLE001
            math_ok = False
            _LOG.exception("RivaL forward mathematical research cycle failed")
        lap("forward")
        # Challenge the incumbent only with forecasts genuinely issued by
        # the completed live research cycle, on shared UTC target windows.
        try:
            paired = await self._in_thread(
                "multi.settle", self.multi.settle,
                timeout=AUTONOMOUS_MULTI_SETTLE_TIMEOUT_SECONDS,
            )
            fused = await self._in_thread(
                "multi.issue", self.multi.issue,
                timeout=AUTONOMOUS_MULTI_ISSUE_TIMEOUT_SECONDS,
            )
            _LOG.info(
                "RivaL multi-source shadow issued=%s paired_settled=%s "
                "promotion=guarded_hourly_not_this_log", fused, paired,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _LOG.warning(
                "RivaL multi-exchange challenger stage timed out, skipping",
            )
        except Exception:  # noqa: BLE001 - ensemble cannot block existing bot
            _LOG.exception("RivaL multi-exchange challenger unavailable safely")
        lap("multi")
        try:
            await self._in_thread(
                "settle_ai", self.settle_ai,
                timeout=AUTONOMOUS_AI_SETTLE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            settlement_ok = False
            _LOG.warning(
                "RivaL auto-AI settlement stage timed out after %ds, skipping",
                AUTONOMOUS_AI_SETTLE_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            settlement_ok = False
            _LOG.exception("RivaL auto-AI outcome settlement failed")
        lap("ai_settle")
        if datetime.now(UTC).minute < 5:
            try:
                await self._in_thread(
                    "review_model", self.review_model,
                    timeout=AUTONOMOUS_REVIEW_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                review_ok = False
                _LOG.warning(
                    "RivaL shadow model review stage timed out after %ds, skipping",
                    AUTONOMOUS_REVIEW_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001
                review_ok = False
                _LOG.exception("RivaL shadow model gate review failed")
            try:
                await self._in_thread(
                    "evaluate_promotion", self.evaluate_promotion,
                    timeout=AUTONOMOUS_PROMOTION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _LOG.warning(
                    "RivaL promotion evaluation stage timed out after %ds, skipping",
                    AUTONOMOUS_PROMOTION_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL promotion evaluation failed")
        lap("hourly_review")
        try:
            ai_count = await asyncio.wait_for(
                self.run_auto_ai(), timeout=AUTONOMOUS_AUTO_AI_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            auto_ai_ok = False
            _LOG.warning(
                "RivaL automatic AI stage timed out after %ds, skipping",
                AUTONOMOUS_AUTO_AI_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            auto_ai_ok = False
            _LOG.exception("RivaL automatic AI research cycle failed")
        lap("auto_ai")
        try:
            await self._in_thread(
                "_save_cycle", functools.partial(
                    self._save_cycle, started=started,
                    issued=issued, settled=settled, ai_count=ai_count,
                    math_ok=math_ok, ai_settlement_ok=settlement_ok,
                    review_ok=review_ok, auto_ai_ok=auto_ai_ok,
                ),
                timeout=AUTONOMOUS_SAVE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _LOG.warning(
                "RivaL learning-cycle save stage timed out after %ds, skipping",
                AUTONOMOUS_SAVE_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL automatic learning heartbeat failed to persist")
        # Research news AFTER recording the regular math/AI cycle, so even a
        # slow official-source server cannot delay core forecast issuance.
        # Poll attempts are durably throttled per source across restarts.
        try:
            event_status = await self._in_thread(
                "events.refresh", self.events.refresh,
                timeout=AUTONOMOUS_EVENTS_TIMEOUT_SECONDS,
            )
            if event_status["sources_checked"]:
                _LOG.info(
                    "RivaL official event research sources_ok=%s checked=%s "
                    "events_seen=%s failures=%s",
                    event_status["sources_ok"], event_status["sources_checked"],
                    event_status["events_seen"], len(event_status["source_failures"]),
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            _LOG.warning(
                "RivaL official event research stage timed out after %ds, skipping",
                AUTONOMOUS_EVENTS_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - preserve the independent math cycle
            _LOG.exception("RivaL official macro/event research unavailable")
        lap("events")
        self.last_stages = stages
        _LOG.info("RivaL forecast stage timing %s",
                  " ".join(f"{k}={v:.1f}s" for k, v in stages.items()))
        return issued, settled, ai_count
