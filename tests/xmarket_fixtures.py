from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from parlay_bot.research import archive_features, xmarket_training
from parlay_bot.research.xmarket_features import (
    FEATURE_CONTRACT,
    MODEL_VERSION,
    PRICE_SEMANTICS,
    SCHEMA,
    feature_digest,
    feature_names,
)

NOW = datetime(2026, 11, 12, 8, 0, 30, tzinfo=UTC)


def point(source, at, price=.55, *, origin="live", asset="BTC"):
    opens = datetime.fromtimestamp((int(at.timestamp()) - 1) // 900 * 900, UTC)
    condition = "0x" + hashlib.sha256(f"{asset}|{opens}".encode()).hexdigest()
    return {"source": source, "asset": asset,
            "contract_id": condition if source == "polymarket" else "KX" + asset + "15M-" + opens.strftime("%y%m%d%H%M"),
            **({"token_id": "1" * 40, "slug": f"{asset.lower()}-updown-15m-{int(opens.timestamp())}"}
               if source == "polymarket" else {}),
            "at": at.isoformat(), "opens_at": opens.isoformat(),
            "closes_at": (opens + timedelta(minutes=15)).isoformat(),
            "observed_at": (at + timedelta(seconds=1)).isoformat(),
            "price": price, "price_semantics": PRICE_SEMANTICS[source],
            "provider_sha256": ("a" if source == "polymarket" else "c") * 64,
            "normalized_sha256": "b" * 64, "fidelity_minutes": 1, "origin": origin,
            "yes_bid": price - .01, "yes_ask": price + .01}


def frames_and_inputs(n=24000, *, rho=.5, venues=("bitget", "okx")):
    rng = np.random.default_rng(7)
    r = np.zeros(n)
    eps = rng.normal(0, .002, n)
    for i in range(1, n):
        r[i] = rho * r[i - 1] + eps[i]
    index = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    frames = {}
    for j, venue in enumerate(venues):
        c = 100 * np.exp(np.cumsum(r * (1 + j * .01)))
        k = pd.DataFrame({"open": c, "high": c * 1.002, "low": c * .998,
                          "close": c, "volume": 10., "trades": 3}, index=index)
        frames[venue + "|BTC"] = k, archive_features.frame_features(k, None, "BTC")
    samples = []
    for at, ret in zip(index, r):
        p = float(.5 + .15 * np.tanh(ret * 100))
        samples.extend((point("polymarket", at, p), point("kalshi", at, p + .01)))
    inputs = {"samples": samples, "live_venues": list(venues),
              "verification": {s: {"status": "verified", "paired_points": 100,
                  "mismatch_rate": 0., "overlap_coverage": 1.}
                  for s in PRICE_SEMANTICS}}
    return frames, inputs


def valid_doc(*, venues=("bitget", "okx"), asset="BTC", horizon=5, now=NOW):
    names = feature_names(venues)
    weights = [0.01 * ((i % 7) - 3) for i in range(len(names) + 1)]
    return {"schema": SCHEMA, "family": "xmarket_v1", "model_version": MODEL_VERSION,
            "asset": asset, "horizon_minutes": horizon, "input_count": len(names),
            "feature_contract": FEATURE_CONTRACT, "features": list(names),
            "feature_digest": feature_digest(venues), "dispersion_venues": list(venues),
            "price_semantics": PRICE_SEMANTICS, "native_horizon_minutes": 15, "pm_fidelity_minutes": 1,
            "cross_horizon_input": horizon != 15, "weights": weights,
            "weights_sha256": hashlib.sha256(json.dumps(weights, separators=(",", ":")).encode()).hexdigest(),
            "standardizer": {"mean": [0.] * len(names), "std": [1.] * len(names)},
            "calibration": {"method": "platt", "status": "evaluated", "a": 1., "b": 0.,
                            "ece": .02, "fit_rows": 200, "eval_rows": 100},
            "trained_from": (now - timedelta(days=90)).isoformat(),
            "trained_through": (now - timedelta(days=1)).isoformat(),
            "training_rows": 1000, "gates_passed": True, "gates": xmarket_training.GATES,
            "data_checks": {s: {"status": "verified", "paired_points": 100,
                "mismatch_rate": 0., "overlap_coverage": 1.} for s in PRICE_SEMANTICS},
            "evaluation_status": "held-out evaluated",
            "evaluation": {"validation": {"brier_gain": .01},
                "test": {"brier_gain": .01, "daily_positive_fraction": .8, "ece": .02}},
            "provenance": {"provider_sha256_by_month": {"bitget:2026-01": "a" * 64},
                "normalized_sha256_by_month": {"bitget:2026-01": "b" * 64},
                "months_without_provider_hash": [], "supplemental_provider_sha256": ["a" * 64, "c" * 64]}}
