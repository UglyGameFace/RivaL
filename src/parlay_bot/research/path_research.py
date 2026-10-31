"""Chronological, conservative target-before-stop research on verified exchange bars.

This is an OFFLINE challenger to RivaL's existing live forecasts, not a
Polymarket/Kalshi settlement model. Five-minute OHLC cannot determine which
barrier traded first when both occur in the same bar: exclude those paths.
Never manufacture first-observed times, fills, costs, or prospective labels.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _as_datetime, _metrics, assemble

LOOKBACK = 24
HORIZONS = (15, 60)
SIDES = ("long", "short")
MIN_TRAIN = 120
MIN_HOLDOUT = 50
MIN_UTC_DAYS = 7
MIN_COHORT = 30
MIN_GAIN = 0.002


@dataclass(frozen=True)
class PathSample:
    issued_at: object
    expires_at: object
    aligned: bool
    tp1: int
    tp2: int


def trace_barriers(*, entry: float, sigma_horizon: float, side: str,
                   future: list[dict]) -> tuple[int, int] | None:
    """Return TP1/TP2 before stop, or None for uncertain first-touch order.

    Barriers are fixed *before* looking at the future: 1-sigma TP1, 2-sigma
    TP2, 1-sigma stop. We deliberately make no within-bar path assumption.
    TP1 may succeed even if the stop is touched in a subsequent bar.
    """
    if side not in SIDES or not math.isfinite(entry) or entry <= 0:
        raise ValueError("invalid future path identity or entry")
    if not math.isfinite(sigma_horizon) or not 0 < sigma_horizon <= 0.25:
        raise ValueError("invalid prior volatility")
    sign = 1 if side == "long" else -1
    tp1 = entry * math.exp(sign * sigma_horizon)
    tp2 = entry * math.exp(sign * 2 * sigma_horizon)
    stop = entry * math.exp(-sign * sigma_horizon)
    hit1 = False
    hit2 = False
    if not future:
        raise ValueError("no future bars")
    for bar in future:
        high, low = float(bar["high"]), float(bar["low"])
        if (not math.isfinite(high) or not math.isfinite(low)
                or low <= 0 or high < low):
            raise ValueError("invalid future high/low")
        stopped = low <= stop if sign == 1 else high >= stop
        touched1 = high >= tp1 if sign == 1 else low <= tp1
        touched2 = high >= tp2 if sign == 1 else low <= tp2
        # A stopped position cannot both stop and newly reach its next
        # target within one unresolved 5m bar. Drop, do not guess.
        if stopped and ((touched1 and not hit1) or (touched2 and not hit2)):
            return None
        if stopped:
            return int(hit1), int(hit2)
        hit1 = hit1 or touched1
        hit2 = hit2 or touched2
        if hit2:
            return 1, 1
    return int(hit1), int(hit2)


def _samples(rows: list[dict], *, horizon: int, side: str) -> tuple[list[PathSample], dict]:
    if horizon not in HORIZONS or side not in SIDES:
        raise ValueError("unsupported path research horizon or side")
    count = horizon // 5
    opened = [_as_datetime(r["opened_at"]) for r in rows]
    samples: list[PathSample] = []
    discarded = {"ambiguous_same_bar": 0, "gaps": 0, "invalid_past": 0}
    # Nonoverlapping future windows limit correlated target repetition.
    for i in range(LOOKBACK, len(rows) - count, count):
        window = rows[i - LOOKBACK:i + count + 1]
        dates = opened[i - LOOKBACK:i + count + 1]
        if any(b - a != timedelta(minutes=5) for a, b in itertools.pairwise(dates)):
            discarded["gaps"] += 1
            continue
        prior = [float(r["close"]) for r in window[:LOOKBACK + 1]]
        if any(not math.isfinite(x) or x <= 0 for x in prior):
            discarded["invalid_past"] += 1
            continue
        returns = [math.log(b/a) for a, b in itertools.pairwise(prior)]
        mean = sum(returns) / len(returns)
        sigma = math.sqrt(sum((x - mean)**2 for x in returns) / len(returns))
        scaled = sigma * math.sqrt(count)
        if not 0 < scaled <= 0.25:
            discarded["invalid_past"] += 1
            continue
        future = window[LOOKBACK + 1:]
        try:
            outcome = trace_barriers(entry=prior[-1], sigma_horizon=scaled,
                                     side=side, future=future)
        except (ValueError, TypeError, KeyError, OverflowError):
            discarded["invalid_past"] += 1
            continue
        if outcome is None:
            discarded["ambiguous_same_bar"] += 1
            continue
        momentum = math.log(prior[-1]/prior[-4])
        aligned = momentum > 0 if side == "long" else momentum < 0
        samples.append(PathSample(
            issued_at=_as_datetime(rows[i]["closed_at"]),
            expires_at=_as_datetime(rows[i + count]["closed_at"]),
            aligned=aligned, tp1=outcome[0], tp2=outcome[1],
        ))
    return samples, discarded


def _holdout_scores(samples: list[PathSample], *, cohort: dict,
                    baseline: dict) -> dict:
    result = {}
    for label in ("tp1", "tp2"):
        results = [
            (cohort[s.aligned][label], getattr(s, label),
             s.expires_at.date().isoformat()) for s in samples
        ]
        result[label] = _metrics(results, baseline[label])
    return result


def evaluate_path(manifests: list[Path], *, horizon: int, side: str,
                  prepared: tuple | None = None) -> dict:
    identity, rows, provenance = assemble(manifests) if prepared is None else prepared
    if identity["interval_minutes"] != 5 or horizon not in HORIZONS or side not in SIDES:
        raise HistoryImportError("path research requires one verified 5m futures stream")
    samples, discarded = _samples(rows, horizon=horizon, side=side)
    if len(samples) < MIN_TRAIN + 2 * MIN_HOLDOUT:
        raise HistoryImportError("too few unambiguous nonoverlapping futures paths")
    first_holdout = int(len(samples) * 0.6)
    second_holdout = int(len(samples) * 0.8)
    start_validation = samples[first_holdout].issued_at
    start_test = samples[second_holdout].issued_at
    train = [s for s in samples[:first_holdout] if s.expires_at < start_validation]
    validation = [s for s in samples[first_holdout:second_holdout]
                  if s.expires_at < start_test]
    test = samples[second_holdout:]
    if (len(train) < MIN_TRAIN or len(validation) < MIN_HOLDOUT
            or len(test) < MIN_HOLDOUT):
        raise HistoryImportError("insufficient purged first-passage holdouts")
    by_cohort = {
        aligned: [s for s in train if s.aligned == aligned]
        for aligned in (True, False)
    }
    if any(len(group) < MIN_COHORT for group in by_cohort.values()):
        raise HistoryImportError("insufficient causal momentum cohorts")
    # The only fitting data is training history. The validation and final
    # test periods never tune barriers, segment definitions, or parameters.
    base = {
        label: (sum(getattr(s, label) for s in train) + 2) / (len(train) + 4)
        for label in ("tp1", "tp2")
    }
    cohort = {
        aligned: {
            label: (sum(getattr(s, label) for s in group) + 20 * base[label])
                   / (len(group) + 20)
            for label in ("tp1", "tp2")
        }
        for aligned, group in by_cohort.items()
    }
    validation_scores = _holdout_scores(validation, cohort=cohort, baseline=base)
    test_scores = _holdout_scores(test, cohort=cohort, baseline=base)
    # Removing unknown first-hit orders is a form of outcome-dependent
    # selection. Report its frequency and refuse offline qualification when
    # the omitted fraction is material, rather than claiming universal skill.
    ambiguous = discarded["ambiguous_same_bar"]
    ambiguity_fraction = ambiguous / (ambiguous + len(samples))
    passed = (
        ambiguity_fraction <= 0.05
        and len(provenance["months"]) >= 6
        and all(
            item["brier_gain"] > MIN_GAIN
            and item["ece_10"] <= 0.12
            and item["days"] >= MIN_UTC_DAYS
            for period in (validation_scores, test_scores)
            for item in period.values()
        )
    )
    spec = {
        "model": "offline-first-passage-cohort-v1",
        "identity": identity, "horizon_minutes": horizon, "side": side,
        "barriers": {"tp1_sigma": 1, "tp2_sigma": 2, "stop_sigma": 1},
        "cohort": {"aligned": cohort[True], "not_aligned": cohort[False]},
        "baseline": base, "train_count": len(train),
        "training_outcomes_through": max(s.expires_at for s in train).isoformat(),
        "validation_outcomes_through": max(s.expires_at for s in validation).isoformat(),
        "test_outcomes_through": max(s.expires_at for s in test).isoformat(),
        "source_sha256_by_month": provenance["source_sha256_by_month"],
    }
    digest = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "rival-path-research-v1",
        "model_sha256": digest,
        "model_spec": spec,
        "validation": validation_scores,
        "out_of_sample_test": test_scores,
        "sample_counts": {"train": len(train), "validation": len(validation),
                          "test": len(test)},
        "rejections": discarded,
        "ambiguous_path_fraction": round(ambiguity_fraction, 6),
        "probabilities_conditioned_on_unambiguous_paths": True,
        "all_unresolved_same_bar_paths_excluded": True,
        "research_candidate_passed": passed,
        "retrospective_only": True,
        "prospective_probability_proven": False,
        "executable_costs_or_fills_verified": False,
        "live_model_promoted": False,
        "market_type": "exchange_futures_not_prediction_contracts",
    }


def evaluate_directory(root: Path, *, qualified_streams: set[tuple[str, int]]) -> dict:
    groups = defaultdict(list)
    for path in sorted((root / "binance_um_klines").glob("*.manifest.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        key = (meta.get("contract"), meta.get("interval_minutes"))
        if key[1] == 5 and key in qualified_streams:
            groups[key].append(path)
    results = []
    rejected = []
    for key, paths in sorted(groups.items()):
        try:
            prepared = assemble(paths)
        except (HistoryImportError, ValueError, OSError) as exc:
            rejected.append({"contract": key[0], "reason": str(exc)})
            continue
        for horizon in HORIZONS:
            for side in SIDES:
                try:
                    results.append(evaluate_path(paths, horizon=horizon,
                                                 side=side, prepared=prepared))
                except (HistoryImportError, ValueError, OSError) as exc:
                    rejected.append({
                        "contract": key[0], "horizon_minutes": horizon,
                        "side": side, "reason": str(exc),
                    })
    return {
        "schema_version": "rival-path-research-catalog-v1",
        "evaluations": results, "rejected": rejected,
        "exchange_futures_only": True, "market_contract_outcomes_verified": False,
        "live_model_promoted": False, "auto_trading_enabled": False,
    }
