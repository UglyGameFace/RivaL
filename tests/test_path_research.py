"""Regression proof for verified, non-promoted first-passage research."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.free_history import _persist
from parlay_bot.research.path_research import (
    HORIZONS,
    LOOKBACK,
    _samples,
    evaluate_directory,
    evaluate_path,
    trace_barriers,
)
from parlay_bot.research.learning import assemble


def _bar(*, high: float, low: float) -> dict:
    return {"high": high, "low": low}


def test_barrier_trace_distinguishes_tp1_tp2_stop_and_unresolved_same_bar():
    entry, sigma = 100.0, 0.01
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="long",
                          future=[_bar(high=100.1, low=99.5)]) == (0, 0)
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="long",
                          future=[_bar(high=103, low=100)]) == (1, 1)
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="long",
                          future=[_bar(high=102, low=99.5),
                                  _bar(high=100, low=98.5)]) == (1, 0)
    # Same five-minute bar: we cannot tell whether profit or stop hit first.
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="long",
                          future=[_bar(high=103, low=98)]) is None
    # First TP was certain, but TP2 versus the stop is still unresolved.
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="long",
                          future=[_bar(high=102, low=100),
                                  _bar(high=103, low=98)]) is None
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="short",
                          future=[_bar(high=100, low=97)]) == (1, 1)
    assert trace_barriers(entry=entry, sigma_horizon=sigma, side="short",
                          future=[_bar(high=102, low=97)]) is None


@pytest.mark.parametrize("side,entry,sigma,high,low", [
    ("bad", 100., .01, 100., 99.),
    ("long", 0., .01, 100., 99.),
    ("long", 100., 0., 100., 99.),
    ("long", 100., float("nan"), 100., 99.),
    ("long", 100., .01, float("inf"), 99.),
    ("short", 100., .01, 10., -2.),
])
def test_invalid_or_untrustworthy_paths_fail_closed(side, entry, sigma, high, low):
    with pytest.raises(ValueError):
        trace_barriers(entry=entry, sigma_horizon=sigma, side=side,
                       future=[_bar(high=high, low=low)])


def _history(n: int = 8200) -> list[dict]:
    # Synthetic fixtures, never production evidence. Reproducible momentum,
    # varying volatility, and OHLC bars that do not embed unrealized fills.
    start = datetime(2026, 4, 1, tzinfo=UTC)
    rows = []
    previous = 100.0
    for i in range(n):
        close = 100 * math.exp(
            0.004 * math.sin(i * .19) + 0.003 * math.sin(i * .031)
            + 0.0003 * math.sin(i * .007)
        )
        opened = start + timedelta(minutes=5*i)
        rows.append({
            "opened_at": opened.isoformat(),
            "closed_at": (opened + timedelta(minutes=5, milliseconds=-1)).isoformat(),
            "open": previous, "high": max(previous, close)*1.000025,
            "low": min(previous, close)*0.999975, "close": close,
            "volume_base": 5.0 + (i % 11), "trades": 10,
        })
        previous = close
    return rows


def _manifest(tmp_path, rows: list[dict]):
    item = _persist(
        source="binance_um_klines", key="BTCUSDT-5m-2026-04",
        records=rows, raw_sha="f"*64,
        url="https://data.binance.vision/example-fixture-only.zip",
        out_dir=tmp_path,
        extra={
            "venue": "binance", "contract": "BTCUSDT", "quote": "USDT",
            "interval_minutes": 5,
            "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
        },
    )
    return item.manifest_path


def test_research_connects_to_existing_verified_catalog_without_promotion(tmp_path):
    manifest = _manifest(tmp_path, _history())
    prepared = assemble([manifest])
    for horizon in HORIZONS:
        for side in ("long", "short"):
            result = evaluate_path([manifest], horizon=horizon, side=side,
                                   prepared=prepared)
            assert result["model_spec"]["horizon_minutes"] == horizon
            assert result["model_spec"]["side"] == side
            assert result["sample_counts"]["train"] >= 120
            assert result["sample_counts"]["test"] >= 50
            assert result["model_spec"]["train_count"] == result["sample_counts"]["train"]
            assert result["retrospective_only"]
            assert not result["prospective_probability_proven"]
            assert not result["live_model_promoted"]
            assert not result["executable_costs_or_fills_verified"]
            assert not result["research_candidate_passed"]  # one synthetic month only
            assert result["model_sha256"] == evaluate_path(
                [manifest], horizon=horizon, side=side,
                prepared=prepared,
            )["model_sha256"]
    catalog = evaluate_directory(
        tmp_path, qualified_streams={("BTCUSDT", 5)},
    )
    assert len(catalog["evaluations"]) == 4
    assert catalog["rejected"] == []
    assert not catalog["live_model_promoted"]


def test_missing_candle_never_crosses_gap_or_creates_a_future_path():
    rows = _history(400)
    original, report = _samples(rows, horizon=15, side="long")
    assert original and not report["gaps"]
    # Remove a bar in the first part of an otherwise well-formed series.
    del rows[LOOKBACK + 7]
    modified, gaps = _samples(rows, horizon=15, side="long")
    assert gaps["gaps"] > 0
    assert len(modified) < len(original)


def test_only_past_sets_barriers_and_training_segments(tmp_path):
    rows = _history()
    original = evaluate_path([_manifest(tmp_path, rows)], horizon=15, side="long")
    # Build an alternate final-test future: training events are unchanged,
    # but the last 12 bars' known outcomes can differ.
    revised = _history()
    for i in range(len(revised)-12, len(revised)):
        revised[i]["high"] *= 1.2
    root = tmp_path / "different"
    second = evaluate_path([_manifest(root, revised)], horizon=15, side="long")
    assert original["model_spec"]["baseline"] == second["model_spec"]["baseline"]
    assert original["model_spec"]["cohort"] == second["model_spec"]["cohort"]
    assert original["model_spec"]["training_outcomes_through"] == (
        second["model_spec"]["training_outcomes_through"]
    )
