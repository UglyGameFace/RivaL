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
                 incumbent_up,baseline_up,len(eligible),fingerprint),
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
        """Day-blocked, same-target, same-market prospective comparisons.

        Different venues observing the same BTC target are correlated and
        count as ONE event; none of these scores qualifies a trade or promotion.
        """
        stamp = utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT family,target_opened_at,model_brier,incumbent_brier,
                          baseline_brier,outcome_up
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND horizon_minutes=? AND issued_at>=?
                    AND issued_at<=? AND settled_at IS NOT NULL
                    AND settled_at<=?
                   ORDER BY family,target_opened_at""",
                (asset,horizon,(stamp-timedelta(days=14)).isoformat(),
                 stamp.isoformat(),stamp.isoformat()),
            ).fetchall()
        results = {}
        for family in FAMILIES:
            grouped = {}
            for row in rows:
                if row["family"] != family or row["outcome_up"] not in (0,1):
                    continue
                target = str(row["target_opened_at"])
                if int(datetime.fromisoformat(target).timestamp()) % (horizon*60):
                    continue
                if any(row[key] is None or not math.isfinite(row[key])
                       or not 0 <= row[key] <= 1
                       for key in ("model_brier","incumbent_brier","baseline_brier")):
                    continue
                grouped.setdefault(target, []).append(row)
            scored = [
                (target,
                 sum(r["model_brier"] for r in group)/len(group),
                 sum(r["incumbent_brier"] for r in group)/len(group),
                 sum(r["baseline_brier"] for r in group)/len(group))
                for target, group in sorted(grouped.items())
                if len({r["outcome_up"] for r in group}) == 1
            ]
            incumbent = score_paired_forward([(t,m,i) for t,m,i,_ in scored])
            base = score_paired_forward([(t,m,b) for t,m,_,b in scored])
            coin = score_paired_forward([(t,m,.25) for t,m,_,_ in scored])
            results[family] = {
                "targets": incumbent.targets, "days": incumbent.days,
                "brier": incumbent.model_brier,
                "incumbent_brier": incumbent.baseline_brier,
                "baseline_brier": base.baseline_brier,
                "qualified_research": all(
                    result.status == "qualified_shadow"
                    for result in (incumbent,base,coin)
                ),
                "reason": incumbent.reason if incumbent.status != "qualified_shadow"
                          else base.reason if base.status != "qualified_shadow"
                          else coin.reason,
                "live_approved": False,
            }
        return results
