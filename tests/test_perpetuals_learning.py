"""Point-in-time feature and outcome-gated online learning regression tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.features import LabeledExample, live_examples
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.learning import walk_forward

START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def candle(index: int, *, backfilled: bool = False, contract: str = "BTC-USD"):
    opened = START + timedelta(minutes=5 * index)
    closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
    return MarketCandle(
        venue="dydx", contract=contract, quote="USD", interval_minutes=5,
        opened_at=opened, closed_at=closed,
        observed_at=(
            START + timedelta(days=2) if backfilled
            else closed + timedelta(seconds=1)
        ),
        open_price=100 + index * 0.3, high_price=101 + index * 0.3,
        low_price=99 + index * 0.3, close_price=100.5 + index * 0.3,
        volume_base=10 + (index % 6), trades=20,
    )


def test_live_features_do_not_read_future_candles():
    candles = [candle(i) for i in range(50)]
    for horizon, count in ((5, 44), (15, 42), (60, 33)):
        samples = live_examples(candles, horizon_minutes=horizon)
        assert len(samples) == count
        assert all(x.label_known_at > x.issued_at for x in samples)
        assert all(len(x.features) == 5 for x in samples)
        assert all(x.venue == "dydx" and x.quote == "USD" for x in samples)
        assert all(x.label_up == 1 for x in samples)
    with pytest.raises(ValueError, match="horizon"):
        live_examples(candles, horizon_minutes=15 + 1)


def test_same_time_backfill_is_not_misrepresented_as_live_history():
    candles = [candle(i, backfilled=True) for i in range(50)]
    assert live_examples(candles, horizon_minutes=5) == ()


def test_feature_extraction_rejects_gaps_duplicates_and_mismatched_venues():
    original = [candle(i) for i in range(30)]
    with pytest.raises(ValueError, match="chronology"):
        live_examples([*original, original[-1]], horizon_minutes=5)
    modified = [*original]
    modified[12] = candle(12, contract="ETH-USD")
    with pytest.raises(ValueError, match="provenance"):
        live_examples(modified, horizon_minutes=5)
    with_gap = [item for idx, item in enumerate(original) if idx != 8]
    examples = live_examples(with_gap, horizon_minutes=5)
    assert len(examples) < 24
    assert all(x.label_known_at > x.issued_at for x in examples)


def example(index: int, *, delay_minutes: int = 10) -> LabeledExample:
    issued = START + timedelta(minutes=5 * index)
    value = 1 if index % 4 != 0 else 0
    # A benign deterministic synthetic relationship to exercise online updates,
    # not a claim about real-market predictability.
    return LabeledExample(
        venue="dydx", contract="BTC-USD", quote="USD",
        horizon_minutes=5, issued_at=issued,
        label_known_at=issued + timedelta(minutes=delay_minutes),
        features=((1.0 if value else -1.0), 0.3, 0.1, 0.2, 0.0),
        label_up=value,
    )


def test_walkforward_updates_only_from_resolved_outcomes():
    samples = [example(i, delay_minutes=120) for i in range(20)]
    result = walk_forward(
        samples, as_of=START + timedelta(minutes=90), warmup=4,
        minimum_evaluated=20,
    )
    assert result.updates == 0
    assert result.evaluated == 0
    assert result.final_weights == (0.0,) * 6
    later = walk_forward(
        samples, as_of=START + timedelta(hours=6), warmup=4,
        minimum_evaluated=20,
    )
    assert later.updates == 20
    assert later.evaluated == 16
    assert later.brier is not None
    assert not later.candidate_beats_baseline


def test_prequential_result_has_measured_brier_and_nonzero_learned_weights():
    samples = [example(i) for i in range(160)]
    result = walk_forward(
        samples, as_of=START + timedelta(days=1),
        warmup=20, minimum_evaluated=50,
    )
    assert result.updates == 160
    assert result.evaluated == 140
    assert result.brier is not None
    assert result.baseline_brier is not None
    assert 0 <= result.brier <= 1
    assert 0 <= result.baseline_brier <= 1
    assert any(abs(x) > 0 for x in result.final_weights)
    assert result.accuracy is not None
    assert result.brier < result.baseline_brier
    # This is a shadow-research metric, not an automatic model promotion.
    assert isinstance(result.candidate_beats_baseline, bool)


def test_walkforward_rejects_mismatched_or_leaking_labels():
    samples = [example(i) for i in range(6)]
    with pytest.raises(ValueError, match="non-causal"):
        walk_forward(
            [*samples, example(5)], as_of=START + timedelta(days=1),
        )
    broken = LabeledExample(
        venue="dydx", contract="BTC-USD", quote="USD", horizon_minutes=5,
        issued_at=START, label_known_at=START,
        features=(0.1, 0.2), label_up=1,
    )
    with pytest.raises(ValueError, match="non-causal"):
        walk_forward([broken], as_of=START + timedelta(days=1))


def test_batched_observations_create_only_one_live_example_per_timestamp():
    # Multiple previously closed candles can arrive together, but are one
    # real observation event, not several independent live predictions.
    candles = [candle(i) for i in range(70)]
    for index in range(10, 14):
        candles[index] = replace(
            candles[index], observed_at=candles[13].observed_at,
        )
    samples = live_examples(candles, horizon_minutes=5)
    issued = [sample.issued_at for sample in samples]
    assert len(issued) == len(set(issued))
    assert issued == sorted(issued)
    assert all(sample.issued_at < sample.label_known_at for sample in samples)
    result = walk_forward(
        samples, as_of=START + timedelta(days=1), minimum_evaluated=20,
    )
    assert result.updates > 0
