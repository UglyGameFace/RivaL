"""Immutable, truly-forward, read-only accuracy audit for RivaL.

A forecast must exist in PostgreSQL before the target bar was observed.
Never retroactively turn backfilled candles into prospective predictions.
Uses only model weights trained on outcomes already known at issuance.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import LOOKBACK, live_examples, persisted_candles
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import walk_forward
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.regime_evidence import VERSION as REGIME_VERSION
from parlay_bot.perpetuals.regime_evidence import evaluate_regimes, issue_regime
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor

_LOG = logging.getLogger(__name__)
MODEL = "prospective-logistic-shadow-v1"
HORIZONS = (5, 15)
MIN_PREVIOUS_EVALUATED = 20
MAX_STREAMS_PER_CYCLE = 10
MAX_PENDING_SETTLEMENTS = 120


def _features(candles) -> tuple[float, ...]:
    """Match the causal feature vector in live_examples() for the last bar."""
    window = tuple(candles[-LOOKBACK - 1:])
    if len(window) != LOOKBACK + 1:
        raise ValueError("not enough point-in-time candles")
    for left, right in itertools.pairwise(window):
        if (
            (utc(right.opened_at) - utc(left.opened_at)).total_seconds() != 300
            or (left.venue, left.contract, left.quote) !=
            (right.venue, right.contract, right.quote)
        ):
            raise ValueError("latest feature window is not continuous")
    closes = [x.close_price for x in window]
    if any(not math.isfinite(x) or x <= 0 for x in closes):
        raise ValueError("invalid prospective prices")
    returns = [math.log(b / a) for a, b in itertools.pairwise(closes)]
    avg = sum(returns) / len(returns)
    variance = sum((v - avg) ** 2 for v in returns) / len(returns)
    current = window[-1]
    volume_avg = sum(x.volume_base for x in window[:-1]) / LOOKBACK
    return (
        max(-5.0, min(5.0, returns[-1] * 100)),
        max(-5.0, min(5.0, math.log(closes[-1] / closes[0]) * 100)),
        max(0.0, min(5.0, math.sqrt(variance) * 100)),
        max(0.0, min(5.0, (current.high_price - current.low_price)
                         / current.close_price * 100)),
        max(-2.0, min(2.0, math.log((current.volume_base + 1)
                                   / (volume_avg + 1)))),
    )


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

    A ten-venue BTC block must not starve ETH/SOL predictions just because
    the venue-ordered SQL result is sliced into fixed 15-minute groups.
    Rotate each coin's venue list between cycles for source fairness.
    """
    if slot < 0 or not 1 <= limit <= MAX_STREAMS_PER_CYCLE:
        raise ValueError("invalid live forecast selection")
    buckets: dict[str, list[tuple[str, str, str]]] = {
        "BTC": [], "ETH": [], "SOL": [],
    }
    for entry in eligible:
        contract = entry[1]
        asset = ("BTC" if contract.startswith(("BTC", "XBT"))
                 else "ETH" if contract.startswith("ETH")
                 else "SOL" if contract.startswith("SOL") else None)
        if asset is not None:
            buckets[asset].append(entry)
    for asset, streams in buckets.items():
        if streams:
            offset = slot % len(streams)
            buckets[asset] = streams[offset:] + streams[:offset]
    selected: list[tuple[str, str, str]] = []
    while len(selected) < limit and any(buckets.values()):
        for asset in ("BTC", "ETH", "SOL"):
            if buckets[asset]:
                selected.append(buckets[asset].pop(0))
                if len(selected) == limit:
                    break
    return selected


