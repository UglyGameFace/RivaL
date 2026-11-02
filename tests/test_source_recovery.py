"""Evidence classification and automatic recovery never invent old market records."""
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.source_recovery import decision, deferred

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def test_empty_provider_page_is_not_permanently_unavailable():
    proof = decision(
        asset="BTC", family="premium", month="2021-03",
        reason="incomplete UTC grid in window 123:456 (0/96)", now=NOW,
    )
    assert proof["source_status"] == "source_returned_no_rows_not_permanently_proven"
    assert proof["permanent"] is False
    assert deferred(proof, asset="BTC", family="premium", month="2021-03",
                    now=NOW + timedelta(days=1))
    assert not deferred(proof, asset="BTC", family="premium", month="2021-03",
                        now=NOW + timedelta(days=8))
    assert not deferred(proof, asset="ETH", family="premium", month="2021-03",
                        now=NOW)


def test_official_listing_is_only_supported_permanent_classification():
    p = decision(
        asset="SOL", family="market", month="2021-05", reason="no original rows",
        now=NOW, contract_start="2023-03-17T12:00:00+00:00",
    )
    assert p["permanent"] is True
    assert p["retry_after_utc"] is None
    assert deferred(p, asset="SOL", family="market", month="2021-05", now=NOW)
    current_month = decision(
        asset="SOL", family="market", month="2023-03", reason="no original rows",
        now=NOW, contract_start="2023-03-17T12:00:00+00:00",
    )
    assert not current_month["permanent"]


def test_gap_retries_and_throttles_are_bounded_not_ignored():
    gap = decision(asset="BTC", family="market", month="2026-08",
                   reason="incomplete UTC grid in window (95/96)", now=NOW)
    assert gap["source_status"] == "isolated_source_gap_repair_pending"
    throttle = decision(asset="BTC", family="mark", month="2026-08",
                        reason="HTTP 429 source throttled", now=NOW, attempts=8)
    assert throttle["source_status"] == "temporary_source_throttle_or_transport"
    assert (datetime.fromisoformat(throttle["retry_after_utc"]) - NOW
            <= timedelta(hours=12))
    unknown = decision(asset="BTC", family="index", month="2023-06",
                       reason="HTTP 404 unknown source", now=NOW)
    assert not unknown["permanent"]


def test_untrusted_prior_state_cannot_skip_source():
    p = decision(asset="BTC", family="mark", month="2026-08",
                 reason="incomplete UTC grid in window (0/96)", now=NOW)
    for bad in ({**p, "asset": "XRP"}, {**p, "retry_after_utc": "not-time"},
                {**p, "last_checked_utc": "not-time"}):
        assert not deferred(bad, asset="BTC", family="mark",
                            month="2026-08", now=NOW)
    with pytest.raises(ValueError):
        decision(asset="LTC", family="market", month="2026-08",
                 reason="missing", now=NOW)
