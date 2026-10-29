"""Independent bookmaker-pricing baselines for already settled historical football.

These are descriptive, date-only retrospective checks, not point-in-time
predictions, learned model accuracy, executable bets or trading authorizations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from parlay_bot.research.free_history import SCHEMA, HistoryImportError

RESULTS = ("H", "D", "A")


def fair_probabilities(prices: list[float]) -> tuple[tuple[float, ...], float]:
    if len(prices) != 3 or any(
        isinstance(p, bool) or not isinstance(p, (int, float))
        or not math.isfinite(p) or p <= 1 or p > 1000
        for p in prices
    ):
        raise HistoryImportError("valid three-way decimal odds required")
    inverse = [1 / x for x in prices]
    overround = sum(inverse)
    return tuple(x / overround for x in inverse), overround - 1


def _load_verified(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != SCHEMA
        or manifest.get("source") != "football_data"
        or manifest.get("type") != "offline_historical_research_only"
        or manifest.get("prospective_forecast") is not False
    ):
        raise HistoryImportError("only verified offline football manifests are supported")
    name = manifest.get("file", "")
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".ndjson"):
        raise HistoryImportError("invalid manifest data path")
    payload = (manifest_path.parent / name).read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest.get("data_sha256"):
        raise HistoryImportError("research data changed after manifest creation")
    rows = [json.loads(line) for line in payload.splitlines() if line]
    if not rows or len(rows) != manifest.get("records"):
        raise HistoryImportError("research manifest row count mismatch")
    return rows


def bookmaker_benchmark(rows: list[dict[str, Any]], *, bookmaker: str = "B365") -> dict:
    if not rows:
        raise HistoryImportError("no football outcomes to score")
    ordered = sorted(rows, key=lambda row: (row["date"], row["home"], row["away"]))
    samples: list[tuple[str, float, float, float, float | None, float | None]] = []
    for item in ordered:
        result = item.get("result")
        if result not in RESULTS or item.get("odds_snapshot_time_verified") is not False:
            raise HistoryImportError("not a verified date-only settled fixture")
        odds = item.get("books", {}).get(bookmaker, {})
        if not odds or not odds.get("opening"):
            continue
        p, margin = fair_probabilities(odds["opening"])
        label = RESULTS.index(result)
        brier = sum((p[n] - (n == label)) ** 2 for n in range(3))
        log_loss = -math.log(p[label])
        closing = odds.get("closing")
        closing_brier = closing_margin = None
        if closing:
            q, closing_margin = fair_probabilities(closing)
            closing_brier = sum((q[n] - (n == label)) ** 2 for n in range(3))
        samples.append((item["date"], brier, log_loss, margin,
                        closing_brier, closing_margin))
    if not samples:
        raise HistoryImportError("no valid priced outcomes for selected bookmaker")
    # Neither closing prices nor settled results feed a trained model here.
    # Late-period statistics are a separate descriptive slice, not proof of
    # production walk-forward forecasting or positive expected return.
    split = max(1, int(len(samples) * 0.70))
    later = samples[split:]
    def stats(items: list[tuple]) -> dict | None:
        if not items:
            return None
        closing = [x[4] for x in items if x[4] is not None]
        return {
            "matches": len(items),
            "date_from": items[0][0], "date_to": items[-1][0],
            "opening_brier": round(sum(x[1] for x in items) / len(items), 6),
            "opening_log_loss": round(sum(x[2] for x in items) / len(items), 6),
            "mean_book_margin": round(sum(x[3] for x in items) / len(items), 6),
            "closing_matches": len(closing),
            "closing_brier": round(sum(closing) / len(closing), 6) if closing else None,
        }
    return {
        "kind": "retrospective_market_pricing_baseline",
        "bookmaker": bookmaker, "three_way_uniform_brier": round(2 / 3, 6),
        "all": stats(samples), "later_30_percent": stats(later),
        "independent_prospective_forecasts": 0,
        "model_trained": False, "actual_betting_profit_verified": False,
        "snapshot_time_verified": False,
        "status": "descriptive_only" if len(later) >= 100 else "thin_descriptive_sample",
        "warning": "Date-only historical prices cannot prove a bettable pre-match edge.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historical bookmaker baseline, no trading")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--book", default="B365", choices=("B365", "PS", "WH", "BW", "BF"))
    args = parser.parse_args(argv)
    try:
        result = bookmaker_benchmark(_load_verified(args.manifest), bookmaker=args.book)
    except (HistoryImportError, ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"Bookmaker audit rejected: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