class ForwardAccuracyMonitor:
    """Issue, settle and score immutable prospective research only."""

    def __init__(self, market: PerpetualMarketStore) -> None:
        self.market = market
        self.store = market.store

    def initialize(self) -> None:
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
                CREATE INDEX IF NOT EXISTS idx_perp_forward_regime_method
                    ON perp_forward_regimes (method_version,regime);
                CREATE INDEX IF NOT EXISTS idx_perp_forward_pending
                    ON perp_forward_audit (settled_at,target_opened_at);
                CREATE INDEX IF NOT EXISTS idx_perp_forward_asset_horizon
                    ON perp_forward_audit (asset,horizon_minutes,issued_at);
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
                          c.closed_at AS outcome_closed_at
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
                     AND c.close_price<>f.reference_close
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
                if observed <= issued or observed > now or close >= now:
                    continue
                value = float(pred["outcome_close"])
                previous = float(pred["reference_close"])
                if not math.isfinite(value) or value <= 0 or value == previous:
                    continue  # Flat moves remain unresolved, never forced to up/down.
                label = int(value > previous)
                p = float(pred["probability_up"])
                b = float(pred["baseline_up"])
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

    def run_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        stamp = utc(now or datetime.now(UTC))
        if stamp > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("prospective forecast time cannot be in the future")
        self.initialize()
        settled = self._settle(stamp)
        issued = 0
        metrics = {
            "discovered_streams": 0, "selected_streams": 0,
            "short_history": 0, "stale_last_candle": 0,
            "invalid_data": 0, "insufficient_causal_labels": 0,
            "insufficient_training": 0, "target_expired": 0,
            "duplicate_forecasts": 0, "issued_forecasts": 0,
            "settled_forecasts": settled,
        }
        # Reuse the same bounded, freshness-checked stream selection as
        # existing offline evaluation, without external HTTP requests.
        eligible = ShadowLearningMonitor(self.store)._eligible_streams(
            now=stamp, limit=30,
        )
        metrics["discovered_streams"] = len(eligible)
        # Always inspect each available asset every five-minute cycle.
        # Fairly rotate its venues without exhausting the research budget.
        selection = select_live_streams(
            eligible, slot=int(stamp.timestamp() // 300),
        )
        metrics["selected_streams"] = len(selection)
        for venue, contract, quote in selection:
            try:
                rows = self.market.recent_candles(
                    venue=venue, contract=contract, quote=quote,
                    interval_minutes=5, count=1000, as_of=stamp,
                )
                if len(rows) < 65:
                    metrics["short_history"] += 1
                    continue
                candles = persisted_candles(rows)
                last = candles[-1]
                if not timedelta(0) <= stamp - utc(last.observed_at) <= timedelta(
                    minutes=10
                ):
                    metrics["stale_last_candle"] += 1
                    continue
                features = _features(candles)
                for horizon in HORIZONS:
                    samples = live_examples(candles, horizon_minutes=horizon)
                    if len(samples) < MIN_PREVIOUS_EVALUATED + 20:
                        metrics["insufficient_causal_labels"] += 1
                        continue
                    tested = walk_forward(
                        samples, as_of=stamp, minimum_evaluated=50,
                    )
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
                    asset = ("BTC" if contract.startswith(("BTC", "XBT"))
                             else "ETH" if contract.startswith("ETH")
                             else "SOL" if contract.startswith("SOL") else None)
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
                        issued += int(record.rowcount == 1)
                        metrics["duplicate_forecasts"] += int(record.rowcount == 0)
            except (ValueError, TypeError, ArithmeticError):
                metrics["invalid_data"] += 1
                _LOG.warning(
                    "Prospective issue skipped invalid venue=%s contract=%s",
                    venue, contract,
                )
                continue
        metrics["issued_forecasts"] = issued
        self._record_cycle(stamp, metrics)
        _LOG.info(
            "RivaL prospective shadow audit issued=%s settled=%s",
            issued, settled,
        )
        return issued, settled

    def recent_issued(self, *, asset: str, limit: int = 3) -> list[dict]:
        """Read actual immutable forecast rows; never synthesize predictions."""
        if asset not in ("BTC", "ETH", "SOL") or not 1 <= limit <= 8:
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
        if asset not in ("BTC", "ETH", "SOL") or horizon_minutes not in HORIZONS:
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

    def summary(self, *, asset: str, now: datetime | None = None) -> dict:
        if asset not in ("BTC", "ETH", "SOL"):
            raise ValueError("invalid asset")
        stamp = utc(now or datetime.now(UTC))
        start = (stamp - timedelta(days=7)).isoformat()
        prior = (stamp - timedelta(days=14)).isoformat()
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,horizon_minutes,issued_at,settled_at,
                          outcome_up,probability_up,baseline_up,model_brier,
                          baseline_brier
                   FROM perp_forward_audit
                   WHERE asset=? AND issued_at>=? AND issued_at<=?
                   ORDER BY issued_at""",
                (asset, prior, stamp.isoformat()),
            ).fetchall()
        def bucket(items) -> dict:
            settled_rows = [x for x in items if x["settled_at"] is not None]
            n = len(settled_rows)
            if not n:
                return {
                    "issued": len(items), "settled": 0, "hit_rate": None,
                    "brier": None, "baseline_brier": None,
                    "venues": 0, "model_beats_baseline": None,
                }
            brier = sum(float(r["model_brier"]) for r in settled_rows) / n
            base = sum(float(r["baseline_brier"]) for r in settled_rows) / n
            accuracy = sum(
                int((float(r["probability_up"]) > 0.5) == bool(r["outcome_up"]))
                for r in settled_rows if float(r["probability_up"]) != 0.5
            )
            decided = sum(float(r["probability_up"]) != 0.5 for r in settled_rows)
            return {
                "issued": len(items), "settled": n,
                "venues": len({r["venue"] for r in settled_rows}),
                "hit_rate": accuracy / decided if decided else None,
                "brier": brier, "baseline_brier": base,
                "model_beats_baseline": brier < base if n >= 50 else None,
            }
        recent = [r for r in rows if r["issued_at"] >= start]
        earlier = [r for r in rows if prior <= r["issued_at"] < start]
        by_horizon = {
            horizon: bucket([r for r in recent if r["horizon_minutes"] == horizon])
            for horizon in HORIZONS
        }
        return {
            "asset": asset, "as_of": stamp, "model": MODEL,
            "last_7d": by_horizon, "prior_7d": {
                horizon: bucket([r for r in earlier if r["horizon_minutes"] == horizon])
                for horizon in HORIZONS
            },
            "threshold": 50, "independent_trials": False,
        }
