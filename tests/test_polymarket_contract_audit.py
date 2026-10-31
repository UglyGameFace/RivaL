"""Contract-aligned Polymarket proof rejects exchange-candle labels and hindsight."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.polymarket_watch import market_slug
from parlay_bot.perpetuals.polymarket_contract_audit import (
    ContractComparison,
    audit_contract_predictions,
)


def observations(*, total_days=22, per_day=9):
    results = []
    for day in range(total_days):
        for slot in range(per_day):
            opened = (
                datetime(2026, 9, 1, tzinfo=UTC)
                + timedelta(days=day, minutes=5*slot)
            )
            is_up = (slot % 3 != 0)
            results.append(ContractComparison(
                slug=market_slug("BTC", 5, opened),
                asset="BTC", horizon_minutes=5,
                opens=opened, closes=opened + timedelta(minutes=5),
                candidate_issued_at=opened - timedelta(seconds=60),
                baseline_issued_at=opened - timedelta(seconds=65),
                candidate_probability_up=0.80 if is_up else 0.20,
                baseline_probability_up=0.50,
                oracle_start_twap=78000.0,
                oracle_end_twap=78000.0 if is_up else 77999.0,
                oracle_recorded_at=opened+timedelta(minutes=5, seconds=15),
                oracle_provenance="chainlink-twap:btc-usd-twap-60s-streams",
                quote_observed_at=opened - timedelta(seconds=30),
                up_ask=0.45, down_ask=0.45,
                tie_direction="UP" if is_up else None,
                settlement_rules_sha256="a" * 64 if is_up else None,
            ))
    return results


def test_contract_evidence_requires_unique_21_day_verified_window_brier_and_cost():
    cohort = observations()
    report = audit_contract_predictions(cohort)
    assert report.status == "research_only"
    assert report.unique_windows == 198
    assert report.days == 22
    assert report.quoted_windows == 198
    assert report.quote_coverage == 1.0
    assert report.candidate_brier < report.baseline_brier
    assert report.eligible_trades == 198
    assert report.mean_after_cost_per_share > 0
    # Even passing research math must never grant live promotion.
    assert report.status != "promoted"


def test_chainlink_equal_price_resolves_up_only_with_exact_rule_evidence():
    row = observations(total_days=1, per_day=2)[1]
    assert row.oracle_start_twap == row.oracle_end_twap
    report = audit_contract_predictions([row])
    assert report.candidate_accuracy == 1.0
    assert report.status == "insufficient"


@pytest.mark.parametrize("update", [
    {"slug": "btc-updown-15m-123"},
    {"horizon_minutes": 15},
    {"asset": "SOL"},
    {"candidate_issued_at": datetime(2026, 9, 1, 0, 0, 1, tzinfo=UTC)},
    {"oracle_provenance": "binance-exchange-candle"},
    {"oracle_end_twap": -7.0},
    {"oracle_recorded_at": datetime(2026, 9, 1, 0, 5, 0, tzinfo=UTC)},
    {"quote_observed_at": datetime(2026, 9, 1, 0, 0, 1, tzinfo=UTC)},
    {"up_ask": 1.25},
    {"baseline_probability_up": 1.1},
])
def test_mismatched_hindsight_and_exchange_labels_blocked(update):
    row = replace(observations(total_days=1, per_day=1)[0], **update)
    assert audit_contract_predictions([row]).status == "blocked"


def test_duplicate_market_and_missing_quotes_rejected_or_insufficient():
    row = observations(total_days=1, per_day=1)[0]
    assert audit_contract_predictions([row, row]).status == "blocked"
    cohort = observations()
    half_without_quotes = [
        replace(row, quote_observed_at=None, up_ask=None, down_ask=None)
        if idx % 2 else row
        for idx, row in enumerate(cohort)
    ]
    report = audit_contract_predictions(half_without_quotes)
    assert report.status == "insufficient"
    assert report.quote_coverage == 0.5


def test_weak_challenger_is_rejected_against_matching_contract_baseline():
    rows = [
        replace(row, candidate_probability_up=0.50)
        for row in observations()
    ]
    report = audit_contract_predictions(rows)
    assert report.status == "rejected"
    assert "baseline" in report.reason


def test_strict_nonzero_positive_after_cost_required():
    rows = [
        replace(row, up_ask=0.999, down_ask=0.999)
        for row in observations()
    ]
    report = audit_contract_predictions(rows, cost_per_share=0.01)
    assert report.status == "rejected"
    assert report.mean_after_cost_per_share < 0


def test_never_approve_too_few_calendar_days_even_with_many_rows():
    rows = observations(total_days=4, per_day=50)
    report = audit_contract_predictions(rows)
    assert report.status == "insufficient"
    assert report.unique_windows == 200
    assert report.days == 4


def test_equal_price_without_explicit_market_rule_is_never_a_label():
    row = observations(total_days=1, per_day=2)[1]
    assert row.oracle_start_twap == row.oracle_end_twap
    report = audit_contract_predictions([
        replace(row, tie_direction=None, settlement_rules_sha256=None),
    ])
    assert report.status == "blocked"
    assert report.candidate_brier is None


def test_contract_specific_down_tie_rule_changes_both_score_and_profit():
    row = observations(total_days=1, per_day=2)[1]
    down = replace(
        row, tie_direction="DOWN", settlement_rules_sha256="b" * 64,
        candidate_probability_up=.20, baseline_probability_up=.5,
    )
    report = audit_contract_predictions([down])
    assert report.status == "insufficient"
    assert report.candidate_accuracy == 1.0
    assert report.candidate_brier == pytest.approx(.04)
    assert report.mean_after_cost_per_share == pytest.approx(.54)
    # An unsupported "tie=UP" assumption would reverse this evidence.
    assert audit_contract_predictions([
        replace(down, settlement_rules_sha256=None),
    ]).status == "blocked"


@pytest.mark.parametrize("tie_direction,rule_hash", [
    ("UP", None),
    ("UP", "not-a-sha"),
    ("UNKNOWN", "d" * 64),
    ("DOWN", "g" * 64),
])
def test_rule_attestation_must_be_explicit_and_well_formed(
    tie_direction, rule_hash,
):
    row = observations(total_days=1, per_day=2)[1]
    report = audit_contract_predictions([
        replace(row, tie_direction=tie_direction, settlement_rules_sha256=rule_hash),
    ])
    assert report.status == "blocked"
