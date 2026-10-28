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
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.gemini_research import GeminiResearch, ResearchUnavailable
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
MIN_UNIQUE_TIMES = 100
MIN_DAYS = 7
MIN_BRIER_GAIN = 0.005
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
        *, ai: GeminiResearch | None, venues: tuple[str, ...],
        assets: tuple[str, ...] = ("BTC", "ETH", "SOL"),
        auto_ai: bool = True,
    ) -> None:
        self.market = market
        self.store = market.store
        self.forward = ForwardAccuracyMonitor(market)
        self.ai = ai
        self.venues = venues
        self.assets = assets
        self.auto_ai = auto_ai
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        self.forward.initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
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
        if (
            not self.auto_ai or self.ai is None or not self.ai.available
            or current.hour % SLOT_HOURS or current.minute % 20 >= 5
        ):
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
        """Promote/rollback for SHADOW research only, never auto-betting.

        Each five-minute target is one correlated event even if nine exchanges
        all recorded it. Requires 100 distinct settled UTC targets across at
        least 7 separate days and lower prospective Brier loss than baseline.
        """
        current = _utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT asset,horizon_minutes,model_version,target_opened_at,
                          model_brier,baseline_brier
                   FROM perp_forward_audit
                   WHERE settled_at IS NOT NULL AND issued_at>=?
                     AND issued_at<=?
                   ORDER BY target_opened_at""",
                ((current - timedelta(days=14)).isoformat(), current.isoformat()),
            ).fetchall()
        count = 0
        for asset in self.assets:
            for horizon in (5, 15):
                relevant = [
                    x for x in rows if x["asset"] == asset
                    and int(x["horizon_minutes"]) == horizon
                ]
                buckets: dict[str, list[tuple[float, float]]] = {}
                for x in relevant:
                    key = str(x["target_opened_at"])
                    buckets.setdefault(key, []).append(
                        (float(x["model_brier"]), float(x["baseline_brier"]))
                    )
                recent = [
                    (key, values) for key, values in buckets.items()
                    if _time(key) >= current - timedelta(days=7)
                ]
                n = len(recent)
                days = len({_time(k).date() for k, _ in recent})
                model = (
                    sum(sum(p for p, _ in values) / len(values) for _, values in recent) / n
                    if n else None
                )
                baseline = (
                    sum(sum(b for _, b in values) / len(values) for _, values in recent) / n
                    if n else None
                )
                if n < MIN_UNIQUE_TIMES or days < MIN_DAYS:
                    status, reason = "insufficient", "Not enough unique forward UTC targets/days"
                elif model is not None and baseline is not None and model + MIN_BRIER_GAIN < baseline:
                    status, reason = "qualified_shadow", "Prospective Brier beats base-rate benchmark"
                else:
                    status, reason = "rejected_shadow", "No demonstrated improvement over baseline"
                with self.store.connect() as conn:
                    previous = conn.execute(
                        """SELECT status,unique_target_windows,calendar_days
                           FROM perp_model_governance WHERE asset=? AND horizon_minutes=?
                           ORDER BY evaluated_at DESC LIMIT 1""",
                        (asset, horizon),
                    ).fetchone()
                    if previous and (
                        previous["status"] == status and
                        int(previous["unique_target_windows"]) == n and
                        int(previous["calendar_days"]) == days
                    ):
                        continue
                    conn.execute(
                        """INSERT INTO perp_model_governance
                           (asset,horizon_minutes,model_version,evaluated_at,
                            unique_target_windows,calendar_days,candidate_brier,
                            baseline_brier,status,reason)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT DO NOTHING""",
                        (asset, horizon, "prospective-logistic-shadow-v1",
                         current.isoformat(), n, days, model, baseline, status, reason),
                    )
                    count += 1
        return count

    async def cycle(self) -> tuple[int, int, int]:
        """Five-minute autonomous cycle; each operation fails independently."""
        issued = settled = ai_count = 0
        try:
            issued, settled = await asyncio.to_thread(self.forward.run_once)
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL forward mathematical research cycle failed")
        try:
            await asyncio.to_thread(self.settle_ai)
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL Gemini outcome settlement failed")
        if datetime.now(UTC).minute < 5:
            try:
                await asyncio.to_thread(self.review_model)
            except Exception:  # noqa: BLE001
                _LOG.exception("RivaL shadow model gate review failed")
        try:
            ai_count = await self.run_auto_ai()
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL automatic Gemini research cycle failed")
        return issued, settled, ai_count
