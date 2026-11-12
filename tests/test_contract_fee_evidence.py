"""Exact-fee source receipts: source match, scheduled changes and no false fills."""
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.contract_fee_evidence import (
    kalshi_taker_paper_fee,
    polymarket_taker_paper_cost,
    resolve_kalshi_fee,
)

NOW = datetime(2026, 11, 12, 20, tzinfo=UTC)
END = NOW + timedelta(minutes=15)


def official(*, event_override=None, series_change=None, event_change=None):
    return resolve_kalshi_fee(
        series={"ticker": "KXBTC15M", "fee_type": "quadratic",
                "fee_multiplier": 1.25},
        event={"series_ticker": "KXBTC15M", "event_ticker": "KXBTC15M-EVENT",
               "fee_type_override": (event_override[0] if event_override else None),
               "fee_multiplier_override": (event_override[1] if event_override else None)},
        series_changes={"series_fee_change_arr": (
            [series_change] if series_change else []), "cursor": ""},
        event_changes={"event_fee_changes": (
            [event_change] if event_change else []), "cursor": ""},
        series_ticker="KXBTC15M", event_ticker="KXBTC15M-EVENT",
        received_at=NOW, expires_at=END,
    )


def test_series_fee_and_event_override_precedence_are_explicit():
    base = official()
    assert base["status"] == "fee_metadata_verified"
    assert base["fee_type"] == "quadratic"
    assert base["fee_multiplier"] == 1.25
    assert base["source"]["level"] == "series"
    assert len(base["receipt_sha256"]) == 64
    assert not base["after_cost_qualified"]
    changed = official(event_override=("quadratic_with_maker_fees", 0.5))
    assert changed["status"] == "fee_metadata_verified"
    assert changed["source"]["level"] == "event_override"
    assert changed["fee_multiplier"] == 0.5
    assert changed["fee_type"] == "quadratic_with_maker_fees"
    assert changed["receipt_sha256"] != base["receipt_sha256"]


def test_scheduled_fee_changes_or_pagination_fail_closed():
    series_change = {"series_ticker": "KXBTC15M", "scheduled_ts":
                     (NOW + timedelta(minutes=3)).isoformat(),
                     "fee_type": "flat", "fee_multiplier": 1.0}
    assert official(series_change=series_change)["status"] == "fee_change_during_contract"
    event_change = {"event_ticker": "KXBTC15M-EVENT", "scheduled_ts":
                    (NOW + timedelta(minutes=10)).isoformat(),
                    "fee_type_override": "flat", "fee_multiplier_override": 0.5}
    assert official(event_change=event_change)["status"] == "fee_change_during_contract"
    assert official(series_change={
        **series_change, "scheduled_ts": (END + timedelta(hours=1)).isoformat()
    })["status"] == "fee_metadata_verified"
    base = official()
    assert base["fee_verified"]
    assert not base["fills_verified"]


def test_invalid_fee_source_and_unsupported_flat_never_prove_after_cost():
    assert official(event_override=("flat", 1.0))["status"] == (
        "metadata_verified_fee_formula_unknown")
    assert official(event_override=("quadratic", -1))["status"] == (
        "invalid_event_override")
    assert kalshi_taker_paper_fee(
        price=.5, quantity=10, fee_type="flat", multiplier=1)["status"] == (
        "unsupported_specific_fee_table")
    result = kalshi_taker_paper_fee(
        price=.5, quantity=10, fee_type="quadratic", multiplier=1)
    assert result["estimated_model_fee_usd"] == 0.175
    assert result["estimated_per_contract_fee_usd"] == 0.0175
    assert result["verified_after_cost"] is False


def test_polymarket_exact_side_token_and_freshness_paper_cost():
    args = dict(
        side="UP", token_id="12345678901234567890123456789012",
        quote_price=.4, quote_depth=12, shares=10, fee_rate=.07,
        quote_received_at=NOW-timedelta(seconds=3),
        fee_received_at=NOW-timedelta(seconds=5), issue_at=NOW,
    )
    result = polymarket_taker_paper_cost(**args)
    assert result["status"] == "same_token_paper_cost_estimate"
    assert result["estimated_cost_per_share"] == pytest.approx(.4168)
    assert result["verified_after_cost"] is False
    assert polymarket_taker_paper_cost(
        **{**args, "quote_depth": 8})["status"] == "insufficient_ask_depth"
    assert polymarket_taker_paper_cost(
        **{**args, "fee_received_at": NOW-timedelta(minutes=1)}
    )["status"] == "stale_fee_or_quote"
    assert polymarket_taker_paper_cost(
        **{**args, "token_id": "wrong"})["status"] == "wrong_token_or_side"
