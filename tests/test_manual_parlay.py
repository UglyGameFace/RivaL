import pytest

from datetime import UTC, datetime, timedelta

from parlay_bot.parlays.manual import quote_is_fresh, selected_slip


def leg(fixture, price, book="draftkings"):
    return {
        "fixture_id": fixture, "selection_key": fixture,
        "bookmaker": book, "price_decimal": price,
    }


def test_manual_slip_probability_is_market_implied():
    result = selected_slip([leg("a", 2.0), leg("b", 2.5)])
    assert result["combined_decimal_odds"] == pytest.approx(5.0)
    assert result["combined_implied_probability"] == pytest.approx(0.2)
    assert "not a calibrated" in result["assumption"]


def test_same_game_not_naively_multiplied():
    with pytest.raises(ValueError, match="correlation"):
        selected_slip([leg("a", 2.0), {**leg("a", 2.5), "selection_key": "other"}])


def test_mixed_books_not_executable():
    with pytest.raises(ValueError, match="one sportsbook"):
        selected_slip([leg("a", 2.0), leg("b", 2.0, "fanduel")])


@pytest.mark.parametrize("price", [1.0, float("nan"), float("inf")])
def test_bad_price_rejected(price):
    with pytest.raises(ValueError):
        selected_slip([leg("a", price)])


def test_matching_book_different_source_must_not_be_combined():
    a = {**leg("one", 2), "provider": "oddspapi"}
    b = {**leg("two", 2), "provider": "sportsgameodds"}
    with pytest.raises(ValueError, match="different providers"):
        selected_slip([a, b])


def test_cached_quote_expiry_uses_real_observation_and_future_game_time():
    now = datetime(2026, 10, 31, 15, tzinfo=UTC)
    quote = {
        "observed_at": (now - timedelta(minutes=29)).isoformat(),
        "start_time": (now + timedelta(minutes=15)).isoformat(),
        "price_decimal": 2.01,
    }
    assert quote_is_fresh(quote, now=now)
    assert not quote_is_fresh(quote, now=now + timedelta(minutes=2))
    assert not quote_is_fresh({**quote, "start_time": now.isoformat()}, now=now)
    assert not quote_is_fresh({**quote, "observed_at": (now + timedelta(seconds=3)).isoformat()}, now=now)
    assert not quote_is_fresh({**quote, "observed_at": None}, now=now)
    assert not quote_is_fresh({**quote, "price_decimal": float("nan")}, now=now)
    assert not quote_is_fresh({**quote, "price_decimal": 1.0}, now=now)
    assert not quote_is_fresh({**quote, "start_time": "invalid"}, now=now)
    assert not quote_is_fresh({**quote, "observed_at": now.replace(tzinfo=None).isoformat()}, now=now)
    assert not quote_is_fresh({**quote, "start_time": now.replace(tzinfo=None).isoformat()}, now=now)


def test_quote_without_both_timestamps_is_not_safe_for_interactive_slips():
    now = datetime(2026, 10, 31, tzinfo=UTC)
    assert not quote_is_fresh({"price_decimal": 2.0}, now=now)
    with pytest.raises(ValueError):
        quote_is_fresh({"price_decimal": 2.0}, now=now.replace(tzinfo=None))
