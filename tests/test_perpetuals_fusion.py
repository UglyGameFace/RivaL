"""Point-in-time source fusion and delayed-feedback regression coverage."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.fusion import (
    SourceForecast,
    SourceSkill,
    fuse_forecasts,
)
from parlay_bot.perpetuals.ledger import PredictionLedger

NOW = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self.raw.__exit__(*args)
        finally:
            self.raw.close()

    def execute(self, query, params=()):
        return self.raw.execute(query, tuple(params))

    def executescript(self, script):
        self.raw.executescript(script)


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        raw = sqlite3.connect(self.path)
        raw.row_factory = sqlite3.Row
        return Connection(raw)


@pytest.fixture
def ledger(tmp_path):
    result = PredictionLedger(Store(tmp_path / "learning.sqlite"))
    result.initialize()
    return result


def source(name, chance, *, origin=None, quality=1.0, when=NOW,
           asset="BTC", quote="USDC", horizon=5, kind="linear_perpetual"):
    return SourceForecast(
        source=name, origin=origin or name, asset=asset, quote=quote,
        horizon_minutes=horizon, observed_at=when, probability_up=chance,
        quality=quality, reference_price=100, contract_kind=kind,
    )


def combine(ledger, *observations):
    return fuse_forecasts(observations, issued_at=NOW, skill_for=ledger.skill_for)


def test_fusion_excludes_stale_low_quality_duplicate_and_future_sources(ledger):
    assert combine(
        ledger, source("a", .9), source("a-proxy", .9, origin="a"), source("b", .7)
    ) is None
    assert combine(
        ledger, source("a", .9), source("b", .8),
        source("c", .8, when=NOW - timedelta(hours=1))
    ) is None
    assert combine(
        ledger, source("a", .9), source("b", .8),
        source("c", .8, when=NOW + timedelta(seconds=1))
    ) is None
    assert combine(
        ledger, source("a", .9), source("b", .8), source("c", .8, quality=.1)
    ) is None


@pytest.mark.parametrize("changed", [
    {"asset": "ETH"}, {"quote": "USDT"}, {"horizon": 60},
    {"kind": "inverse_perpetual"},
])
def test_fusion_rejects_different_instrument_or_horizon(ledger, changed):
    with pytest.raises(ValueError, match="mismatched"):
        combine(ledger, source("a", .8), source("b", .7, **changed), source("c", .6))


def test_weight_cap_and_disagreement_shrink(ledger):
    result = combine(
        ledger, source("a", .99), source("b", .2, quality=.2),
        source("c", .1, quality=.2),
    )
    assert result is not None
    assert result.reference_source == "a"
    assert sum(result.weights) == pytest.approx(1)
    assert max(result.weights) <= .4000000001
    raw_mean = sum(x.probability_up * weight for x, weight in zip(
        result.contributors, result.weights
    ))
    assert abs(result.probability_up - .5) < abs(raw_mean - .5)
    assert result.agreement < 1


def test_immutable_predictions_settle_once_after_horizon(ledger):
    prediction = combine(
        ledger, source("a", .8), source("b", .7), source("c", .6)
    )
    assert prediction is not None
    assert ledger.record(prediction)
    assert not ledger.record(prediction)
    with pytest.raises(ValueError, match="due time"):
        ledger.settle(prediction.id, reference_source=prediction.reference_source,
                      observed_at=NOW + timedelta(minutes=4), settlement_price=110)
    with pytest.raises(ValueError, match="reference venue"):
        ledger.settle(prediction.id, reference_source="other",
                      observed_at=prediction.due_at, settlement_price=110)
    with pytest.raises(ValueError, match="due time"):
        ledger.settle(prediction.id, reference_source=prediction.reference_source,
                      observed_at=prediction.due_at + timedelta(minutes=3),
                      settlement_price=110)
    with pytest.raises(ValueError, match="flat price"):
        ledger.settle(prediction.id, reference_source=prediction.reference_source,
                      observed_at=prediction.due_at, settlement_price=100)
    assert ledger.settle(
        prediction.id, reference_source=prediction.reference_source,
        observed_at=prediction.due_at, settlement_price=110,
    )
    assert not ledger.settle(
        prediction.id, reference_source=prediction.reference_source,
        observed_at=prediction.due_at, settlement_price=110,
    )
    assert PredictionLedger(ledger.store).skill_for("BTC", "USDC", "a", 5).count == 1
    perf = ledger.performance(asset="BTC", quote="USDC", horizon_minutes=5)
    assert perf.count == 1
    assert perf.directional_accuracy == 1
    assert perf.brier == pytest.approx((prediction.probability_up - 1) ** 2)


def test_learning_decreases_weight_of_source_that_was_wrong(ledger):
    items = [source("a", .9), source("b", .5), source("c", .5)]
    before = combine(ledger, *items)
    assert before is not None
    assert ledger.record(before)
    assert ledger.skill_for("BTC", "USDC", "a", 5).count == 0
    ledger.settle(
        before.id, reference_source=before.reference_source,
        observed_at=before.due_at, settlement_price=90,
    )
    after = combine(ledger, *items)
    assert after is not None
    assert after.weights[0] < before.weights[0]
    assert ledger.skill_for("BTC", "USDC", "a", 60) == SourceSkill()
    assert ledger.skill_for("ETH", "USDC", "a", 5) == SourceSkill()


def test_bad_values_and_untrusted_timestamps_rejected():
    with pytest.raises(ValueError):
        source("a", float("nan"))
    with pytest.raises(ValueError):
        source("a", 1.5)
    with pytest.raises(ValueError):
        source("a", .8, when=datetime(2026, 10, 27))
    with pytest.raises(ValueError):
        SourceSkill(count=1, brier_sum=-1).reliability()
