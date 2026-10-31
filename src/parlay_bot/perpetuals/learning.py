"""Small, reproducible online model evaluated strictly before feedback arrives.

This is prequential/shadow research on VERIFIED time-ordered live observations,
not an automatically approved trading signal or a deployable model checkpoint.
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.fusion import utc


@dataclass(frozen=True)
class WalkForwardResult:
    evaluated: int
    updates: int
    brier: float | None
    baseline_brier: float | None
    accuracy: float | None
    candidate_beats_baseline: bool
    final_weights: tuple[float, ...]
    # Evidence about weights used, not prospective performance scores.
    training_labels: int = 0
    historical_training_labels: int = 0
    historical_seed_sha256: str | None = None


def _sigmoid(value: float) -> float:
    clipped = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-clipped))


def walk_forward(
    samples: Sequence[LabeledExample],
    *,
    as_of: datetime,
    warmup: int = 20,
    minimum_evaluated: int = 50,
    learning_rate: float = 0.025,
    l2: float = 0.0001,
) -> WalkForwardResult:
    """Fit on resolved prior outcomes only, then score timestamped predictions.

    Labels are not visible at issuance or while unresolved. Shadow candidate
    evaluation is compared to a rolling Laplace-smoothed base-rate baseline.
    A favorable result never automatically promotes a model.
    """
    current = utc(as_of)
    if (
        warmup < 1 or minimum_evaluated < 20
        or not 0 < learning_rate <= 0.1 or not 0 <= l2 <= 0.01
    ):
        raise ValueError("invalid walk-forward settings")
    if not samples:
        return WalkForwardResult(0, 0, None, None, None, False, ())
    ordered = sorted(samples, key=lambda x: utc(x.issued_at))
    first = ordered[0]
    key = (first.venue, first.contract, first.quote, first.horizon_minutes)
    size = len(first.features)
    if size < 2:
        raise ValueError("feature vector too short")
    last_issued: datetime | None = None
    for sample in ordered:
        issued = utc(sample.issued_at)
        resolved = utc(sample.label_known_at)
        if (
            (sample.venue, sample.contract, sample.quote, sample.horizon_minutes) != key
            or len(sample.features) != size
            or sample.label_up not in (0, 1)
            or (last_issued is not None and issued <= last_issued)
            or issued >= resolved
            or any(not math.isfinite(x) or abs(x) > 5 for x in sample.features)
        ):
            raise ValueError("invalid or non-causal learning example")
        last_issued = issued

    weights = [0.0] * (size + 1)
    pending: list[tuple[datetime, int, LabeledExample]] = []
    resolved_count = 0
    resolved_positive = 0
    updates = 0
    scored: list[tuple[float, float, int]] = []

    def train(sample: LabeledExample) -> None:
        nonlocal resolved_count, resolved_positive, updates
        x = (1.0, *sample.features)
        prediction = _sigmoid(sum(w * z for w, z in zip(weights, x)))
        error = sample.label_up - prediction
        for i, feature in enumerate(x):
            penalty = 0 if i == 0 else l2 * weights[i]
            weights[i] += learning_rate * (error * feature - penalty)
        resolved_count += 1
        resolved_positive += sample.label_up
        updates += 1

    for index, sample in enumerate(ordered):
        issued = utc(sample.issued_at)
        if issued > current:
            continue
        # Strict '<' blocks using a target labeled at exactly issuance time.
        while pending and pending[0][0] < issued:
            known_at, _, matured = heapq.heappop(pending)
            if known_at <= current:
                train(matured)
        x = (1.0, *sample.features)
        probability = _sigmoid(sum(w * z for w, z in zip(weights, x)))
        baseline = (resolved_positive + 5) / (resolved_count + 10)
        if index >= warmup and utc(sample.label_known_at) <= current:
            scored.append((probability, baseline, sample.label_up))
        heapq.heappush(pending, (utc(sample.label_known_at), index, sample))
    # Final learning is permitted only for matured labels as of the evaluation clock;
    # it cannot retroactively change frozen predictions above.
    while pending and pending[0][0] <= current:
        _, _, matured = heapq.heappop(pending)
        train(matured)

    if not scored:
        return WalkForwardResult(0, updates, None, None, None, False, tuple(weights))
    count = len(scored)
    model_brier = sum((p - y) ** 2 for p, _, y in scored) / count
    baseline_brier = sum((b - y) ** 2 for _, b, y in scored) / count
    nonneutral = [(p, y) for p, _, y in scored if p != 0.5]
    accuracy = (
        sum(int((p > 0.5) == y) for p, y in nonneutral) / len(nonneutral)
        if nonneutral else None
    )
    return WalkForwardResult(
        evaluated=count, updates=updates, brier=model_brier,
        baseline_brier=baseline_brier, accuracy=accuracy,
        candidate_beats_baseline=(
            count >= minimum_evaluated and model_brier + 0.005 < baseline_brier
        ),
        final_weights=tuple(weights),
    )
