"""Autonomous read-only dual-track forecasts, settlement, and model governance.

The mathematical track runs after each five-minute collection cycle and
relearns solely from already-released labels. Gemini runs at most once per
asset per four-hour UTC slot, with independent persisted API budgets and no
order placement. Every score is from a forecast stored before its outcome.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.gemini_research import GeminiResearch, ResearchUnavailable
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.perpetuals.openrouter_research import OpenRouterResearch

_LOG = logging.getLogger(__name__)
SLOT_HOURS = 4


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
        *, ai: GeminiResearch | OpenRouterResearch | None, venues: tuple[str, ...],
        assets: tuple[str, ...] = ASSETS,
        auto_ai: bool = True,
    ) -> None:
        self.market = market
        self.store = market.store
        self.forward = ForwardAccuracyMonitor(market)
        self.multi = MultiExchangeResearch(self.store)
        self.ai = ai
        self.venues = venues
        self.assets = assets
        self.auto_ai = auto_ai
        self._lock = asyncio.Lock()
        self._schema_lock = threading.Lock()
        self._schema_ready = False

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
                        gemini_ok INTEGER NOT NULL
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
        self.initialize()
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
        result = await self.ai.ask(asset=asset, venues=self.venues)
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
                    "RivaL auto Gemini slot blocked reason=%s",
                    "auto_ai_disabled" if not self.auto_ai else "ai_unavailable",
                )
            return 0
        # Global Gemini API spacing is 15 minutes, so never dispatch BTC,
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
                        "RivaL optional auto Gemini skipped asset=%s reason=%s",
                        asset, type(exc).__name__,
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("RivaL auto Gemini audit failed asset=%s", asset)
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
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT asset,horizon_minutes,target_opened_at,
                          model_brier,baseline_brier
                   FROM perp_forward_audit
                   WHERE model_version=? AND settled_at IS NOT NULL
                     AND issued_at>=? AND issued_at<=?
                   ORDER BY target_opened_at""",
                (FORWARD_MODEL, (current - timedelta(days=14)).isoformat(),
                 current.isoformat()),
            ).fetchall()
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
                          model_review_ok,gemini_ok
                   FROM perp_learning_cycle_health
                   ORDER BY completed_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def _save_cycle(
        self, *, started: datetime, issued: int, settled: int, ai_count: int,
        math_ok: bool, ai_settlement_ok: bool, review_ok: bool,
        gemini_ok: bool,
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
                    model_review_ok,gemini_ok)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slot_utc) DO UPDATE SET
                       completed_at=excluded.completed_at,
                       math_issued=excluded.math_issued,
                       math_settled=excluded.math_settled,
                       ai_issued=excluded.ai_issued,
                       math_ok=excluded.math_ok,
                       ai_settlement_ok=excluded.ai_settlement_ok,
                       model_review_ok=excluded.model_review_ok,
                       gemini_ok=excluded.gemini_ok
                   WHERE perp_learning_cycle_health.completed_at <=
                         excluded.completed_at""",
                (
                    slot, started.isoformat(), ended.isoformat(),
                    issued, settled, ai_count, int(math_ok),
                    int(ai_settlement_ok), int(review_ok), int(gemini_ok),
                ),
            )

    async def cycle(self) -> tuple[int, int, int]:
        """Five-minute automatic research, with durable proof of each stage."""
        started = datetime.now(UTC)
        issued = settled = ai_count = 0
        math_ok = settlement_ok = review_ok = gemini_ok = True
        try:
            issued, settled = await asyncio.to_thread(self.forward.run_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            math_ok = False
            _LOG.exception("RivaL forward mathematical research cycle failed")
        # Challenge the incumbent only with forecasts genuinely issued by
        # the completed live research cycle, on shared UTC target windows.
        try:
            paired = await asyncio.to_thread(self.multi.settle)
            fused = await asyncio.to_thread(self.multi.issue)
            _LOG.info(
                "RivaL multi-source shadow issued=%s paired_settled=%s "
                "promotion=disabled", fused, paired,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - ensemble cannot block existing bot
            _LOG.exception("RivaL multi-exchange challenger unavailable safely")
        try:
            await asyncio.to_thread(self.settle_ai)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            settlement_ok = False
            _LOG.exception("RivaL Gemini outcome settlement failed")
        if datetime.now(UTC).minute < 5:
            try:
                await asyncio.to_thread(self.review_model)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                review_ok = False
                _LOG.exception("RivaL shadow model gate review failed")
            try:
                await asyncio.to_thread(self.evaluate_promotion)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL promotion evaluation failed")
        try:
            ai_count = await self.run_auto_ai()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            gemini_ok = False
            _LOG.exception("RivaL automatic Gemini research cycle failed")
        try:
            await asyncio.to_thread(
                self._save_cycle, started=started,
                issued=issued, settled=settled, ai_count=ai_count,
                math_ok=math_ok, ai_settlement_ok=settlement_ok,
                review_ok=review_ok, gemini_ok=gemini_ok,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL automatic learning heartbeat failed to persist")
        return issued, settled, ai_count
