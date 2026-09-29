from datetime import UTC, datetime

from parlay_bot.ingestion.oddspapi import normalize_current_odds


def sample_payload() -> dict:
    return {
        "fixtureId": "fixture-1",
        "sportId": 4,
        "tournamentId": 99,
        "statusId": 0,
        "statusName": "Pre-Game",
        "startTime": "2026-10-01T23:00:00Z",
        "updatedAt": "2026-09-28T20:00:00Z",
        "participant1Name": "Home",
        "participant2Name": "Away",
        "sportName": "Basketball",
        "tournamentName": "NBA",
        "bookmakerOdds": {
            "fanduel": {
                "bookmakerIsActive": True,
                "suspended": False,
                "markets": {
                    "100": {
                        "marketActive": True,
                        "outcomes": {
                            "101": {
                                "players": {
                                    "0": {
                                        "active": True,
                                        "price": 1.91,
                                        "priceAmerican": "-110",
                                        "priceFractional": "10/11",
                                        "limit": 5000,
                                        "changedAt": "2026-09-28T19:59:00Z",
                                        "bookmakerChangedAt": None,
                                        "mainLine": True,
                                        "playerName": None,
                                        "exchangeMeta": None,
                                    }
                                }
                            }
                        },
                    },
                    "5000": {
                        "marketActive": True,
                        "outcomes": {
                            "5001": {
                                "players": {
                                    "12345": {
                                        "active": True,
                                        "price": 2.05,
                                        "priceAmerican": "+105",
                                        "priceFractional": "21/20",
                                        "limit": 250,
                                        "changedAt": "2026-09-28T19:58:00Z",
                                        "bookmakerChangedAt": "2026-09-28T19:57:59Z",
                                        "mainLine": True,
                                        "playerName": "Example Player",
                                        "exchangeMeta": None,
                                    }
                                }
                            }
                        },
                    },
                },
            }
        },
    }


def test_normalizes_standard_and_player_prop_selections() -> None:
    observed = datetime(2026, 9, 28, 20, 1, tzinfo=UTC)
    board = normalize_current_odds(sample_payload(), observed_at=observed)

    assert board.fixture_id == "fixture-1"
    assert board.tournament_name == "NBA"
    assert len(board.observations) == 2

    standard = board.observations[0]
    prop = board.observations[1]

    assert standard.player_id == "0"
    assert standard.player_name is None
    assert standard.price_american == "-110"
    assert standard.main_line is True

    assert prop.player_id == "12345"
    assert prop.player_name == "Example Player"
    assert prop.price_decimal == 2.05
    assert prop.bookmaker_changed_at == datetime(2026, 9, 28, 19, 57, 59, tzinfo=UTC)


def test_selection_key_is_stable_but_state_fingerprint_changes_with_price() -> None:
    board = normalize_current_odds(sample_payload())
    original = board.observations[0]

    changed_payload = sample_payload()
    player = changed_payload["bookmakerOdds"]["fanduel"]["markets"]["100"]["outcomes"]["101"][
        "players"
    ]["0"]
    player["price"] = 2.0
    player["priceAmerican"] = "+100"
    player["changedAt"] = "2026-09-28T20:02:00Z"

    changed = normalize_current_odds(changed_payload).observations[0]

    assert original.selection_key == changed.selection_key
    assert original.state_fingerprint != changed.state_fingerprint
