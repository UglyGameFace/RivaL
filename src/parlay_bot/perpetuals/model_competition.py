"""Causal, source-specific shadow competition beside the live RivaL learner.

Gaussian naive Bayes and distance-weighted kNN are established, inexpensive
alternatives to the incumbent logistic model. They train only on labels known
before issuance. Predictions are immutable and settled against the *same*
future market candle as the incumbent. Qualification is research-only:
it neither enables orders nor changes live signal/model promotion.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.research.boosted_portable import predict_portable
from parlay_bot.research.candidate_registry import ResearchCandidateRegistry

FAMILIES = ("gaussian_nb_v1", "knn21_v1")
MIN_TRAIN = 80
MAX_TRAIN = 480


def _probability(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("nonfinite model probability")
    return max(0.02, min(0.98, value))


def shadow_predictions(samples, features, *, as_of: datetime) -> dict[str, float]:
    """Fit two distinct deterministic families on ONLY matured live labels.

    No archive labels, unknown target outcomes, cross-venue samples, or future
    observations are permitted. Missing or invalid data yields no challenger.
    """
    instant = utc(as_of)
    if len(features) != 10 or any(not math.isfinite(x) or abs(x) > 5 for x in features):
        raise ValueError("invalid live feature contract")
    eligible = []
    identity = None
    for sample in samples:
        key = (sample.venue, sample.contract, sample.quote, sample.horizon_minutes)
        if identity is None:
            identity = key
        if key != identity:
            raise ValueError("shadow training cannot mix instruments")
        if (utc(sample.label_known_at) <= instant
                and utc(sample.issued_at) < utc(sample.label_known_at)):
            if (sample.label_up not in (0, 1) or len(sample.features) != 10
                    or any(not math.isfinite(v) or abs(v) > 5 for v in sample.features)):
                raise ValueError("invalid matured training observation")
            eligible.append(sample)
    eligible.sort(key=lambda row: (utc(row.label_known_at), utc(row.issued_at)))
    eligible = eligible[-MAX_TRAIN:]
    if len(eligible) < MIN_TRAIN:
        return {}
    up = sum(s.label_up for s in eligible)
    down = len(eligible) - up
    prior = (up + 5) / (len(eligible) + 10)
    # Diagonal Gaussian Naive Bayes with shrunk class variances and priors.
    by_class = [[s.features for s in eligible if s.label_up == cls] for cls in (0, 1)]
    if min(len(group) for group in by_class) < 10:
        return {}
    params = []
    for group in by_class:
        mean = [sum(row[j] for row in group) / len(group) for j in range(10)]
        var = [
            max(0.15, sum((row[j] - mean[j]) ** 2 for row in group) / len(group))
            for j in range(10)
        ]
        params.append((mean, var))
    log_odds = math.log((up + 5) / (down + 5))
    for j, value in enumerate(features):
        negative_mean, negative_var = params[0][0][j], params[0][1][j]
        positive_mean, positive_var = params[1][0][j], params[1][1][j]
        log_odds += (
            -0.5 * math.log(positive_var / negative_var)
            - (value - positive_mean) ** 2 / (2 * positive_var)
            + (value - negative_mean) ** 2 / (2 * negative_var)
        )
    nb = _probability(1 / (1 + math.exp(-max(-30., min(30., log_odds)))))
    # K-nearest neighbors; deterministic bounded training and feature scales.
    distances = (
        (sum((a - b) ** 2 for a, b in zip(features, s.features)), index, s.label_up)
        for index, s in enumerate(eligible)
    )
    neighbors = heapq.nsmallest(21, distances)
    weights = [1 / (0.25 + math.sqrt(d)) for d, _, _ in neighbors]
    # Pseudo-counts avoid claiming certainty from 21 near-identical candles.
    knn = _probability((sum(w * row[2] for w, row in zip(weights, neighbors))
                        + 5 * prior) / (sum(weights) + 5))
    return dict(zip(FAMILIES, (nb, knn), strict=True))


class ShadowModelCompetition:
    """Persistent immutable multi-model research without production promotion."""

    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS rival_model_shadow_forecasts (
                venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
                asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
                family TEXT NOT NULL, reference_opened_at TEXT NOT NULL,
                issued_at TEXT NOT NULL, target_opened_at TEXT NOT NULL,
                probability_up DOUBLE PRECISION NOT NULL,
                incumbent_up DOUBLE PRECISION NOT NULL,
                baseline_up DOUBLE PRECISION NOT NULL,
                training_count INTEGER NOT NULL, training_sha256 TEXT NOT NULL,
                settled_at TEXT, outcome_up INTEGER,
                model_brier DOUBLE PRECISION, incumbent_brier DOUBLE PRECISION,
                baseline_brier DOUBLE PRECISION,
                PRIMARY KEY (venue,contract,quote,horizon_minutes,family,reference_opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_rival_model_shadow_unsettled
                ON rival_model_shadow_forecasts (settled_at,target_opened_at);
            CREATE INDEX IF NOT EXISTS idx_rival_model_shadow_score
                ON rival_model_shadow_forecasts (asset,horizon_minutes,family,issued_at);
            """)

    def issue(self, conn, *, samples, features, now, venue, contract, quote,
              asset, horizon, reference_opened_at, target_opened_at, issued_at,
              incumbent_up, baseline_up):
        """Call only inside the incumbent forecast's *successful* transaction."""
        predictions = shadow_predictions(samples, features, as_of=now)
        # Exact-source historical boosters enter the SAME immutable future
        # comparison as existing live families, never the incumbent weights.
        boosters = {}
        for item in ResearchCandidateRegistry(self.store).compatible_boosters(
            venue=venue, contract=contract, quote=quote,
            horizon=horizon, now=utc(issued_at),
        ):
            family = f"boosted_{item['family']}_{item['model_sha256'][:12]}"
            boosters[family] = item
            predictions[family] = predict_portable(
                item["portable_model"], features, trusted=True,
            )
        if not predictions:
            return 0
        # Training provenance is persisted at issuance, never reconstructed
        # from current weights after the future price is known.
        eligible = sorted(
            (s for s in samples if utc(s.label_known_at) <= utc(now)),
            key=lambda s: (utc(s.label_known_at), utc(s.issued_at)),
        )[-MAX_TRAIN:]
        fingerprint = hashlib.sha256(json.dumps([
            (utc(s.issued_at).isoformat(), utc(s.label_known_at).isoformat(),
             s.label_up, s.features) for s in eligible
        ], separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if (utc(issued_at) >= utc(target_opened_at) + timedelta(minutes=5)
                or not (0 <= incumbent_up <= 1 and 0 <= baseline_up <= 1)):
            return 0
        saved = 0
        for family, probability in predictions.items():
            row = conn.execute(
                """INSERT INTO rival_model_shadow_forecasts
                    (venue,contract,quote,asset,horizon_minutes,family,
                     reference_opened_at,issued_at,target_opened_at,
                     probability_up,incumbent_up,baseline_up,training_count,
                     training_sha256)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (venue,contract,quote,asset,horizon,family,
                 utc(reference_opened_at).isoformat(),utc(issued_at).isoformat(),
                 utc(target_opened_at).isoformat(),probability,
                 incumbent_up,baseline_up,
                  (len(eligible) if family not in boosters else 0),
                  (fingerprint if family not in boosters
                   else boosters[family]["model_sha256"])),
            )
            saved += int(row.rowcount == 1)
        return saved

    def settle(self, *, now: datetime) -> int:
        """Only copy genuinely settled incumbent outcomes for the exact target.

        The incumbent settlement has already checked the original source
        candle's first-observation time and prohibited future/backfill labels.
        A shadow forecast may not exist or be scored without that proof.
        """
        self.initialize()
        stamp = utc(now).isoformat()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT s.*,f.outcome_up AS verified_outcome_up,
                          f.settled_at AS verified_settled_at
                   FROM rival_model_shadow_forecasts s
                   JOIN perp_forward_audit f
                    ON f.model_version='prospective-logistic-persistent-v3'
                    AND f.venue=s.venue AND f.contract=s.contract
                    AND f.quote=s.quote AND f.asset=s.asset
                    AND f.horizon_minutes=s.horizon_minutes
                    AND f.reference_opened_at=s.reference_opened_at
                    AND f.issued_at=s.issued_at
                   WHERE s.settled_at IS NULL AND f.settled_at IS NOT NULL
                    AND f.settled_at<=? AND f.outcome_up IN (0,1)
                    AND s.target_opened_at=f.target_opened_at
                   ORDER BY s.target_opened_at LIMIT 240""",
                (stamp,),
            ).fetchall()
            total = 0
            for row in rows:
                # Reject impossible pre-close settlements even when a legacy
                # row incorrectly marks its incumbent as settled.
                try:
                    end = utc(datetime.fromisoformat(str(row["target_opened_at"])))
                    issued = utc(datetime.fromisoformat(str(row["issued_at"])))
                    settled = utc(datetime.fromisoformat(str(row["verified_settled_at"])))
                    if not issued < end + timedelta(minutes=5) < settled <= utc(now):
                        continue
                except (TypeError, ValueError, OverflowError):
                    continue
                y = int(row["verified_outcome_up"])
                update = conn.execute(
                    """UPDATE rival_model_shadow_forecasts SET settled_at=?,
                        outcome_up=?,model_brier=?,incumbent_brier=?,baseline_brier=?
                       WHERE venue=? AND contract=? AND quote=?
                        AND horizon_minutes=? AND family=?
                        AND reference_opened_at=? AND settled_at IS NULL""",
                    (row["verified_settled_at"],y,
                     (row["probability_up"]-y)**2,
                     (row["incumbent_up"]-y)**2,
                     (row["baseline_up"]-y)**2,
                     row["venue"],row["contract"],row["quote"],
                     row["horizon_minutes"],row["family"],row["reference_opened_at"]),
                )
                total += int(update.rowcount == 1)
            return total

    def report(self, *, asset, horizon, now=None) -> dict:
        """Research comparisons per *actual* venue, not a pooled fake winner.

        Outcome groups from different venues can describe aggregate behavior,
        but never cross-subsidize the decision that one instrument's family
        has earned prospective research eligibility. Regime attributions are
        derived solely from immutable at-issuance tags.
        """
        stamp = utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT s.family,s.venue,s.contract,s.quote,s.issued_at,
                          s.target_opened_at,s.settled_at,s.outcome_up,
                          s.probability_up,s.model_brier,s.incumbent_brier,
                          s.baseline_brier,r.regime
                   FROM rival_model_shadow_forecasts s
                   LEFT JOIN perp_forward_regimes r
                    ON r.venue=s.venue AND r.contract=s.contract
                    AND r.quote=s.quote
                    AND r.horizon_minutes=s.horizon_minutes
                    AND r.reference_opened_at=s.reference_opened_at
                    AND r.issued_at=s.issued_at
                    AND r.model_version='prospective-logistic-persistent-v3'
                   WHERE s.asset=? AND s.horizon_minutes=? AND s.issued_at>=?
                    AND s.issued_at<=? AND s.settled_at IS NOT NULL
                    AND s.settled_at<=?
                   ORDER BY s.family,s.target_opened_at""",
                (asset,horizon,(stamp-timedelta(days=14)).isoformat(),
                 stamp.isoformat(),stamp.isoformat()),
            ).fetchall()
        results = {}
        all_families = list(FAMILIES) + sorted({
            str(row['family']) for row in rows
            if str(row['family']).startswith('boosted_')
        })
        for family in all_families:
            scoped = {}
            invalid = 0
            for row in rows:
                if row["family"] != family:
                    continue
                try:
                    target = utc(datetime.fromisoformat(str(row["target_opened_at"])))
                    issued = utc(datetime.fromisoformat(str(row["issued_at"])))
                    settled = utc(datetime.fromisoformat(str(row["settled_at"])))
                    scores = tuple(float(row[k]) for k in (
                        "probability_up", "model_brier",
                        "incumbent_brier", "baseline_brier",
                    ))
                    if (row["outcome_up"] not in (0, 1)
                            or not issued < target + timedelta(minutes=5) < settled <= stamp
                            or int(target.timestamp()) % (horizon*60)
                            or not all(math.isfinite(v) and 0 <= v <= 1 for v in scores)):
                        invalid += 1
                        continue
                except (ValueError, TypeError, OverflowError):
                    invalid += 1
                    continue
                key = (str(row["venue"]), str(row["contract"]), str(row["quote"]))
                # A target belongs to one instrument only. Multiple conflicting
                # rows in a scope invalidate that scope's target.
                scoped.setdefault(key, {}).setdefault(target.isoformat(), []).append(row)
            per_venue = {}
            regime_failures = {}
            for key, targets in sorted(scoped.items()):
                evaluated = []
                for target, group in sorted(targets.items()):
                    if len(group) != 1:
                        invalid += len(group)
                        continue
                    row = group[0]
                    m, i, b = (
                        float(row["model_brier"]), float(row["incumbent_brier"]),
                        float(row["baseline_brier"]),
                    )
                    evaluated.append((target,m,i,b))
                    regime = str(row["regime"] or "unknown")
                    point = regime_failures.setdefault(regime, {
                        "forecasts": 0, "wrong_direction": 0,
                        "high_confidence_misses": 0, "model_error_sum": 0.0,
                        "incumbent_error_sum": 0.0,
                    })
                    miss = int((float(row["probability_up"]) > .5) != bool(row["outcome_up"]))
                    point["forecasts"] += 1
                    point["wrong_direction"] += miss
                    point["high_confidence_misses"] += (
                        miss and abs(float(row["probability_up"]) - .5) >= .10
                    )
                    point["model_error_sum"] += m
                    point["incumbent_error_sum"] += i
                incumbent = score_paired_forward([(t,m,i) for t,m,i,_ in evaluated])
                base = score_paired_forward([(t,m,b) for t,m,_,b in evaluated])
                coin = score_paired_forward([(t,m,.25) for t,m,_,_ in evaluated])
                eligible = all(
                    x.status == "qualified_shadow" for x in (incumbent, base, coin)
                )
                per_venue["/".join(key)] = {
                    "targets": incumbent.targets, "days": incumbent.days,
                    "model_brier": incumbent.model_brier,
                    "incumbent_brier": incumbent.baseline_brier,
                    "base_rate_brier": base.baseline_brier,
                    "qualified_research": eligible,
                    "reason": "passed prospective research screen" if eligible
                              else incumbent.reason if incumbent.status != "qualified_shadow"
                              else base.reason if base.status != "qualified_shadow"
                              else coin.reason,
                }
            # Select a single independently proven source for the headline.
            # Conflicting BTC outcomes across venues must never be averaged
            # into a fake source-agnostic record.
            source_winners = [
                key for key, proof in per_venue.items() if proof["qualified_research"]
            ]
            best_source = (
                source_winners[0] if source_winners
                else max(per_venue, key=lambda key: per_venue[key]["targets"])
                if per_venue else None
            )
            headline = per_venue.get(best_source, {})
            regimes = {
                regime: {
                    "forecasts": d["forecasts"],
                    "wrong_direction": d["wrong_direction"],
                    "high_confidence_misses": d["high_confidence_misses"],
                    "model_brier": d["model_error_sum"] / d["forecasts"],
                    "incumbent_brier": d["incumbent_error_sum"] / d["forecasts"],
                }
                for regime,d in sorted(regime_failures.items())
            }
            results[family] = {
                "targets": headline.get("targets", 0),
                "days": headline.get("days", 0),
                "brier": headline.get("model_brier"),
                "incumbent_brier": headline.get("incumbent_brier"),
                "baseline_brier": headline.get("base_rate_brier"),
                "selected_source": best_source,
                "qualified_research": bool(source_winners),
                "qualified_sources": source_winners,
                "by_venue": per_venue,
                "by_regime": regimes,
                "invalid_rows": invalid,
                "reason": ("source-specific prospective screen passed"
                           if source_winners else
                           "No source-specific forward superiority proven"),
                "live_approved": False,
            }
        return results
