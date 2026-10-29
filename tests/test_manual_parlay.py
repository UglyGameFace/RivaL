import pytest

from parlay_bot.parlays.manual import selected_slip


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
