"""Leakage-controlled, retrospective multi-horizon futures research.

Historical OHLCV has no authenticated first-observed-at and cannot prove
the strategy's actual prior forecast skill, fills or profitability. This module
only writes candidate evidence; it never promotes a live model or places orders.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from parlay_bot.research.free_history import INTERVALS, SCHEMA, HistoryImportError

LOOKBACK = 12
HORIZON_MINUTES = (5, 15, 60, 240, 1440, 10080)
MIN_TRAIN = 300
MIN_HOLDOUT = 100
EPS = 1e-8


def _sigmoid(v: float) -> float:
    v = min(30.0, max(-30.0, v))
    return 1 / (1 + math.exp(-v))


def _metrics(scored: list[tuple[float, int, str]], baseline: float) -> dict:
    n = len(scored)
    if not n:
        return {"count": 0, "days": 0, "brier": None, "baseline_brier": None}
    brier = sum((p - y) ** 2 for p, y, _ in scored) / n
    baseline_brier = sum((baseline - y) ** 2 for _, y, _ in scored) / n
    logloss = -sum(
        y * math.log(max(EPS, p)) + (1 - y) * math.log(max(EPS, 1 - p))
        for p, y, _ in scored
    ) / n
    by_day: dict[str, list[float]] = defaultdict(list)
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for p, y, day in scored:
        by_day[day].append((baseline - y) ** 2 - (p - y) ** 2)
        bins[min(9, int(10 * p))].append((p, y))
    daily_gain = [sum(v) / len(v) for v in by_day.values()]
    ece = sum(
        len(b) / n * abs(sum(p for p, _ in b) / len(b)
                         - sum(y for _, y in b) / len(b))
        for b in bins if b
    )
    return {
        "count": n, "days": len(by_day),
        "brier": round(brier, 8),
        "baseline_brier": round(baseline_brier, 8),
        "brier_gain": round(baseline_brier - brier, 8),
        "log_loss": round(logloss, 8),
        "accuracy": round(sum((p > 0.5) == bool(y) for p, y, _ in scored) / n, 6),
        "ece_10": round(ece, 6),
        "daily_positive_skill_fraction": round(
            sum(g > 0 for g in daily_gain) / len(daily_gain), 6
        ),
    }


def _rows(manifest_path: Path) -> tuple[dict, list[dict]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema_version") != SCHEMA
            or manifest.get("source") not in {"binance_um_klines", "verified_venue_perp_klines"}
            or manifest.get("checksum_verified") is not True
            or manifest.get("prospective_forecast") is not False
            or manifest.get("time_reference") != "exchange_candle_open_not_first_observed_at"
            or (manifest.get("source") == "verified_venue_perp_klines"
                and not (
                    manifest.get("source_integrity") == "postgres-first-observed-at-v1"
                    or (
                        manifest.get("source_integrity")
                            == "official-bitget-rest-observed-at-v1"
                        and manifest.get("venue") == "bitget"
                        and manifest.get("source_url")
                            == "https://api.bitget.com/api/v3/market/history-candles"
                        and manifest.get("source_api_category") == "USDT-FUTURES"
                        and manifest.get("source_api_interval") == "5m"
                        and manifest.get("source_api_type") == "market"
                        and manifest.get("provider_signed_checksum") is False
                        and manifest.get("historical_first_observed_at_is_fetch_time") is True
                    )
                ))
        ):
            raise HistoryImportError("ineligible historical source")
        name = manifest.get("file")
        if not isinstance(name, str) or Path(name).name != name:
            raise HistoryImportError("invalid research path")
        payload = (manifest_path.parent / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != manifest.get("data_sha256"):
            raise HistoryImportError("historical record checksum mismatch")
        rows = [json.loads(line) for line in payload.splitlines() if line]
        if len(rows) != manifest.get("records") or not rows:
            raise HistoryImportError("historical row count mismatch")
        if manifest["source"] == "verified_venue_perp_klines":
            for row in rows:
                if (not isinstance(row, dict)
                    or row.get("venue") != manifest.get("venue")
                    or row.get("contract") != manifest.get("contract")
                    or row.get("quote") != manifest.get("quote")
                    or row.get("interval_minutes") != 5
                    or _as_datetime(row["first_observed_at"]) <
                       _as_datetime(row["closed_at"])):
                    raise HistoryImportError("unverified source-native historical record")
        return manifest, rows
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HistoryImportError("unreadable or invalid history manifest") from exc


def _as_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise HistoryImportError("timezone absent in history")
    return result


def assemble(manifests: list[Path]) -> tuple[dict, list[dict], dict]:
    """Use one venue, instrument and base interval, sorted by immutable time."""
    if not manifests:
        raise HistoryImportError("at least one historical monthly manifest required")
    identity = None
    months: set[str] = set()
    rows: list[dict] = []
    sources: dict[str, str] = {}
    for path in manifests:
        meta, records = _rows(path)
        key = (meta.get("venue"), meta.get("contract"),
               meta.get("quote"), meta.get("interval_minutes"))
        if meta["source"] == "binance_um_klines":
            if key[0] != "binance" or key[2] != "USDT":
                raise HistoryImportError("unexpected Binance instrument identity")
        elif (key[0] not in {
            "hyperliquid", "dydx", "bitget", "gate", "okx",
            "kucoin", "deribit", "lighter", "phemex",
        } or key[2] not in {"USDT", "USDC", "USD"}):
            raise HistoryImportError("unexpected native research venue or quote")
        if key[3] not in INTERVALS.values():
            raise HistoryImportError("unexpected instrument interval")
        if identity is not None and key != identity:
            raise HistoryImportError("do not mix instruments, venues or timeframes")
        identity = key
        if (meta["source"] == "verified_venue_perp_klines"
            and (meta.get("venue"), meta.get("contract"),
                 meta.get("quote"), meta.get("interval_minutes")) != key):
            raise HistoryImportError("source-native manifest identity mismatch")
        first_month = records[0]["opened_at"][:7]
        if first_month in months:
            raise HistoryImportError("duplicate monthly dataset")
        months.add(first_month)
        sources[first_month] = meta["data_sha256"]
        rows.extend(records)
    rows.sort(key=lambda r: r["opened_at"])
    interval = int(identity[3])
    step = timedelta(minutes=interval)
    prev = None
    gaps = 0
    for row in rows:
        opened = _as_datetime(row["opened_at"])
        closed = _as_datetime(row["closed_at"])
        if prev is not None:
            if opened <= prev or (opened - prev) % step:
                raise HistoryImportError("duplicate, non-monotonic or off-grid history")
            gaps += int((opened - prev) / step) - 1
        if closed != opened + step - timedelta(milliseconds=1):
            raise HistoryImportError("invalid closed historical interval")
        if any(
            not math.isfinite(float(row[k])) or float(row[k]) <= 0
            for k in ("open", "high", "low", "close")
        ):
            raise HistoryImportError("invalid historical price")
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(
            row["open"], row["close"]
        ):
            raise HistoryImportError("invalid high-low envelope")
        prev = opened
    return {
        "venue": identity[0], "contract": identity[1],
        "quote": identity[2], "interval_minutes": interval,
    }, rows, {"months": sorted(months), "source_sha256_by_month": sources,
              "gaps_between_rows": gaps}


def _examples(rows: list[dict], interval_minutes: int, horizon_minutes: int) -> list:
    if horizon_minutes % interval_minutes:
        return []
    step = timedelta(minutes=interval_minutes)
    target_bars = horizon_minutes // interval_minutes
    # Nonoverlapping target windows avoid treating correlated returns as
    # thousands of independent forward outcomes for long horizons.
    examples = []
    for i in range(LOOKBACK, len(rows) - target_bars, target_bars):
        history = rows[i - LOOKBACK:i + 1]
        target = rows[i + target_bars]
        stamps = [_as_datetime(c["opened_at"]) for c in history]
        if any(b - a != step for a, b in itertools.pairwise(stamps)):
            continue
        if _as_datetime(target["opened_at"]) - stamps[-1] != (
            timedelta(minutes=horizon_minutes)
        ):
            continue
        closes = [float(c["close"]) for c in history]
        prices = [math.log(closes[j] / closes[j - 1]) for j in range(1, len(closes))]
        mean = sum(prices) / len(prices)
        vol = math.sqrt(sum((x - mean) ** 2 for x in prices) / len(prices))
        volume_now = float(history[-1]["volume_base"])
        volume_mean = sum(float(c["volume_base"]) for c in history[:-1]) / LOOKBACK
        current = history[-1]
        vector = (
            max(-4.0, min(4.0, prices[-1] * 100)),
            max(-4.0, min(4.0, math.log(closes[-1] / closes[0]) * 100)),
            min(4.0, vol * 100),
            min(4.0, (float(current["high"]) - float(current["low"]))
                / float(current["close"]) * 100),
            max(-4.0, min(4.0, math.log((volume_now + 1) / (volume_mean + 1)))),
        )
        if float(target["close"]) == closes[-1]:
            continue
        examples.append((
            stamps[-1],
            _as_datetime(target["closed_at"]),
            vector,
            int(float(target["close"]) > closes[-1]),
        ))
    return examples


def _score(samples: list, weights: list[float]) -> list[tuple[float, int, str]]:
    return [
        (_sigmoid(weights[0] + sum(w * v for w, v in zip(weights[1:], x))),
         y, due.date().isoformat())
        for _, due, x, y in samples
    ]


def evaluate(
    manifests: list[Path], *, horizon_minutes: int, minimum_coverage: float = 0.995,
) -> dict:
    """Fully isolated chronological research, no automatic live-model promotion."""
    identity, candles, provenance = assemble(manifests)
    base = identity["interval_minutes"]
    if horizon_minutes not in HORIZON_MINUTES or horizon_minutes % base:
        raise HistoryImportError("horizon incompatible with historical bar interval")
    if not 0.90 <= minimum_coverage <= 1:
        raise ValueError("invalid coverage threshold")
    # Include the first and last expected slot across declared adjacent months.
    months = provenance["months"]
    if len(months) < 2:
        raise HistoryImportError("insufficient monthly partitions")
    first = _as_datetime(candles[0]["opened_at"])
    last = _as_datetime(candles[-1]["opened_at"])
    expected = int((last - first) / timedelta(minutes=base)) + 1
    coverage = len(candles) / expected
    if coverage < minimum_coverage:
        raise HistoryImportError("historical source coverage too low")
    examples = _examples(candles, base, horizon_minutes)
    if len(examples) < MIN_TRAIN + 2 * MIN_HOLDOUT:
        raise HistoryImportError("insufficient nonoverlapping training and holdout targets")
    # Purge the boundary using outcome release timestamps, not just issue time.
    first_holdout = int(len(examples) * 0.60)
    second_holdout = int(len(examples) * 0.80)
    cutoff_val = examples[first_holdout][0]
    cutoff_test = examples[second_holdout][0]
    train = [v for v in examples[:first_holdout] if v[1] < cutoff_val]
    validation = [v for v in examples[first_holdout:second_holdout] if v[1] < cutoff_test]
    test = examples[second_holdout:]
    if min(len(train), len(validation), len(test)) < MIN_HOLDOUT:
        raise HistoryImportError("insufficient causally separated holdout targets")
    if len(train) < MIN_TRAIN:
        raise HistoryImportError("insufficient train history after purge")
    # Balanced Laplace-smoothed training-only base rate. No future-label
    # baseline and no reuse of validation/test outcomes in fitting.
    baseline = (sum(v[3] for v in train) + 5) / (len(train) + 10)
    weights = [0.0] * (len(train[0][2]) + 1)
    weights[0] = math.log(baseline / (1 - baseline))
    # Fixed hyperparameters, epochs and sample ordering; validation/test are
    # never used for early stopping, feature selection or hyperparameter tuning.
    for _ in range(8):
        for _, _, x, y in train:
            p = _sigmoid(weights[0] + sum(w * v for w, v in zip(weights[1:], x)))
            delta = (y - p) * 0.0125
            weights[0] += delta
            for k, value in enumerate(x, start=1):
                weights[k] += delta * value - 0.000001 * weights[k]
    valid_metrics = _metrics(_score(validation, weights), baseline)
    test_metrics = _metrics(_score(test, weights), baseline)
    qualifying = (
        valid_metrics["count"] >= MIN_HOLDOUT
        and test_metrics["count"] >= MIN_HOLDOUT
        and valid_metrics["days"] >= 7 and test_metrics["days"] >= 7
        and valid_metrics["brier_gain"] > 0.005
        and test_metrics["brier_gain"] > 0.005
        and test_metrics["daily_positive_skill_fraction"] >= 0.60
    )
    model_spec = {
        "model": "offline-logistic-multi-horizon-v1",
        "identity": identity, "horizon_minutes": horizon_minutes,
        "train_cutoff": cutoff_val.isoformat(),
        "validation_cutoff": cutoff_test.isoformat(),
        "weights": [round(w, 12) for w in weights],
        "train_base_rate": round(baseline, 12),
        "source_sha256_by_month": provenance["source_sha256_by_month"],
    }
    model_sha = hashlib.sha256(
        json.dumps(model_spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "rival-offline-evaluation-v1",
        "model_sha256": model_sha, "model_spec": model_spec,
        "source_months": months, "candles": len(candles),
        "coverage_ratio_between_first_last": round(coverage, 8),
        "missing_candles_between_first_last": expected - len(candles),
        "training_count": len(train),
        "validation": valid_metrics, "out_of_sample_test": test_metrics,
        "research_candidate_passed": qualifying,
        "live_model_promoted": False, "real_betting_profit_verified": False,
        "independent_live_forecast_accuracy": None,
        "warning": (
            "Retrospective immutable exchange OHLCV has no verified historical "
            "first-observed-at. Test skill is NOT live prospective accuracy, "
            "account fills, calibrated trading probability or net betting profit."
        ),
    }


def evaluate_directory(
    root: Path, *, intervals: tuple[int, ...] = (5, 60, 1440),
    qualified_streams: set[tuple[str, int]] | None = None,
) -> dict:
    groups: dict[tuple[str, int], list[Path]] = defaultdict(list)
    for path in (root / "binance_um_klines").glob("*.manifest.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("interval_minutes") in intervals:
            groups[(meta["contract"], meta["interval_minutes"])].append(path)
    reports = []
    rejected = []
    for key, paths in sorted(groups.items()):
        if qualified_streams is not None and key not in qualified_streams:
            rejected.append({"contract": key[0], "interval_minutes": key[1],
                             "reason": "source failed full-month quality gate"})
            continue
        for h in HORIZON_MINUTES:
            if h % key[1]:
                continue
            try:
                result = evaluate(paths, horizon_minutes=h)
                reports.append(result)
            except (HistoryImportError, ValueError, OSError) as exc:
                rejected.append({"contract": key[0], "interval_minutes": key[1],
                                 "horizon_minutes": h, "reason": str(exc)})
    return {
        "schema_version": "rival-multi-horizon-research-v1",
        "evaluations": reports, "rejected": rejected,
        "live_accuracy_changed": False, "automatic_betting_enabled": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline RivaL research, no live promotion")
    parser.add_argument("--root", type=Path, default=Path("data/research-history"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = evaluate_directory(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"evaluated": len(report["evaluations"]),
                      "rejected": len(report["rejected"]), "report": str(args.out)}))
    return 0 if report["evaluations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
