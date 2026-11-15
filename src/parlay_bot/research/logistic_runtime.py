"""Scalar serving math for fitted offline logistic artifacts.

Training keeps its NumPy implementation. Live serving needs only the frozen
standardizer, weights and calibration, including the same missing-value and
clipping rules. No scientific package is required on the bot host.
"""
from __future__ import annotations

import math


def calibrated_probability(doc: dict, values, *, serve_clamp: tuple[float, float]) -> float:
    """Evaluate a validated artifact with the training transform unchanged."""
    mean, std = doc["standardizer"]["mean"], doc["standardizer"]["std"]
    weights = doc["weights"]
    if not len(values) == len(mean) == len(std) == len(weights) - 1:
        raise ValueError("offline logistic input/standardizer width mismatch")
    standardized = []
    for value, center, scale in zip(values, mean, std, strict=True):
        scale = float(scale)
        z = (float(value) - float(center)) / scale if scale else 0.0
        standardized.append(max(-5.0, min(5.0, z)) if math.isfinite(z) else 0.0)
    logit = float(weights[0]) + sum(
        float(weight) * value for weight, value in zip(weights[1:], standardized, strict=True))

    def sigmoid(value):
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))

    raw = max(1e-6, min(1 - 1e-6, sigmoid(logit)))
    calibration = doc["calibration"]
    calibrated = sigmoid(float(calibration["a"]) * math.log(raw / (1 - raw))
                         + float(calibration["b"]))
    return min(serve_clamp[1], max(serve_clamp[0], calibrated))
