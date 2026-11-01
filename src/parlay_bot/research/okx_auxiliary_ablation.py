"""Paired historical OKX feature ablation, never live model registration.

A candidate feature must beat the existing venue-native 10-feature baseline on
the SAME purged chronological outcomes. This is retrospective research, not
the required prospective 21-day after-cost evidence.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from parlay_bot.perpetuals.learning import _sigmoid
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _as_datetime, _metrics, assemble
from parlay_bot.research.live_training import compatible_partitions, historical_features

AUX_FEATURES = {
    "trades": ("log_trade_count", "aggressor_size_imbalance"),
    "funding_rates": ("latest_funding_rate",),
    "orderbook_l2_400": ("spread_bps", "top5_size_imbalance"),
    "orderbook_l2_5000": ("spread_bps", "top5_size_imbalance"),
}
MAX_AGE = {
    "trades": timedelta(minutes=5),
    "funding_rates": timedelta(hours=9),
    "orderbook_l2_400": timedelta(minutes=5),
    "orderbook_l2_5000": timedelta(minutes=5),
}


def _load_one(receipt_path: Path, *, venue: str, contract: str) -> tuple[str, list]:
    """Verify local source bytes, identities, event windows and causal sidecar."""
    if venue != "okx":
        raise HistoryImportError("auxiliary research must remain on original OKX venue")
    info = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (info.get("schema_version") != "okx-auxiliary-research-v1"
        or info.get("contract") != contract
        or info.get("venue") != venue or info.get("module") not in AUX_FEATURES
        or info.get("prospective_forecast") is not False
        or info.get("live_model_import_allowed") is not False
        or info.get("after_cost_edge_verified") is not False
        or info.get("source_provider_checksum_verified") is not False):
        raise HistoryImportError("unqualified OKX historical feature receipt")
    module = info["module"]
    name = receipt_path.name.removesuffix(".receipt.json")
    if name == receipt_path.name or not name.startswith(contract):
        raise HistoryImportError("invalid OKX auxiliary receipt filename")
    root = receipt_path.parent
    raw = root / name
    sidecar = root / (name + ".research.ndjson")
    if (hashlib.sha256(raw.read_bytes()).hexdigest() != info.get("archive_sha256")
        or hashlib.sha256(sidecar.read_bytes()).hexdigest() != info.get("feature_sha256")):
        raise HistoryImportError("OKX raw/derived checksum mismatch")
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()
            if line]
    if not rows or len(rows) != info.get("feature_buckets"):
        raise HistoryImportError("missing or altered OKX derived feature rows")
    month = info.get("month")
    if not isinstance(month, str) or len(month) != 7:
        raise HistoryImportError("invalid archive month")
    records = []
    previous = None
    allowed = {
        "trades": {"trades", "buy_size_source_units", "sell_size_source_units"},
        "funding_rates": {"funding_rate"},
        "orderbook_l2_400": {"l2_snapshot_count", "l2_linked_update_count",
                             "last_snapshot_ts_ms", "last_snapshot_spread_bps",
                             "l2_top5_imbalance"},
        "orderbook_l2_5000": {"l2_snapshot_count", "l2_linked_update_count",
                              "last_snapshot_ts_ms", "last_snapshot_spread_bps",
                              "l2_top5_imbalance"},
    }[module]
    for row in rows:
        if (not isinstance(row, dict) or row.get("venue") != venue
            or row.get("contract") != contract
            or not isinstance(row.get("features"), dict)
            or not set(row["features"]) <= allowed):
            raise HistoryImportError("cross-venue or unknown OKX derived feature")
        opened = _as_datetime(row["event_bucket_open"])
        usable = _as_datetime(row["available_after"])
        if (opened.strftime("%Y-%m") != month or usable != opened + timedelta(minutes=5)
            or opened.minute % 5 != 0 or opened.second != 0
            or previous is not None and usable <= previous):
            raise HistoryImportError("off-window, repeated or premature feature")
        previous = usable
        features = row["features"]
        if module == "trades":
            count = float(features.get("trades", 0))
            buy = float(features.get("buy_size_source_units", 0))
            sell = float(features.get("sell_size_source_units", 0))
            if min(count, buy, sell) < 0 or count <= 0 or buy + sell <= 0:
                raise HistoryImportError("bad OKX trade aggregation")
            vector = (min(4.0, math.log1p(count) / 4),
                      (buy - sell) / (buy + sell))
        elif module == "funding_rates":
            rate = float(features["funding_rate"])
            if abs(rate) > 1:
                raise HistoryImportError("invalid OKX funding rate")
            vector = (max(-4.0, min(4.0, rate * 1000)),)
        else:
            spread = float(features["last_snapshot_spread_bps"])
            imbalance = float(features["l2_top5_imbalance"])
            if spread < 0 or not -1 <= imbalance <= 1:
                raise HistoryImportError("invalid archived orderbook summary")
            vector = (min(4.0, spread / 20), imbalance)
        if not all(math.isfinite(v) for v in vector):
            raise HistoryImportError("nonfinite historical auxiliary feature")
        records.append((usable, vector))
    return module, records


def load_sidecars(paths: list[Path], *, venue: str, contract: str) -> tuple[str, list]:
    if not paths:
        raise HistoryImportError("no verified OKX auxiliary archives")
    module = None
    events: dict[datetime, tuple] = {}
    for path in paths:
        kind, rows = _load_one(path, venue=venue, contract=contract)
        if module is not None and kind != module:
            raise HistoryImportError("compare exactly one auxiliary module at a time")
        module = kind
        for time, vector in rows:
            if time in events:
                raise HistoryImportError("duplicate auxiliary time across source archives")
            events[time] = vector
    return module, sorted(events.items())


def _asof(samples, series, *, module: str) -> list:
    timestamps = [x[0] for x in series]
    paired = []
    for issue, due, live_features, outcome in samples:
        index = bisect.bisect_right(timestamps, issue) - 1
        if index < 0 or issue - timestamps[index] > MAX_AGE[module]:
            continue
        paired.append((issue, due, live_features, series[index][1], outcome))
    return paired


def _fit(samples, *, with_aux: bool) -> tuple[list[float], float]:
    if len(samples) < 300:
        raise HistoryImportError("too few paired historical training observations")
    base = (sum(s[4] for s in samples) + 5) / (len(samples) + 10)
    dimension = len(samples[0][2]) + (len(samples[0][3]) if with_aux else 0)
    weights = [math.log(base / (1 - base)), *([0.] * dimension)]
    for _ in range(4):
        for _, _, features, extra, outcome in samples:
            vector = (1., *features, *(extra if with_aux else ()))
            err = outcome - _sigmoid(sum(w * v for w, v in zip(weights, vector)))
            for j, v in enumerate(vector):
                weights[j] += .0075 * (err * v - (.003 * weights[j] if j else 0))
    return weights, base


def _scores(samples, weights, base, *, with_aux):
    scored = []
    for issue, _, features, extra, outcome in samples:
        vector = (1., *features, *(extra if with_aux else ()))
        prediction = _sigmoid(sum(w * v for w, v in zip(weights, vector)))
        scored.append((prediction, outcome, issue.date().isoformat()))
    return _metrics(scored, base)


def evaluate_ablation(manifests: list[Path], sidecars: list[Path], *, horizon: int) -> dict:
    """Only research evidence: paired same-target chronological comparisons."""
    identity, candles, provenance = assemble(manifests)
    if identity["venue"] != "okx" or identity["interval_minutes"] != 5:
        raise HistoryImportError("OKX source-native 5m historical evidence required")
    if len(provenance["months"]) < 6 or provenance["gaps_between_rows"] != 0:
        raise HistoryImportError("six contiguous source-native OHLCV months required")
    module, events = load_sidecars(sidecars, venue="okx", contract=identity["contract"])
    vectors = historical_features(candles, identity)
    train, validation, test = compatible_partitions(
        candles, vectors, horizon=horizon, identity=identity)
    whole = train + validation + test
    matched = _asof(whole, events, module=module)
    coverage = len(matched) / len(whole)
    if coverage < .80:
        raise HistoryImportError(
            f"auxiliary historical pairing coverage below 80%: {coverage:.3f}")
    train_matched = _asof(train, events, module=module)
    val_matched = _asof(validation, events, module=module)
    test_matched = _asof(test, events, module=module)
    if len(train_matched) < 300 or min(len(val_matched), len(test_matched)) < 100:
        raise HistoryImportError("not enough purged paired auxiliary holdouts")
    incumbent, base = _fit(train_matched, with_aux=False)
    expanded, expanded_base = _fit(train_matched, with_aux=True)
    scores = {
        "validation": {
            "incumbent": _scores(val_matched, incumbent, base, with_aux=False),
            "augmented": _scores(val_matched, expanded, expanded_base, with_aux=True),
        },
        "test": {
            "incumbent": _scores(test_matched, incumbent, base, with_aux=False),
            "augmented": _scores(test_matched, expanded, expanded_base, with_aux=True),
        },
    }
    for phase in scores.values():
        phase["paired_brier_gain"] = round(
            phase["incumbent"]["brier"] - phase["augmented"]["brier"], 8)
    return {
        "schema_version": "okx-auxiliary-ablation-v1",
        "identity": identity, "module": module,
        "feature_names": AUX_FEATURES[module],
        "horizon_minutes": horizon,
        "source_sha256_by_month": provenance["source_sha256_by_month"],
        "auxiliary_receipts_sha256": [
            hashlib.sha256(p.read_bytes()).hexdigest() for p in sidecars],
        "paired_coverage": round(coverage, 6),
        "training_count": len(train_matched),
        "validation_count": len(val_matched),
        "test_count": len(test_matched),
        "scores": scores,
        "retrospective_gain_supported": all(
            scores[s]["paired_brier_gain"] > .005
            for s in ("validation", "test")),
        "prospective_outcomes": 0,
        "after_cost_edge_verified": False,
        "live_model_import_allowed": False,
        "promoted_live": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKX retrospective auxiliary ablation")
    parser.add_argument("--manifest", action="append", required=True, type=Path)
    parser.add_argument("--aux-receipt", action="append", required=True, type=Path)
    parser.add_argument("--horizon", type=int, choices=(5, 15, 60), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = evaluate_ablation(
            args.manifest, args.aux_receipt, horizon=args.horizon)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except (HistoryImportError, OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"OKX auxiliary ablation UNVERIFIED: {exc}\n")
    print(json.dumps({
        "status": "retrospective_only", "paired_coverage": result["paired_coverage"],
        "brier_gain": result["scores"]["test"]["paired_brier_gain"],
        "promoted_live": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
