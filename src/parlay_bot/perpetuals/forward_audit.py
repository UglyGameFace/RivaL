"""Immutable, truly-forward, read-only accuracy audit for RivaL.

A forecast must exist in PostgreSQL before the target bar was observed.
Never retroactively turn backfilled candles into prospective predictions.
Uses only model weights trained on outcomes already known at issuance.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.calibrated_challenger import CalibratedIncumbent
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.features import (
    WindowFeatureMemo,
    feature_vector,
    live_examples,
    persisted_candles,
)
from parlay_bot.perpetuals.flow_challenger import FlowChallenger, FlowChallengerV2
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.historical_challengers import HistoricalChallengers
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS, asset_for_contract
from parlay_bot.perpetuals.markets import HORIZONS as RESEARCH_HORIZONS
from parlay_bot.perpetuals.model_checkpoints import LiveModelCheckpoints
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.path_forward import (
    ProspectivePathLedger,
    empirical_paths,
    prune_path_memo,
)
from parlay_bot.perpetuals.pooled_features import model_vector
from parlay_bot.perpetuals.pooled_model import PooledModelSet
from parlay_bot.perpetuals.pooled_model import probability as pooled_probability
from parlay_bot.perpetuals.regime_evidence import VERSION as REGIME_VERSION
from parlay_bot.perpetuals.regime_evidence import evaluate_regimes, issue_regime
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.perpetuals.watch_evidence import day_block_watch_evidence
from parlay_bot.storage.database import database_clock

_LOG = logging.getLogger(__name__)
MODEL = "prospective-logistic-persistent-v3"
# A forecast can settle once its 5-minute target candle has closed; still
# unsettled an hour after that means a missing candle or a stalled settler.
def no_trade_outcome(row) -> bool:
    """True when the target candle recorded zero trades (unknown is -1)."""
    try:
        return int(row["outcome_trades"]) == 0
    except (KeyError, TypeError, ValueError, IndexError):
        return False


SETTLEMENT_DUE_AFTER = timedelta(minutes=5)
SETTLEMENT_OVERDUE_AFTER = timedelta(minutes=65)
HORIZONS = RESEARCH_HORIZONS
MIN_PREVIOUS_EVALUATED = 20
MAX_STREAMS_PER_CYCLE = 40
MAX_PENDING_SETTLEMENTS = 120
# flow_v1/flow_v2 are retired: over 7 days of graded outcomes (to Nov 5, 2026)
# they lost to every venue's up-rate at every horizon (60m: -0.047/-0.054
# Brier) and their ~78%-UP calls came true 33-44% of the time. A calibrated,
# input-standardized successor would be a new family, not these.
ISSUE_FLOW_CHALLENGERS = False
# The incumbent's fast settlement covers the last 2 days; a day of margin.
SHADOW_FAST_SETTLE_WINDOW = timedelta(days=3)


def _features(candles) -> tuple[float, ...]:
    return feature_vector(candles)

def _probability(weights: tuple[float, ...], features: tuple[float, ...]) -> float:
    if len(weights) != len(features) + 1:
        raise ValueError("invalid trained shadow model")
    value = max(-30.0, min(30.0, sum(
        w * x for w, x in zip(weights, (1.0, *features))
    )))
    return 1.0 / (1.0 + math.exp(-value))


def select_live_streams(
    eligible: list[tuple[str, str, str]], *, slot: int,
    limit: int = MAX_STREAMS_PER_CYCLE,
) -> list[tuple[str, str, str]]:
    """Bound each research cycle while including each available coin.

    Venue grouping must not starve ETH/SOL predictions just because
    the venue-ordered SQL result is sliced into fixed 15-minute groups.
    Rotate each coin's venue list between cycles for source fairness.
    """
    if slot < 0 or not 1 <= limit <= MAX_STREAMS_PER_CYCLE:
        raise ValueError("invalid live forecast selection")
    buckets: dict[str, list[tuple[str, str, str]]] = {asset: [] for asset in ASSETS}
    for entry in eligible:
        contract = entry[1]
        asset = asset_for_contract(contract)
        if asset is not None:
            buckets[asset].append(entry)
    for asset, streams in buckets.items():
        if streams:
            offset = slot % len(streams)
            buckets[asset] = streams[offset:] + streams[:offset]
    selected: list[tuple[str, str, str]] = []
    while len(selected) < limit and any(buckets.values()):
        for asset in ASSETS:
            if buckets[asset]:
                selected.append(buckets[asset].pop(0))
                if len(selected) == limit:
                    break
    return selected


def select_family_proof_scope(
    eligible: list[tuple[str, str, str]], *, hour_slot: int,
) -> tuple[str, str, str, int] | None:
    """Rotate every quote-observable venue/asset/horizon within 48 hours.

    Only original source-matched bid/ask snapshots can satisfy the after-cost
    proof. The old hour % all_streams selection spent most proof evaluations
    on venues without a quote observer, then changed horizon only after a
    complete stream rotation (often >48 hours). Build a stable, deterministic
    source-specific rotation instead. Never invent missing quotes or promote
    a candidate without independent future and cost validation.
    """
    if hour_slot < 0:
        raise ValueError("invalid UTC proof-evaluation slot")
    # Delayed import avoids circular initialization: family_quotes references
    # profit_proof, which itself imports the authoritative forward model.
    from parlay_bot.perpetuals.family_quotes import supports_quote_source

    scopes = sorted({
        (venue, contract, currency, horizon)
        for venue, contract, currency in eligible
        if supports_quote_source(venue, currency)
        for horizon in HORIZONS
    })
    return scopes[hour_slot % len(scopes)] if scopes else None


class ForwardAccuracyMonitor:
    """Issue, settle and score immutable prospective research only."""

    def __init__(self, market: PerpetualMarketStore, *,
                 read_only: bool = False) -> None:
        self.market = market
        self.store = market.store
        self._read_only = read_only

    def initialize(self) -> None:
        if self._read_only:
            # Diagnostics may inspect existing production tables, never run
            # DDL or retrospective label corrections merely to read them.
            return
        # Monitors are constructed for every signal and venue proof. Schema
        # DDL belongs to process startup, not every background scan. Share
        # readiness with all monitors backed by this market instance.
        if self.market._forward_schema_ready:
            return
        with self.market._forward_schema_lock:
            if self.market._forward_schema_ready:
                return
            if self.market.live_model_checkpoints is None:
                self.market.live_model_checkpoints = LiveModelCheckpoints(self.store)
            self.market.live_model_checkpoints.initialize()
            if self.market.historical_challengers is None:
                self.market.historical_challengers = HistoricalChallengers(self.store, self.market.live_model_checkpoints)
            self.market.historical_challengers.initialize()
            self._initialize_schema()
            ShadowModelCompetition(self.store).initialize()
            self._withdraw_no_trade_labels()
            self.market._forward_schema_ready = True

    def _pooled_models(self):
        """The bot-wide pooled model set (None if its table is unavailable)."""
        current = getattr(self.market, "pooled_models", None)
        if current is None:
            try:
                current = PooledModelSet(self.store)
                current.initialize()
                self.market.pooled_models = current
            except Exception:  # noqa: BLE001 - optional challenger only
                _LOG.exception("RivaL pooled model registry unavailable")
                return None
        return current

    @staticmethod
    def _pooled_calls(pooled_set, horizon: int, vectors) -> list[dict]:
        """One shadow call per served pooled model (one per training lineage)."""
        pairs = (vectors or {}).get(horizon) or ()
        if isinstance(pairs, tuple) and len(pairs) == 2 and isinstance(pairs[0], dict):
            pairs = (pairs,)  # a single (doc, vector)
        if pooled_set is None:
            return []
        calls = []
        for doc, vector in pairs:
            if doc is None or vector is None:
                continue
            try:
                calls.append({"family": doc["_family"],
                              "probability": pooled_probability(doc, vector),
                              "model_sha256": doc["_sha256"],
                              "training_rows": int(doc.get("training_rows") or 0)})
            except (ValueError, KeyError, TypeError, OverflowError):
                _LOG.warning("RivaL pooled model prediction failed %s min", horizon)
        return calls

    def _withdraw_no_trade_labels(self) -> None:
        """Correct outcomes graded from candles that had no trades.

        Earlier settlement scored a zero-trade target candle (the previous
        price repeated) as NOT UP. Those labels are withdrawn: the row stays
        settled with its observed close, but is no longer counted as a
        comparable result. Idempotent; only touches flat, zero-trade targets.
        """
        try:
            with self.store.connect() as conn:
                result = conn.execute(
                    """UPDATE perp_forward_audit
                       SET outcome_up=NULL, model_brier=NULL, baseline_brier=NULL
                       WHERE settled_at IS NOT NULL AND outcome_up IS NOT NULL
                         AND realized_close=reference_close
                         AND EXISTS (
                           SELECT 1 FROM perp_market_candles AS c
                           WHERE c.venue=perp_forward_audit.venue
                             AND c.contract=perp_forward_audit.contract
                             AND c.quote=perp_forward_audit.quote
                             AND c.interval_minutes=5
                             AND c.opened_at=perp_forward_audit.target_opened_at
                             AND c.trades=0)"""
                )
                if result.rowcount:
                    _LOG.warning("RivaL withdrew %d forecast results graded from "
                                 "no-trade candles", result.rowcount)
        except Exception:  # noqa: BLE001 - correction must never block startup
            _LOG.exception("RivaL no-trade outcome correction unavailable")

    def _initialize_schema(self) -> None:
        self.market.initialize()
        ShadowLearningMonitor(self.store).initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_forward_cycle_diagnostics (
                    slot_utc TEXT PRIMARY KEY,
                    checked_at TEXT NOT NULL,
                    discovered_streams INTEGER NOT NULL,
                    selected_streams INTEGER NOT NULL,
                    short_history INTEGER NOT NULL,
                    stale_last_candle INTEGER NOT NULL,
                    invalid_data INTEGER NOT NULL,
                    insufficient_causal_labels INTEGER NOT NULL,
                    insufficient_training INTEGER NOT NULL,
                    target_expired INTEGER NOT NULL,
                    duplicate_forecasts INTEGER NOT NULL,
                    issued_forecasts INTEGER NOT NULL,
                    settled_forecasts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS perp_forward_audit (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    reference_opened_at TEXT NOT NULL,
                    reference_close DOUBLE PRECISION NOT NULL,
                    issued_at TEXT NOT NULL,
                    target_opened_at TEXT NOT NULL,
                    probability_up DOUBLE PRECISION NOT NULL,
                    baseline_up DOUBLE PRECISION NOT NULL,
                    training_evaluated INTEGER NOT NULL,
                    settled_at TEXT,
                    outcome_up INTEGER,
                    realized_close DOUBLE PRECISION,
                    model_brier DOUBLE PRECISION,
                    baseline_brier DOUBLE PRECISION,
                    PRIMARY KEY (
                        venue, contract, quote, horizon_minutes,
                        model_version, reference_opened_at
                    )
                );
                CREATE TABLE IF NOT EXISTS perp_forward_regimes (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    reference_opened_at TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    method_version TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    five_min_volatility_pct DOUBLE PRECISION NOT NULL,
                    feature_sha256 TEXT NOT NULL,
                    weights_sha256 TEXT NOT NULL,
                    PRIMARY KEY (
                        venue,contract,quote,horizon_minutes,model_version,
                        reference_opened_at
                    )
                );
                -- Per-issuance evidence, separate from old forecast rows.
                -- Only new predictions can claim a known training lineage.
                CREATE TABLE IF NOT EXISTS rival_forward_model_lineage (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    reference_opened_at TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    weights_sha256 TEXT NOT NULL,
                    feature_sha256 TEXT NOT NULL,
                    training_labels INTEGER NOT NULL,
                    historical_training_labels INTEGER NOT NULL,
                    online_training_updates INTEGER NOT NULL,
                    historical_seed_sha256 TEXT,
                    PRIMARY KEY (
                        venue,contract,quote,horizon_minutes,
                        model_version,reference_opened_at
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_rival_forward_lineage_issued
                    ON rival_forward_model_lineage (issued_at);
                -- Scope the current-policy lookup by horizon/model and time.
                CREATE INDEX IF NOT EXISTS idx_rival_forward_lineage_policy
                    ON rival_forward_model_lineage
                       (model_version,horizon_minutes,issued_at,venue,contract,quote);
                CREATE INDEX IF NOT EXISTS idx_perp_forward_regime_method
                    ON perp_forward_regimes (method_version,regime);
                CREATE INDEX IF NOT EXISTS idx_perp_forward_pending
                    ON perp_forward_audit (settled_at,target_opened_at);
                CREATE INDEX IF NOT EXISTS idx_perp_forward_asset_horizon
                    ON perp_forward_audit (asset,horizon_minutes,issued_at);
                -- Serves recent_watches exactly: asset + issued range for
                -- still-pending forecasts, ordered newest first.
                CREATE INDEX IF NOT EXISTS idx_perp_forward_asset_issued
                    ON perp_forward_audit (asset,issued_at)
                    WHERE settled_at IS NULL;
                """
            )

    def latest_diagnostics(self) -> dict | None:
        """Actual most recent issuance cycle, uncached and restart-safe."""
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_forward_cycle_diagnostics
                   ORDER BY checked_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row is not None else None

    def _record_cycle(self, stamp: datetime, metrics: dict[str, int]) -> None:
        """Bounded per-UTC-slot reason counts, no guesses about missing data."""
        slot = stamp.replace(
            minute=stamp.minute // 5 * 5, second=0, microsecond=0,
        ).isoformat()
        columns = (
            "discovered_streams", "selected_streams", "short_history",
            "stale_last_candle", "invalid_data",
            "insufficient_causal_labels", "insufficient_training",
            "target_expired", "duplicate_forecasts",
            "issued_forecasts", "settled_forecasts",
        )
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_cycle_diagnostics
                   (slot_utc,checked_at,discovered_streams,selected_streams,
                    short_history,stale_last_candle,invalid_data,
                    insufficient_causal_labels,insufficient_training,
                    target_expired,duplicate_forecasts,
                    issued_forecasts,settled_forecasts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (slot_utc) DO UPDATE SET
                     checked_at=excluded.checked_at,
                     discovered_streams=excluded.discovered_streams,
                     selected_streams=excluded.selected_streams,
                     short_history=excluded.short_history,
                     stale_last_candle=excluded.stale_last_candle,
                     invalid_data=excluded.invalid_data,
                     insufficient_causal_labels=excluded.insufficient_causal_labels,
                     insufficient_training=excluded.insufficient_training,
                     target_expired=excluded.target_expired,
                     duplicate_forecasts=excluded.duplicate_forecasts,
                     issued_forecasts=excluded.issued_forecasts,
                     settled_forecasts=excluded.settled_forecasts
                   WHERE perp_forward_cycle_diagnostics.checked_at
                         <= excluded.checked_at""",
                (slot, stamp.isoformat(), *(metrics[x] for x in columns)),
            )

    def learning_lineage_summary(
        self, *, asset: str, horizon_minutes: int,
        now: datetime | None = None,
    ) -> dict:
        """Show whether recent REAL forecasts used a verified historical seed.

        Legacy forecasts have no lineage proof. Never interpret missing rows as
        zero historical training, or training counts as unseen forward wins.
        """
        if horizon_minutes not in HORIZONS or asset not in ASSETS:
            raise ValueError("unsupported asset or horizon")
        self.initialize()
        stamp = utc(now or datetime.now(UTC))
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT f.issued_at, l.weights_sha256, l.feature_sha256,
                          l.training_labels, l.historical_training_labels,
                          l.online_training_updates, l.historical_seed_sha256
                   FROM perp_forward_audit AS f
                   LEFT JOIN rival_forward_model_lineage AS l
                     ON l.venue=f.venue AND l.contract=f.contract
                    AND l.quote=f.quote AND l.horizon_minutes=f.horizon_minutes
                    AND l.model_version=f.model_version
                    AND l.reference_opened_at=f.reference_opened_at
                   WHERE f.asset=? AND f.horizon_minutes=?
                     AND f.model_version=? AND f.issued_at>=?
                     AND f.issued_at<=?
                   ORDER BY f.issued_at DESC LIMIT 100""",
                (asset, horizon_minutes, MODEL,
                 (stamp - timedelta(days=7)).isoformat(), stamp.isoformat()),
            ).fetchall()
        proven = [r for r in rows if r["weights_sha256"] is not None]
        latest = rows[0] if rows else None
        latest_proven = latest is not None and latest["weights_sha256"] is not None
        return {
            "asset": asset, "horizon_minutes": horizon_minutes,
            "window": "latest 100 forecasts within 7 days",
            "forecasts": len(rows), "lineage_proven": len(proven),
            "lineage_unknown": len(rows) - len(proven),
            "historically_seeded": sum(bool(r["historical_seed_sha256"]) for r in proven),
            "latest_historical_seed_sha256": latest["historical_seed_sha256"] if latest_proven else None,
            "latest_historical_training_labels": latest["historical_training_labels"] if latest_proven else None,
            "latest_online_training_updates": latest["online_training_updates"] if latest_proven else None,
            "latest_training_labels": latest["training_labels"] if latest_proven else None,
            "latest_weights_sha256": latest["weights_sha256"] if latest_proven else None,
            "latest_lineage_verified": latest_proven,
            "forward_accuracy_verified_by_lineage": False,
        }

    def active_learning_evidence(self, *, asset, horizon_minutes, now=None):
        """Score current adaptive policies, not training counts or older seeds.

        Each venue's latest issued seed identifies its current policy. Weights
        evolve online, so this does not claim a single frozen weight snapshot
        improved. Multiple exchanges on one aligned target count as one event.
        """
        from parlay_bot.perpetuals.governance import score_paired_forward

        if asset not in ASSETS or horizon_minutes not in HORIZONS:
            raise ValueError("unsupported asset or horizon")
        self.initialize()
        stamp = utc(now or datetime.now(UTC))
        with self.store.connect() as conn:
            rows = conn.execute(
                # Start from this coin's settled forecasts (~10k rows) and
                # look each lineage row up by its primary key. Left to the
                # planner, PostgreSQL misestimated these joins as 1 row and
                # probed the forecast table once per lineage row of EVERY
                # coin (40k probes, 2-5 s). MATERIALIZED fixes the order;
                # the rows joined and the scores are unchanged.
                """WITH fx AS MATERIALIZED (
                    SELECT f.venue,f.contract,f.quote,f.reference_opened_at,
                           f.issued_at,f.target_opened_at,f.model_brier,f.baseline_brier
                    FROM perp_forward_audit f
                    WHERE f.asset=? AND f.horizon_minutes=? AND f.model_version=?
                      AND f.issued_at>=? AND f.issued_at<=? AND f.settled_at<=?
                      AND f.settled_at>f.issued_at AND f.outcome_up IN (0,1)
                      AND f.model_brier BETWEEN 0 AND 1
                      AND f.baseline_brier BETWEEN 0 AND 1
                ), fl AS MATERIALIZED (
                    SELECT fx.venue,fx.contract,fx.quote,fx.target_opened_at,
                           fx.model_brier,fx.baseline_brier,
                           COALESCE(l.historical_seed_sha256,'') AS seed
                    FROM fx JOIN rival_forward_model_lineage l ON l.venue=fx.venue
                      AND l.contract=fx.contract AND l.quote=fx.quote
                      AND l.horizon_minutes=? AND l.model_version=?
                      AND l.reference_opened_at=fx.reference_opened_at
                      AND l.issued_at=fx.issued_at
                ), latest_policy AS MATERIALIZED (
                    SELECT venue,contract,quote,MAX(issued_at) AS issued_at
                    FROM rival_forward_model_lineage
                    WHERE model_version=? AND horizon_minutes=?
                      AND issued_at<=? AND issued_at>=?
                    GROUP BY venue,contract,quote
                ), current_policy AS MATERIALIZED (
                    SELECT l.venue,l.contract,l.quote,
                           COALESCE(l.historical_seed_sha256,'') AS seed
                    FROM rival_forward_model_lineage l
                    JOIN latest_policy p ON p.venue=l.venue
                      AND p.contract=l.contract AND p.quote=l.quote
                      AND p.issued_at=l.issued_at
                    WHERE l.model_version=? AND l.horizon_minutes=?
                ), per_venue AS (
                    SELECT fl.target_opened_at, fl.venue,
                           AVG(fl.model_brier) AS model_brier,
                           AVG(fl.baseline_brier) AS baseline_brier
                    FROM fl JOIN current_policy c ON c.venue=fl.venue
                      AND c.contract=fl.contract AND c.quote=fl.quote
                      AND c.seed=fl.seed
                    GROUP BY fl.target_opened_at, fl.venue
                )
                SELECT target_opened_at, AVG(model_brier) AS model_brier,
                       AVG(baseline_brier) AS baseline_brier,
                       COUNT(*) AS independent_venues
                FROM per_venue
                -- Opposite valid venue outcomes are evidence, not a reason
                -- to drop the whole target and select only agreements.
                -- Weight each venue equally so two contracts on one venue
                -- cannot outweigh another venue's worse predictions.
                GROUP BY target_opened_at
                ORDER BY target_opened_at""",
                (asset, horizon_minutes, MODEL, (stamp-timedelta(days=14)).isoformat(),
                 stamp.isoformat(), stamp.isoformat(),
                 horizon_minutes, MODEL,
                 MODEL, horizon_minutes, stamp.isoformat(), (stamp-timedelta(days=14)).isoformat(),
                 MODEL, horizon_minutes),
            ).fetchall()
        aligned = [r for r in rows if
                   int(datetime.fromisoformat(r["target_opened_at"]).timestamp()) %
                   (60*horizon_minutes) == 0]
        base = score_paired_forward([(r["target_opened_at"], r["model_brier"],
                                      r["baseline_brier"]) for r in aligned])
        coin = score_paired_forward([(r["target_opened_at"], r["model_brier"], .25)
                                     for r in aligned])
        qualified = base.status == coin.status == "qualified_shadow"
        return {"status": "supported" if qualified else
                "insufficient" if "insufficient" in (base.status, coin.status) else "not_improving",
                "targets": base.targets, "days": base.days, "brier": base.model_brier,
                "baseline_brier": base.baseline_brier, "coin_flip_brier": .25,
                "base_rate_gain": base.paired_gain, "coin_flip_gain": coin.paired_gain,
                "reason": base.reason if base.status != "qualified_shadow" else coin.reason,
                "scope": "current adaptive policies; 14 days; aligned target groups",
                "frozen_weights_improvement_proven": False}

    def _settle_rows(self, now: datetime, pending) -> int:
        resolved = 0
        for pred in pending:
            try:
                observed = utc(datetime.fromisoformat(
                    str(pred["outcome_observed_at"])
                ))
                issued = utc(datetime.fromisoformat(str(pred["issued_at"])))
                close = utc(datetime.fromisoformat(
                    str(pred["outcome_closed_at"])
                ))
                if observed <= issued or observed > now or close >= now or issued >= close:
                    continue
                value = float(pred["outcome_close"])
                previous = float(pred["reference_close"])
                if not math.isfinite(value) or value <= 0:
                    continue
                # UP is a strict price increase. A flat close settles as
                # NOT UP instead of staying pending forever: leaving a
                # known completed target unresolved corrupts the issued
                # versus resolved accounting, and training labels ties
                # the same way so train and serve stay consistent.
                p = float(pred["probability_up"])
                b = float(pred["baseline_up"])
                if no_trade_outcome(pred):
                    # No trade happened in the target candle, so its "close"
                    # is just the previous price repeated (dYdX does this on
                    # thin markets). Grade it as settled but not comparable
                    # instead of counting a fake NOT-UP result.
                    with self.store.connect() as conn:
                        result = conn.execute(
                            """UPDATE perp_forward_audit
                               SET settled_at=?, outcome_up=NULL, realized_close=?,
                                   model_brier=NULL, baseline_brier=NULL
                               WHERE venue=? AND contract=? AND quote=?
                                 AND horizon_minutes=? AND model_version=?
                                 AND reference_opened_at=? AND settled_at IS NULL""",
                            (now.isoformat(), value, pred["venue"], pred["contract"],
                             pred["quote"], pred["horizon_minutes"],
                             pred["model_version"], pred["reference_opened_at"]),
                        )
                        resolved += int(result.rowcount == 1)
                    continue
                label = int(value > previous)
                with self.store.connect() as conn:
                    result = conn.execute(
                        """UPDATE perp_forward_audit
                           SET settled_at=?, outcome_up=?, realized_close=?,
                               model_brier=?,baseline_brier=?
                           WHERE venue=? AND contract=? AND quote=?
                             AND horizon_minutes=? AND model_version=?
                             AND reference_opened_at=? AND settled_at IS NULL""",
                        (
                            now.isoformat(), label, value, (p - label) ** 2,
                            (b - label) ** 2, pred["venue"], pred["contract"],
                            pred["quote"], pred["horizon_minutes"],
                            pred["model_version"], pred["reference_opened_at"],
                        ),
                    )
                    resolved += int(result.rowcount == 1)
            except (ValueError, TypeError, ArithmeticError):
                _LOG.warning("Prospective settlement skipped corrupt market row")
        return resolved

    def _settle(self, now: datetime) -> int:
        # Recover the OLDEST genuinely settleable outcomes first. The old
        # DESC/LIMIT queue could repeatedly skip valid old forecasts after
        # downtime. Missing target candles and retrospectively imported
        # outcomes must not occupy scarce settlement slots.
        with self.store.connect() as conn:
            pending = conn.execute(
                """SELECT f.venue,f.contract,f.quote,f.asset,f.horizon_minutes,
                          f.model_version,f.reference_opened_at,
                          f.reference_close,f.issued_at,f.target_opened_at,
                          f.probability_up,f.baseline_up,
                          c.close_price AS outcome_close,
                          c.first_observed_at AS outcome_observed_at,
                          c.closed_at AS outcome_closed_at,
                          c.trades AS outcome_trades
                   FROM perp_forward_audit AS f
                   JOIN perp_market_candles AS c
                     ON c.venue=f.venue AND c.contract=f.contract
                    AND c.quote=f.quote AND c.interval_minutes=5
                    AND c.opened_at=f.target_opened_at
                   WHERE f.settled_at IS NULL
                     AND f.target_opened_at<?
                     AND f.target_opened_at>=?
                     AND c.first_observed_at>f.issued_at
                     AND c.first_observed_at<=?
                     AND c.closed_at<?
                   ORDER BY f.target_opened_at ASC,f.venue,f.contract,
                            f.horizon_minutes
                   LIMIT ?""",
                (
                    (now - timedelta(minutes=5)).isoformat(),
                    (now - timedelta(days=2)).isoformat(),
                    now.isoformat(), now.isoformat(),
                    MAX_PENDING_SETTLEMENTS,
                ),
            ).fetchall()
        return self._settle_rows(now, pending)

    def reconcile_stale_settlements(
        self, now: datetime, *, limit: int = 200,
    ) -> int:
        """Settle forecasts stranded beyond the normal two-day scan.

        An exchange outage or a late historical recovery can leave a
        valid outcome candle available only after the target aged past
        the fast scan's window. This bounded pass revisits those rows,
        oldest first, under the SAME prospective guards as the fast scan:
        the outcome candle must have been first observed after issuance,
        so retrospectively imported outcomes never rewrite the audit as
        information available earlier. Settlement time is the actual
        reconciliation time, never backdated.
        """
        if limit < 1:
            raise ValueError("reconciliation limit must be positive")
        with self.store.connect() as conn:
            pending = conn.execute(
                """SELECT f.venue,f.contract,f.quote,f.asset,f.horizon_minutes,
                          f.model_version,f.reference_opened_at,
                          f.reference_close,f.issued_at,f.target_opened_at,
                          f.probability_up,f.baseline_up,
                          c.close_price AS outcome_close,
                          c.first_observed_at AS outcome_observed_at,
                          c.closed_at AS outcome_closed_at,
                          c.trades AS outcome_trades
                   FROM perp_forward_audit AS f
                   JOIN perp_market_candles AS c
                     ON c.venue=f.venue AND c.contract=f.contract
                    AND c.quote=f.quote AND c.interval_minutes=5
                    AND c.opened_at=f.target_opened_at
                   WHERE f.settled_at IS NULL
                     AND f.target_opened_at<?
                     AND f.target_opened_at<?
                     AND c.first_observed_at>f.issued_at
                     AND c.first_observed_at<=?
                     AND c.closed_at<?
                   ORDER BY f.target_opened_at ASC,f.venue,f.contract,
                            f.horizon_minutes
                   LIMIT ?""",
                (
                    (now - timedelta(minutes=5)).isoformat(),
                    (now - timedelta(days=2)).isoformat(),
                    now.isoformat(), now.isoformat(), limit,
                ),
            ).fetchall()
        return self._settle_rows(now, pending)

    def _mark(self) -> tuple[float, float]:
        clock = getattr(self, "_db_clock", None)
        return time.perf_counter(), clock.seconds if clock is not None else 0.0

    def _lap(self, phase: str, mark: tuple[float, float]) -> None:
        """Add wall time since ``mark`` to a phase, and the PostgreSQL share of it."""
        started, db_started = mark
        self.last_timing[phase] += time.perf_counter() - started
        clock = getattr(self, "_db_clock", None)
        if clock is not None:
            self.last_db_timing[phase] += clock.seconds - db_started

    def run_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        """One forecast cycle, with its PostgreSQL time measured per phase."""
        with database_clock() as clock:
            self._db_clock = clock
            try:
                return self._run_once(now=now)
            finally:
                self._db_clock = None

    def _run_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        stamp = utc(now or datetime.now(UTC))
        if stamp > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("prospective forecast time cannot be in the future")
        self.initialize()
        # Optional research migrations must never prevent incumbent forecasts
        # from running. Retry at the next background cycle after DB recovery.
        path_ready = bool(getattr(self.market, "_path_schema_ready", False))
        if not path_ready:
            try:
                ProspectivePathLedger(self.store).initialize()
                self.market._path_schema_ready = True
                path_ready = True
            except Exception:  # noqa: BLE001 - optional schema must fail open for incumbent
                _LOG.exception("RivaL optional path research schema unavailable")
        issued = 0
        # Seconds per phase for this cycle, logged once at the end so a slow
        # cycle names its own bottleneck (forecasts expire when it runs long).
        timing = {k: 0.0 for k in ("select", "read", "pooled", "fit", "challengers",
                                   "boosters", "write", "settle")}
        self.last_timing = timing
        self.last_db_timing = dict.fromkeys(timing, 0.0)
        mark = self._mark()
        metrics = {
            "discovered_streams": 0, "selected_streams": 0,
            "short_history": 0, "stale_last_candle": 0,
            "invalid_data": 0, "insufficient_causal_labels": 0,
            "insufficient_training": 0, "target_expired": 0,
            "duplicate_forecasts": 0, "issued_forecasts": 0,
            "settled_forecasts": 0,
        }
        # Reuse the same bounded, freshness-checked stream selection as
        # existing offline evaluation, without external HTTP requests.
        eligible = ShadowLearningMonitor(self.store)._eligible_streams(
            now=stamp, limit=40,
        )
        metrics["discovered_streams"] = len(eligible)
        # Always inspect each available asset every five-minute cycle.
        # Fairly rotate its venues without exhausting the research budget.
        selection = select_live_streams(
            eligible, slot=int(stamp.timestamp() // 300),
        )
        metrics["selected_streams"] = len(selection)
        # Pooled multi-year challenger: needs each venue's BTC stream for its
        # BTC-lead inputs. Read each venue's BTC candles at most once.
        pooled_set = self._pooled_models()
        btc_streams = {v: (c, q) for v, c, q in eligible if asset_for_contract(c) == "BTC"}
        btc_cache: dict[str, list] = {}
        # Order-flow challenger inputs: one causal snapshot per asset per cycle.
        # v2 adds OKX/Coinbase flow and spot basis; both are graded side by side.
        # Retired Nov 5, 2026 (ISSUE_FLOW_CHALLENGERS): their stored forecasts
        # and weights stay; nothing new is issued or learned.
        flows = ((FlowChallenger(self.store), FlowChallengerV2(self.store))
                 if ISSUE_FLOW_CHALLENGERS else ())
        flow_inputs: dict = {}
        cycle_assets = {a for _, c, _ in selection if (a := asset_for_contract(c)) is not None}
        for flow in flows:
            try:
                flow_inputs[flow.family] = flow.asset_inputs(stamp, cycle_assets)
            except Exception:  # noqa: BLE001 - optional challenger never blocks forecasts
                _LOG.exception("RivaL %s challenger inputs unavailable", flow.family)
        # Calibrated incumbent: refit (causally, from already-graded outcomes)
        # at most every 30 minutes; kept on the monitor across cycles.
        calib = self.__dict__.setdefault("_calibrated", CalibratedIncumbent(self.store))
        try:
            calib.refit(stamp, cycle_assets)
        except Exception:  # noqa: BLE001 - optional challenger never blocks forecasts
            _LOG.exception("RivaL calibrated challenger fit unavailable")
        self._lap("select", mark)
        # Per-stream window inputs from the previous cycle (process memory
        # only; rebuilt from candles after a restart). Bounded to the
        # streams that can be selected, so rotated-out streams are dropped.
        window_memos = getattr(self.market, "_window_feature_memos", None)
        if window_memos is None:
            window_memos = {}
            self.market._window_feature_memos = window_memos
        while len(window_memos) > MAX_STREAMS_PER_CYCLE * 2:
            window_memos.pop(next(iter(window_memos)))
        path_memos = getattr(self.market, "_path_sample_memos", None)
        if path_memos is None:
            path_memos = {}
            self.market._path_sample_memos = path_memos
        while len(path_memos) > MAX_STREAMS_PER_CYCLE * 4:
            path_memos.pop(next(iter(path_memos)))
        for venue, contract, quote in selection:
            try:
                mark = self._mark()
                rows = self.market.recent_candles(
                    venue=venue, contract=contract, quote=quote,
                    interval_minutes=5, count=1000, as_of=stamp,
                )
                self._lap("read", mark)
                if len(rows) < 65:
                    metrics["short_history"] += 1
                    continue
                candles = persisted_candles(rows)
                last = candles[-1]
                if not timedelta(0) <= stamp - utc(last.observed_at) <= timedelta(
                    minutes=10
                ) or last.trades == 0:
                    # A candle with no trades repeats the previous price; a
                    # forecast "from" it would start from a stale price.
                    metrics["stale_last_candle"] += 1
                    continue
                features = _features(candles)
                pooled_vector = None
                mark = self._mark()
                if pooled_set is not None and any(pooled_set.serving(h) for h in HORIZONS):
                    try:
                        coin = asset_for_contract(contract)
                        btc_rows = None
                        source = btc_streams.get(venue)
                        if coin != "BTC" and source is not None:
                            if venue not in btc_cache:
                                btc_cache[venue] = persisted_candles(self.market.recent_candles(
                                    venue=venue, contract=source[0], quote=source[1],
                                    interval_minutes=5, count=30, as_of=stamp,
                                ))
                            btc_rows = btc_cache[venue]
                        if coin is not None:
                            context = getattr(self.market, "binance_context", None)
                            bundle = context.bundle(coin) if context else None
                            # Per horizon: each active model gets the inputs
                            # its own version was trained on; without live
                            # Binance context the price-only model serves.
                            # Every training lineage gets its own call.
                            pooled_vector = {
                                h: [(doc, model_vector(
                                    doc, candles, asset=coin, btc_candles=btc_rows,
                                    context=bundle))
                                    for doc in pooled_set.serving(
                                        h, context=bundle is not None)]
                                for h in HORIZONS
                            }
                    except Exception:  # noqa: BLE001 - optional challenger only
                        _LOG.exception("RivaL pooled model inputs unavailable")
                self._lap("pooled", mark)
                # Shared by this stream's three horizons AND carried to the
                # next cycle: only the newest window is computed again.
                stream_key = (venue, contract, quote)
                window_inputs = WindowFeatureMemo(window_memos.pop(stream_key, None))
                for horizon in HORIZONS:
                    mark = self._mark()
                    samples = live_examples(candles, horizon_minutes=horizon,
                                            feature_cache=window_inputs)
                    if len(samples) < MIN_PREVIOUS_EVALUATED + 20:
                        metrics["insufficient_causal_labels"] += 1
                        # Feature work for a skipped horizon is still cycle time.
                        self._lap("fit", mark)
                        continue
                    tested = self.market.live_model_checkpoints.fit(samples, as_of=stamp)
                    self._lap("fit", mark)
                    mark = self._mark()
                    # Historical challengers are *shadow research* until a
                    # separate, model-SHA-specific prospective + quote/cost
                    # promotion gate authorizes serving. In particular,
                    # choose()'s shorter research screen must never replace
                    # the incumbent in the authoritative perp_forward_audit
                    # stream that drives fusion and user-facing signals.
                    # Still issue/settle immutable challenger shadow rows.
                    historical_shadows = []
                    self.market.historical_challengers.choose(
                        samples, features, last, horizon=horizon, now=stamp,
                        incumbent=tested, wall_clock=now is None,
                        shadow_candidates=historical_shadows,
                    )
                    self._lap("challengers", mark)
                    if tested.evaluated < MIN_PREVIOUS_EVALUATED:
                        metrics["insufficient_training"] += 1
                        continue
                    usable = [
                        sample for sample in samples
                        if utc(sample.label_known_at) <= stamp
                    ]
                    positive = sum(x.label_up for x in usable)
                    baseline = (positive + 5) / (len(usable) + 10)
                    probability = _probability(tested.final_weights, features)
                    asset = asset_for_contract(contract)
                    if asset is None:
                        continue
                    target_open = utc(last.opened_at) + timedelta(minutes=horizon)
                    # Training can take time. Stamp actual issuance AFTER
                    # fitting, not the timestamp captured at cycle start.
                    # Retain explicit fixture clocks only for deterministic
                    # historical tests; the production path passes no 'now'.
                    issued_at = stamp if now is not None else datetime.now(UTC)
                    if issued_at >= target_open + timedelta(
                        minutes=5, milliseconds=-1,
                    ):
                        metrics["target_expired"] += 1
                        continue
                    # Fetch immutable historical artifacts OUTSIDE the forecast
                    # transaction. A blocked Drive/database read must never
                    # roll back the existing incumbent. Missing source-exact
                    # artifacts simply mean no boosted shadow prediction.
                    boosters = ()
                    mark = self._mark()
                    try:
                        boosters = self.market.historical_challengers.registry.compatible_boosters(
                            venue=venue, contract=contract, quote=quote,
                            horizon=horizon, now=issued_at,
                        )
                    except Exception:  # noqa: BLE001 - optional research inputs
                        _LOG.exception("RivaL boosted artifact lookup unavailable")
                    self._lap("boosters", mark)
                    mark = self._mark()
                    # Store regime and model fingerprints BEFORE the target
                    # closes, in the same DB transaction as the forecast.
                    # Existing predictions are never retro-tagged using
                    # future outcomes or rewritten during retraining.
                    regime = issue_regime(features)
                    feature_hash = hashlib.sha256(json.dumps(
                        features, separators=(",", ":"), allow_nan=False,
                    ).encode()).hexdigest()
                    weight_hash = hashlib.sha256(json.dumps(
                        tested.final_weights, separators=(",", ":"),
                        allow_nan=False,
                    ).encode()).hexdigest()
                    # Shadow-model fitting is CPU work on inputs already
                    # fixed at issuance: do it before the transaction opens.
                    try:
                        shadow_prepared = ShadowModelCompetition.prepare(
                            samples, features, now=stamp)
                    except Exception:  # noqa: BLE001 - optional challenger
                        shadow_prepared = None  # issue() recomputes and isolates it
                    # Path priors likewise: CPU work, before the transaction,
                    # with each reference bar's samples carried across cycles.
                    path_options = None
                    if path_ready and horizon in (15, 60):
                        memo_key = (venue, contract, quote, horizon)
                        path_memo = path_memos.pop(memo_key, None) or {}
                        try:
                            path_options = empirical_paths(
                                candles, horizon=horizon, now=stamp, memo=path_memo)
                        except Exception:  # noqa: BLE001 - issue() recomputes and isolates it
                            path_options = None
                        prune_path_memo(path_memo, candles)
                        path_memos[memo_key] = path_memo
                    with self.store.connect() as conn:
                        record = conn.execute(
                            """INSERT INTO perp_forward_audit (
                                 venue,contract,quote,asset,horizon_minutes,
                                 model_version,reference_opened_at,reference_close,
                                 issued_at,target_opened_at,probability_up,
                                 baseline_up,training_evaluated
                               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT DO NOTHING""",
                            (
                                venue, contract, quote, asset, horizon, MODEL,
                                utc(last.opened_at).isoformat(), last.close_price,
                                issued_at.isoformat(), target_open.isoformat(),
                                probability, baseline, tested.evaluated,
                            ),
                        )
                        if record.rowcount == 1:
                            conn.execute(
                                """INSERT INTO perp_forward_regimes (
                                     venue,contract,quote,horizon_minutes,
                                     model_version,reference_opened_at,issued_at,
                                     method_version,regime,five_min_volatility_pct,
                                     feature_sha256,weights_sha256
                                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (venue, contract, quote, horizon, MODEL,
                                 utc(last.opened_at).isoformat(), issued_at.isoformat(),
                                 REGIME_VERSION, regime, features[2],
                                 feature_hash, weight_hash),
                            )
                            # Immutable, source-matched audit of what the actual
                            # forecast weights had learned at issuance. A six-month
                            # archive does not qualify as a seed by mere existence.
                            conn.execute(
                                """INSERT INTO rival_forward_model_lineage (
                                     venue,contract,quote,horizon_minutes,
                                     model_version,reference_opened_at,issued_at,
                                     weights_sha256,feature_sha256,training_labels,
                                     historical_training_labels,online_training_updates,
                                     historical_seed_sha256
                                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (venue, contract, quote, horizon, MODEL,
                                 utc(last.opened_at).isoformat(), issued_at.isoformat(),
                                 weight_hash, feature_hash,
                                 tested.training_labels,
                                 tested.historical_training_labels,
                                 tested.updates,
                                 tested.historical_seed_sha256),
                            )
                            # New models issue alongside, not in place of,
                            # the existing live model. Same transaction and
                            # immutable target/venue identity as the incumbent.
                            # Isolate optional model failures from the
                            # authoritative incumbent insert on both backends.
                            # PostgreSQL transactions must clear a failed
                            # statement before they can commit incumbent work.
                            conn.execute("SAVEPOINT rival_shadow_prediction")
                            try:
                                flow_calls = tuple(
                                    call for flow in flows
                                    if asset in flow_inputs.get(flow.family, {})
                                    and (call := flow.call(
                                        conn, venue=venue, contract=contract, quote=quote,
                                        asset=asset, horizon=horizon,
                                        reference_opened_at=utc(last.opened_at),
                                        issued_at=issued_at, features=features,
                                        flow=flow_inputs[flow.family][asset])))
                                calib_call = calib.call(asset=asset, horizon=horizon,
                                                        incumbent_up=probability,
                                                        baseline_up=baseline)
                                if calib_call is not None:
                                    flow_calls = (*flow_calls, calib_call)
                                ShadowModelCompetition(self.store).issue(
                                    conn, samples=samples, features=features,
                                    now=stamp, venue=venue, contract=contract, quote=quote,
                                    asset=asset, horizon=horizon,
                                    reference_opened_at=last.opened_at,
                                    target_opened_at=target_open, issued_at=issued_at,
                                    incumbent_up=probability, baseline_up=baseline,
                                    boosters=boosters,
                                    historical_candidates=historical_shadows,
                                    pooled=self._pooled_calls(pooled_set, horizon, pooled_vector),
                                    challengers=flow_calls,
                                    prepared=shadow_prepared,
                                )
                            except Exception:  # noqa: BLE001 - optional challenger
                                conn.execute("ROLLBACK TO SAVEPOINT rival_shadow_prediction")
                                _LOG.exception("RivaL shadow model failed; incumbent preserved")
                            finally:
                                conn.execute("RELEASE SAVEPOINT rival_shadow_prediction")
                            if path_ready and horizon in (15, 60):
                                # These are independent paper outcomes, not
                                # trading signals; a malformed research model
                                # cannot roll back the incumbent transaction.
                                conn.execute("SAVEPOINT rival_path_shadow")
                                try:
                                    ProspectivePathLedger(self.store).issue(
                                        conn, candles=candles, asset=asset,
                                        horizon=horizon, now=stamp, issued_at=issued_at,
                                        options=path_options,
                                    )
                                except Exception:  # noqa: BLE001 - optional path learner
                                    conn.execute("ROLLBACK TO SAVEPOINT rival_path_shadow")
                                    _LOG.exception("RivaL path research issue unavailable")
                                finally:
                                    conn.execute("RELEASE SAVEPOINT rival_path_shadow")
                        issued += int(record.rowcount == 1)
                        metrics["duplicate_forecasts"] += int(record.rowcount == 0)
                    self._lap("write", mark)
                window_memos[stream_key] = window_inputs.current
            except (ValueError, TypeError, ArithmeticError) as exc:
                metrics["invalid_data"] += 1
                _LOG.warning(
                    "Prospective issue skipped invalid venue=%s contract=%s reason=%s: %s",
                    venue, contract, type(exc).__name__, str(exc)[:240],
                )
                continue
        metrics["issued_forecasts"] = issued
        # One bounded, fair source-specific proof review per hour, including
        # all three horizons. Other venues remain in immutable forward research
        # but cannot earn after-cost proof without a real matching quote feed.
        if stamp.minute < 5:
            proof_scope = select_family_proof_scope(
                selection, hour_slot=int(stamp.timestamp() // 3600),
            )
            if proof_scope is None:
                _LOG.info(
                    "RivaL after-cost proof deferred: no supported source "
                    "with observable matched bid/ask quotes"
                )
            else:
                venue, contract, currency, horizon = proof_scope
                asset = asset_for_contract(contract)
                if asset is not None:
                    try:
                        family_status = FamilyPromotion(self.store).evaluate(
                            asset=asset, venue=venue, contract=contract,
                            quote=currency, horizon=horizon, now=stamp,
                        )
                        _LOG.info(
                            "RivaL family promotion gate venue=%s asset=%s horizon=%s "
                            "status=%s family=%s",
                            venue, asset, horizon, family_status["status"],
                            family_status["selected_family"] or "none",
                        )
                    except Exception:  # noqa: BLE001 - optional shadow governance
                        _LOG.exception(
                            "RivaL source-specific family gate unavailable"
                        )
        # Grade older forecasts only AFTER issuing this slot's forecasts.
        # Settlement used to run first and pushed issuance a median 85 s
        # (up to 5 min) into the target window, so many forecasts expired.
        # Issuance reads candles, not settlements, so nothing it uses changes.
        mark = self._mark()
        settled = self._settle(stamp)
        for flow in flows:
            try:
                learned = flow.learn(stamp)
                if learned:
                    _LOG.info("RivaL %s challenger learned from %d graded outcomes",
                              flow.family, learned)
            except Exception:  # noqa: BLE001 - optional challenger never blocks settlement
                _LOG.exception("RivaL %s challenger learning unavailable", flow.family)
        # Causal shadow forecasts settle only against already-verified matching
        # incumbent outcomes. No new provider calls, Discord messages or fills.
        try:
            # Fast pass over recent targets every cycle; once an hour, also
            # the full history, so targets whose incumbent settled late (stale
            # reconciliation runs in the same hourly slot) are still graded.
            competition = ShadowModelCompetition(self.store)
            competition.settle(now=stamp, since=stamp - SHADOW_FAST_SETTLE_WINDOW)
            if stamp.minute < 5:
                competition.settle(now=stamp)
        except Exception:  # noqa: BLE001 - shadow research never blocks the live learner
            _LOG.exception("RivaL multi-model shadow settlement unavailable")
        if path_ready:
            try:
                path_results = ProspectivePathLedger(self.store).settle(now=stamp)
                if any(path_results.values()):
                    _LOG.info("RivaL path outcomes verified %s", path_results)
            except Exception:  # noqa: BLE001 - research cannot block forecasts
                _LOG.exception("RivaL source-matched path settlement unavailable")
        if stamp.minute < 5:
            try:
                reconciled = self.reconcile_stale_settlements(stamp)
                if reconciled:
                    _LOG.info(
                        "RivaL stale settlement reconciliation settled=%d",
                        reconciled,
                    )
            except Exception:  # noqa: BLE001 - background repair never blocks issuance
                _LOG.exception("RivaL stale settlement reconciliation failed")
        metrics["settled_forecasts"] = settled
        self._lap("settle", mark)
        clock = self._db_clock
        # Wall time per phase, then the PostgreSQL part of each (execute +
        # commit + pool wait); the difference is Python work in that phase.
        _LOG.info("RivaL forward cycle timing %s | db %s | statements=%d commits=%d "
                  "checkouts=%d pool_wait=%.1fs",
                  " ".join(f"{k}={v:.1f}s" for k, v in timing.items()),
                  " ".join(f"{k}={v:.1f}s" for k, v in self.last_db_timing.items()),
                  clock.statements if clock else 0, clock.commits if clock else 0,
                  clock.checkouts if clock else 0, clock.wait_seconds if clock else 0.0)
        self._record_cycle(stamp, metrics)
        # The table alone does not explain a bare menu: log the gate
        # breakdown so the next empty-signal report names its cause.
        _LOG.info(
            "RivaL forward cycle discovered=%d selected=%d short=%d "
            "stale=%d insufficient_labels=%d insufficient_training=%d "
            "expired=%d duplicate=%d issued=%d",
            metrics["discovered_streams"], metrics["selected_streams"],
            metrics["short_history"], metrics["stale_last_candle"],
            metrics["insufficient_causal_labels"],
            metrics["insufficient_training"], metrics["target_expired"],
            metrics["duplicate_forecasts"], metrics["issued_forecasts"],
        )
        _LOG.info(
            "RivaL prospective shadow audit issued=%s settled=%s",
            issued, settled,
        )
        return issued, settled

    def recent_issued(self, *, asset: str, limit: int = 3) -> list[dict]:
        """Read actual immutable forecast rows; never synthesize predictions."""
        if asset not in ASSETS or not 1 <= limit <= 8:
            raise ValueError("invalid recent prediction request")
        self.initialize()
        with self.store.connect() as conn:
            records = conn.execute(
                """SELECT venue,horizon_minutes,model_version,
                          issued_at,target_opened_at,probability_up,
                          settled_at,outcome_up,model_brier,baseline_brier
                   FROM perp_forward_audit
                   WHERE asset=? AND model_version=?
                   ORDER BY issued_at DESC LIMIT ?""",
                (asset, MODEL, limit),
            ).fetchall()
        return [dict(row) for row in records]

    def regime_summary(
        self, *, asset: str, horizon_minutes: int,
        now: datetime | None = None,
    ) -> dict:
        """Prospective skill sliced by regime known when prediction was saved."""
        if asset not in ASSETS or horizon_minutes not in HORIZONS:
            raise ValueError("unsupported regime query")
        stamp = utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT f.target_opened_at,r.regime,
                          MIN(f.issued_at) AS issued_at,
                          MAX(f.settled_at) AS settled_at,
                          AVG(f.model_brier) AS model_brier,
                          AVG(f.baseline_brier) AS baseline_brier
                   FROM perp_forward_audit AS f
                   JOIN perp_forward_regimes AS r
                     ON r.venue=f.venue AND r.contract=f.contract
                    AND r.quote=f.quote AND r.horizon_minutes=f.horizon_minutes
                    AND r.model_version=f.model_version
                    AND r.reference_opened_at=f.reference_opened_at
                   WHERE f.asset=? AND f.horizon_minutes=?
                     AND f.model_version=? AND r.method_version=?
                     AND f.settled_at IS NOT NULL
                     AND f.outcome_up IS NOT NULL
                     AND f.target_opened_at>=?
                     AND f.target_opened_at<=?
                   GROUP BY f.target_opened_at,r.regime
                   ORDER BY f.target_opened_at DESC LIMIT 9000""",
                (asset, horizon_minutes, MODEL, REGIME_VERSION,
                 (stamp - timedelta(days=35)).isoformat(), stamp.isoformat()),
            ).fetchall()
        return evaluate_regimes([dict(row) for row in rows], as_of=stamp)

    def boosted_production_evidence(self, *, asset: str, now: datetime | None = None) -> dict:
        """Read only ACTUAL imported model, frozen future, and book-quote rows.

        This is a host-DB delivery/forward canary, not a backtest and not a
        profitable-trading claim. Never treat historical research uploads,
        snapshots from other venues, or simulated costs as actual fills.
        """
        if asset not in ASSETS:
            raise ValueError("unsupported model evidence asset")
        stamp = utc(now or datetime.now(UTC))
        cutoff = (stamp - timedelta(days=30)).isoformat()
        from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
        from parlay_bot.research.candidate_registry import ResearchCandidateRegistry

        self.initialize()
        FamilyQuoteShadow(self.store).initialize()
        FamilyPromotion(self.store).initialize()
        ResearchCandidateRegistry(self.store).initialize()
        with self.store.connect() as conn:
            imported = conn.execute(
                """SELECT COUNT(*) AS total,
                    SUM(CASE WHEN c.retrospective_screen='passed'
                         AND a.compatible=1 THEN 1 ELSE 0 END) AS screened
                   FROM rival_offline_candidates c
                   JOIN rival_offline_model_artifacts a
                    ON a.model_sha256=c.model_sha256
                   WHERE c.asset=? AND a.model_spec_json
                    LIKE '%"model":"portable-boosted-binary-v1"%'""",
                (asset,),
            ).fetchone()
            # Exact rejection counts from the imported immutable model
            # registry. Zero screened is not a pipeline error by itself:
            # these candidates must pass six source months, purged validation,
            # holdout advantage and calibration before shadow issuance.
            blockers = conn.execute(
                """SELECT COALESCE(c.admission_reason, 'reason_unrecorded')
                              AS reason, COUNT(*) AS total
                   FROM rival_offline_candidates c
                   JOIN rival_offline_model_artifacts a
                     ON a.model_sha256=c.model_sha256
                   WHERE c.asset=?
                     AND a.model_spec_json
                       LIKE '%"model":"portable-boosted-binary-v1"%'
                     AND (c.retrospective_screen!='passed' OR a.compatible<>1)
                   GROUP BY COALESCE(c.admission_reason, 'reason_unrecorded')
                   ORDER BY total DESC, reason LIMIT 8""",
                (asset,),
            ).fetchall()
            forecasts = conn.execute(
                """SELECT s.venue,s.contract,s.quote,s.horizon_minutes,s.family,
                          COUNT(*) AS issued,
                          SUM(CASE WHEN s.settled_at IS NOT NULL
                                   AND s.outcome_up IN (0,1)
                                   AND s.model_brier IS NOT NULL
                                   THEN 1 ELSE 0 END) AS settled,
                          MAX(s.issued_at) AS last_issued_at,
                          MAX(s.settled_at) AS last_settled_at
                   FROM rival_model_shadow_forecasts s
                   WHERE s.asset=? AND s.family LIKE 'boosted_%'
                     AND s.issued_at>=? AND s.issued_at<=?
                   GROUP BY s.venue,s.contract,s.quote,s.horizon_minutes,s.family""",
                (asset,cutoff,stamp.isoformat()),
            ).fetchall()
            quotes = conn.execute(
                """SELECT venue,contract,quote,horizon_minutes,family,
                          COUNT(*) AS observed_entries,
                          SUM(CASE WHEN state='EXIT_QUOTED'
                                   THEN 1 ELSE 0 END) AS observed_exits,
                          SUM(CASE WHEN state='MISSED_EXIT'
                                   THEN 1 ELSE 0 END) AS missing_exits
                   FROM rival_family_quote_shadow
                   WHERE asset=? AND family LIKE 'boosted_%'
                    AND issued_at>=? AND issued_at<=?
                   GROUP BY venue,contract,quote,horizon_minutes,family""",
                (asset,cutoff,stamp.isoformat()),
            ).fetchall()
            selections = conn.execute(
                """SELECT venue,contract,quote,horizon_minutes,selected_family,
                          status,evaluated_at FROM rival_family_promotion
                   WHERE asset=? AND selected_family LIKE 'boosted_%'""",
                (asset,),
            ).fetchall()
        keys = ("venue","contract","quote","horizon_minutes","family")
        by_quote = {tuple(q[k] for k in keys): q for q in quotes}
        scopes = []
        for row in forecasts:
            key = tuple(row[k] for k in keys)
            quote = by_quote.get(key)
            scopes.append({
                "venue": str(row["venue"]), "contract": str(row["contract"]),
                "quote": str(row["quote"]), "horizon": int(row["horizon_minutes"]),
                "family": str(row["family"]), "issued": int(row["issued"]),
                "settled": int(row["settled"] or 0),
                "quote_entries": int(quote["observed_entries"]) if quote else 0,
                "quote_exits": int(quote["observed_exits"] or 0) if quote else 0,
                "missing_exits": int(quote["missing_exits"] or 0) if quote else 0,
                "last_issued_at": str(row["last_issued_at"]),
                "last_settled_at": str(row["last_settled_at"])
                                   if row["last_settled_at"] else None,
            })
        scopes.sort(key=lambda x: (x["last_issued_at"], x["issued"]), reverse=True)
        confirmed_paper = []
        for row in selections:
            try:
                checked = utc(datetime.fromisoformat(str(row["evaluated_at"])))
            except (ValueError,TypeError,OverflowError):
                continue
            if (row["status"] == "promoted_paper" and
                timedelta(0) <= stamp-checked <= timedelta(hours=48) and
                any(s["venue"] == row["venue"] and
                    s["contract"] == row["contract"] and
                    s["quote"] == row["quote"] and
                    s["horizon"] == row["horizon_minutes"] and
                    s["family"] == row["selected_family"] for s in scopes)):
                confirmed_paper.append({
                    "venue": str(row["venue"]), "contract": str(row["contract"]),
                    "horizon": int(row["horizon_minutes"]),
                    "family": str(row["selected_family"]),
                    "checked_at": checked.isoformat(),
                })
        issued = sum(x["issued"] for x in scopes)
        settled = sum(x["settled"] for x in scopes)
        exits = sum(x["quote_exits"] for x in scopes)
        qualified = int(imported["screened"] or 0)
        # Note: paper promotion is not authenticated real-money cost proof.
        if not qualified:
            reason = "None of the models built from history has passed screening"
        elif not issued:
            reason = "Imported trees await source-matched genuine future forecasts"
        elif not settled:
            reason = "Immutable boosted forecasts await independently observed future outcomes"
        elif not exits:
            reason = "Boosted forecasts settled; matched entry and exit book quotes missing"
        elif not confirmed_paper:
            reason = "Future and quote data exist; 21-day severe after-cost paper proof not qualified"
        else:
            reason = "Fresh model-family PAPER qualification recorded; actual fills still unverified"
        return {
            "as_of": stamp.isoformat(), "window_days": 30, "asset": asset,
            "portable_imported": int(imported["total"] or 0),
            "portable_screened": qualified,
            "screening_blockers": [
                {"reason": str(row["reason"]), "count": int(row["total"])}
                for row in blockers
            ],
            "issued": issued, "settled": settled,
            "quote_entries": sum(x["quote_entries"] for x in scopes),
            "quote_exits": exits,
            "missing_exits": sum(x["missing_exits"] for x in scopes),
            "scopes": scopes[:8], "scopes_count": len(scopes),
            "fresh_paper_promotions": confirmed_paper,
            "actual_fills_verified": False,
            "actual_fees_funding_slippage_verified": False,
            "status_reason": reason,
        }

    def summary(self, *, asset: str, now: datetime | None = None) -> dict:
        """Aggregate valid paired future scores without counting missing errors as zero.

        Across nine collecting exchanges, two horizons and two weeks this otherwise
        returns hundreds of thousands of old forecast rows for one click.
        (Binance is quote-only, not a tenth collector.)
        """
        if asset not in ASSETS:
            raise ValueError("invalid asset")
        stamp = utc(now or datetime.now(UTC))
        start = (stamp - timedelta(days=7)).isoformat()
        prior = (stamp - timedelta(days=14)).isoformat()
        due_cut = (stamp - SETTLEMENT_DUE_AFTER).isoformat()
        overdue_cut = (stamp - SETTLEMENT_OVERDUE_AFTER).isoformat()
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT horizon_minutes,
                           CASE WHEN issued_at>=? THEN 'recent' ELSE 'prior' END AS cohort,
                           COUNT(*) AS issued, MIN(issued_at) AS first_issued,
                           SUM(CASE WHEN issued_at>=target_opened_at THEN 1 ELSE 0 END) AS issued_late,
                           SUM(CASE WHEN target_opened_at<=? THEN 1 ELSE 0 END) AS due,
                           SUM(CASE WHEN settled_at IS NULL AND target_opened_at<=? THEN 1 ELSE 0 END) AS overdue,
                           COUNT(DISTINCT CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN target_opened_at END) AS windows,
                           SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled,
                           SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN 1 ELSE 0 END) AS scored,
                           SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 AND probability_up<>0.5 THEN 1 ELSE 0 END) AS decided,
                           SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 AND
                                ((probability_up>0.5 AND outcome_up=1)
                                 OR (probability_up<0.5 AND outcome_up=0))
                                THEN 1 ELSE 0 END) AS correct,
                           SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN model_brier ELSE 0 END) AS model_sum,
                           SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN baseline_brier ELSE 0 END) AS baseline_sum,
                           COUNT(DISTINCT CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN venue END) AS venues
                    FROM perp_forward_audit
                    WHERE asset=? AND model_version=?
                      AND issued_at>=? AND issued_at<=?
                   GROUP BY horizon_minutes, cohort""",
                (start, due_cut, overdue_cut, asset, MODEL, prior, stamp.isoformat()),
            ).fetchall()

        def empty() -> dict:
            return {
                "issued": 0, "first_issued": None, "issued_late": 0,
                "due": 0, "overdue": 0, "windows": 0,
                "settled": 0, "scored": 0, "decided": 0, "correct": 0,
                "hit_rate": None, "brier": None, "baseline_brier": None,
                "venues": 0, "model_beats_baseline": None,
            }

        cohorts = {
            "recent": {h: empty() for h in HORIZONS},
            "prior": {h: empty() for h in HORIZONS},
        }
        for row in rows:
            horizon = int(row["horizon_minutes"])
            cohort = str(row["cohort"])
            if horizon not in HORIZONS or cohort not in cohorts:
                continue
            n = int(row["scored"] or 0)
            settled = int(row["settled"] or 0)
            decided = int(row["decided"] or 0)
            brier = float(row["model_sum"]) / n if n else None
            baseline = float(row["baseline_sum"]) / n if n else None
            cohorts[cohort][horizon] = {
                "issued": int(row["issued"]), "first_issued": row["first_issued"],
                "issued_late": int(row["issued_late"] or 0),
                "due": int(row["due"] or 0),
                "overdue": int(row["overdue"] or 0), "windows": int(row["windows"] or 0),
                "settled": settled,
                "scored": n, "decided": decided, "correct": int(row["correct"] or 0),
                "hit_rate": int(row["correct"] or 0) / decided if decided else None,
                "brier": brier, "baseline_brier": baseline,
                "venues": int(row["venues"] or 0),
                "model_beats_baseline": (
                    # Rows settled without a gradable outcome (no trade in the
                    # result candle) are excluded from n; they no longer
                    # withhold the display comparison. Signal gating still
                    # uses venue_summary's stricter rule.
                    brier < baseline if n >= 50 and brier is not None
                    and baseline is not None else None
                ),
            }
        return {
            "asset": asset, "as_of": stamp, "model": MODEL,
            "last_7d": cohorts["recent"], "prior_7d": cohorts["prior"],
            "threshold": 50, "independent_trials": False,
        }

    def venue_summary(
        self, *, asset: str, venue: str, horizon: int,
        now: datetime | None = None, model_version: str | None = None,
    ) -> dict:
        """Same-venue, same-model seven-day results and conservative WATCH proof.

        A positive mean over 50 rows is a descriptive score, not evidence
        of a repeatable advantage. WATCH additionally requires a positive
        day-block lower bound and observations spanning five valid UTC days.
        Scoring remains read-only; no model promotion is inferred.
        """
        if asset not in ASSETS:
            raise ValueError("invalid asset")
        if horizon not in HORIZONS:
            raise ValueError("invalid horizon")
        if model_version is not None and not model_version.strip():
            raise ValueError("invalid model version")
        # Never mix retired model versions into one venue's hit rate.
        model_version = model_version or MODEL
        stamp = utc(now or datetime.now(UTC))
        start = (stamp - timedelta(days=7)).isoformat()
        version_clause = " AND model_version=?" if model_version is not None else ""
        params = (asset, venue, horizon, start, stamp.isoformat())
        if model_version is not None:
            params += (model_version,)
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS issued,
                          SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled,
                          SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN 1 ELSE 0 END) AS scored,
                          SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 AND probability_up<>0.5 THEN 1 ELSE 0 END) AS decided,
                          SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 AND
                               ((probability_up>0.5 AND outcome_up=1)
                                OR (probability_up<0.5 AND outcome_up=0))
                               THEN 1 ELSE 0 END) AS correct,
                          SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN model_brier ELSE 0 END) AS model_sum,
                          SUM(CASE WHEN settled_at IS NOT NULL AND outcome_up IN (0,1) AND probability_up BETWEEN 0 AND 1 AND model_brier BETWEEN 0 AND 1 AND baseline_brier BETWEEN 0 AND 1 THEN baseline_brier ELSE 0 END) AS baseline_sum
                   FROM perp_forward_audit
                   WHERE asset=? AND venue=? AND horizon_minutes=?
                     AND issued_at>=? AND issued_at<=?"""
                + version_clause,
                params,
            ).fetchone()
            daily = conn.execute(
                """SELECT substr(issued_at,1,10) AS issue_day,
                          COUNT(*) AS n,
                          SUM(model_brier) AS model_sum,
                          SUM(baseline_brier) AS baseline_sum,
                          SUM(CASE WHEN model_brier IS NULL
                                    OR baseline_brier IS NULL
                                    OR outcome_up IS NULL
                                    OR outcome_up NOT IN (0,1)
                                    OR probability_up NOT BETWEEN 0 AND 1
                                    OR model_brier NOT BETWEEN 0 AND 1
                                    OR baseline_brier NOT BETWEEN 0 AND 1
                                   THEN 1 ELSE 0 END) AS invalid_rows
                   FROM perp_forward_audit
                   WHERE asset=? AND venue=? AND horizon_minutes=?
                     AND issued_at>=? AND issued_at<=?
                     AND settled_at IS NOT NULL"""
                + version_clause
                + " GROUP BY substr(issued_at,1,10)",
                params,
            ).fetchall()
        settled = int(row["settled"] or 0)
        n = int(row["scored"] or 0)
        decided = int(row["decided"] or 0)
        brier = (float(row["model_sum"]) / n
                 if n and row["model_sum"] is not None else None)
        baseline = (float(row["baseline_sum"]) / n
                    if n and row["baseline_sum"] is not None else None)
        if brier is not None and not math.isfinite(brier):
            brier = None
        if baseline is not None and not math.isfinite(baseline):
            baseline = None
        block_proof = day_block_watch_evidence(daily)
        return {
            "issued": int(row["issued"]), "settled": settled,
            "scored": n,
            "hit_rate": int(row["correct"] or 0) / decided if decided else None,
            "brier": brier, "baseline_brier": baseline,
            "model_beats_baseline": (
                brier < baseline if n >= 50 and n == settled and brier is not None
                and baseline is not None else None
            ),
            "model_version": model_version,
            **block_proof,
        }
