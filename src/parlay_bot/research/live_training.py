"""Historical model comparison using the exact live research feature contract.

Fits on training labels, selects by purged chronological validation, and opens
one final test only after selection. Exchange outcomes remain exchange outcomes.
No historical timestamp is inserted into a live observation or forecast table.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from parlay_bot.perpetuals.features import INDICATOR_BARS, feature_vector
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.learning import _sigmoid
from parlay_bot.perpetuals.markets import HORIZONS
from parlay_bot.perpetuals.model_checkpoints import FEATURE_CONTRACT, TARGET_CONTRACT, canonical
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _as_datetime, _metrics, assemble


def historical_features(rows, identity):
    """Extract identical vectors without claiming historical first observation."""
    candles = [MarketCandle(
        venue=identity["venue"], contract=identity["contract"], quote=identity["quote"],
        interval_minutes=5, opened_at=_as_datetime(r["opened_at"]),
        closed_at=_as_datetime(r["closed_at"]),
        observed_at=_as_datetime(r["closed_at"]) + timedelta(milliseconds=1),
        open_price=float(r["open"]), high_price=float(r["high"]),
        low_price=float(r["low"]), close_price=float(r["close"]),
        volume_base=float(r["volume_base"]), trades=int(r["trades"]),
    ) for r in rows]
    vectors = {}
    for index in range(INDICATOR_BARS-1, len(candles)):
        try:
            vectors[index] = feature_vector(candles[index-INDICATOR_BARS+1:index+1])
        except ValueError:
            continue  # Never bridge a missing source candle.
    return vectors


def _fit(samples, *, l2, rate=.0125, epochs=4, active=None):
    """Train exclusively on the past segment, using the live ten-feature contract.

    Candidate families differ in learning rate, regularization or feature
    subset, but always emit eleven weights usable by the existing live model.
    No validation or test label is used for gradient steps.
    """
    if not (0 <= l2 <= .1 and 0 < rate <= .05 and 1 <= epochs <= 6):
        raise ValueError("invalid historical candidate hyperparameters")
    enabled = frozenset(range(10) if active is None else active)
    if not enabled or not enabled <= set(range(10)):
        raise ValueError("invalid historical feature subset")
    base = (sum(s[3] for s in samples)+5)/(len(samples)+10)
    weights = [math.log(base/(1-base)), *([0.] * 10)]
    for _ in range(epochs):
        for _, _, features, y in samples:
            x = (1., *features)
            error = y-_sigmoid(sum(w*v for w, v in zip(weights, x)))
            for i, v in enumerate(x):
                if i and i-1 not in enabled:
                    continue
                weights[i] += rate * (error*v - (l2*weights[i] if i else 0))
    return weights, base


def _score(samples, weights, base):
    return _metrics([(_sigmoid(sum(w*v for w, v in zip(weights, (1., *s[2])))),
                      s[3], s[0].date().isoformat()) for s in samples], base)


def compatible_partitions(rows, vectors, *, horizon, identity):
    """The ONE live-compatible, source-specific, gap-safe time split.

    Model families share these purged partitions; the test's labels are not
    available to choose a family, calibrator or training hyperparameters.
    """
    if identity["interval_minutes"] != 5 or horizon not in HORIZONS:
        raise HistoryImportError("live-compatible research needs exact 5m source bars")
    step = horizon // 5
    examples = []
    for i in range(INDICATOR_BARS-1, len(rows)-step, step):
        if i not in vectors:
            continue
        opened = _as_datetime(rows[i]["opened_at"])
        target = rows[i+step]
        if _as_datetime(target["opened_at"])-opened != timedelta(minutes=horizon):
            continue
        examples.append((_as_datetime(rows[i]["closed_at"]),
                         _as_datetime(target["closed_at"]), vectors[i],
                         int(float(target["close"]) > float(rows[i]["close"]))))
    if len(examples) < 600:
        raise HistoryImportError("insufficient compatible training and holdouts")
    a, b = int(.6*len(examples)), int(.8*len(examples))
    validation_start, test_start = examples[a][0], examples[b][0]
    train = [s for s in examples[:a] if s[1] < validation_start]
    validation = [s for s in examples[a:b] if s[1] < test_start]
    test = examples[b:]
    if len(train) < 300 or min(len(validation), len(test)) < 100:
        raise HistoryImportError("insufficient purged compatible holdouts")
    return train, validation, test


def evaluate_compatible(manifests, *, horizon, prepared=None):
    identity, rows, provenance = assemble(manifests) if prepared is None else prepared[:3]
    vectors = historical_features(rows, identity) if prepared is None else prepared[3]
    train, validation, test = compatible_partitions(
        rows, vectors, horizon=horizon, identity=identity,
    )
    test_start = test[0][0]
    compared = []
    for l2 in (0., .001, .01):
        weights, base = _fit(train, l2=l2)
        compared.append((f"logistic-l2-{l2:g}", weights, base,
                         _score(validation, weights, base)))
    # Small prespecified family; all choices are made on the SAME purged
    # validation period. Never tune to the final test period.
    for method, options in (
        ("slow-conservative-logit", {"l2": .005, "rate": .006, "epochs": 3}),
        ("fast-regularized-logit", {"l2": .003, "rate": .018, "epochs": 4}),
        ("trend-only-logit", {"l2": .005, "rate": .01, "epochs": 4,
                              "active": (0, 1, 5, 6)}),
    ):
        weights, base = _fit(train, **options)
        compared.append((method, weights, base,
                         _score(validation, weights, base)))
    # Ensemble ONLY the full-feature L2 family. A sparse model should not
    # quietly overwrite its inactive feature weights through averaging.
    pooled = [sum(v[1][i] for v in compared[:3])/3 for i in range(11)]
    compared.append(("regularized-logit-ensemble", pooled, compared[0][2],
                     _score(validation, pooled, compared[0][2])))
    method, weights, base, validation_scores = min(compared, key=lambda v: v[3]["brier"])
    # The test has never been consulted to choose a method or its weights.
    test_scores = _score(test, weights, base)
    spec = {
        "model": "offline-live-contract-logistic-v3", "method": method,
        "identity": identity, "horizon_minutes": horizon,
        "feature_contract": FEATURE_CONTRACT, "target_contract": TARGET_CONTRACT,
        "weights": [round(w, 12) for w in weights], "train_base_rate": base,
        "training_count": len(train), "training_cutoff": max(s[1] for s in train).isoformat(),
        "validation_cutoff": test_start.isoformat(),
        "evaluation_completed_through": max(s[1] for s in test).isoformat(),
        "source_sha256_by_month": provenance["source_sha256_by_month"],
    }
    passed = (len(provenance["months"]) >= 6 and all(
        s["brier_gain"] > .005 and s["ece_10"] <= .10 and s["days"] >= 7
        for s in (validation_scores, test_scores)
    ))
    return {"schema_version": "rival-live-compatible-research-v3",
            "model_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
            "model_spec": spec, "validation": validation_scores,
            "out_of_sample_test": test_scores, "research_candidate_passed": passed,
            "compared_on_validation": [{"method": x[0], "scores": x[3]} for x in compared],
            "live_model_promoted": False, "prospective_accuracy": None}


def evaluate_directory(root: Path, *, qualified_streams):
    groups = defaultdict(list)
    for p in sorted((root/"binance_um_klines").glob("*.manifest.json")):
        # assemble performs all byte-checksum and chronology verification.
        meta = json.loads(p.read_text())
        key = (meta.get("contract"), meta.get("interval_minutes"))
        if key[1] == 5 and key in qualified_streams:
            groups[key].append(p)
    results = []
    for paths in groups.values():
        identity, rows, provenance = assemble(paths)
        vectors = historical_features(rows, identity)
        prepared = (identity, rows, provenance, vectors)
        for horizon in HORIZONS:
            try:
                results.append(evaluate_compatible(paths, horizon=horizon, prepared=prepared))
            except HistoryImportError:
                continue
    return results
