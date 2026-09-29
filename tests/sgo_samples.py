from __future__ import annotations


def sample_sgo_event() -> dict:
    return {
        "eventID": "sgo-event-1",
        "sportID": "BASKETBALL",
        "leagueID": "NBA",
        "cancelled": False,
        "status": {
            "startsAt": "2099-10-01T23:00:00Z",
            "started": False,
            "ended": False,
            "finalized": False,
            "cancelled": False,
        },
        "teams": {
            "home": {"names": {"long": "Boston Celtics"}},
            "away": {"names": {"long": "New York Knicks"}},
        },
        "players": {
            "player-1": {"name": "Example Player"},
        },
        "odds": {
            "points-home-game-sp-home": {
                "oddID": "points-home-game-sp-home",
                "opposingOddID": "points-away-game-sp-away",
                "marketName": "Spread",
                "statEntityID": "home",
                "betTypeID": "sp",
                "sideID": "home",
                "started": False,
                "ended": False,
                "cancelled": False,
                "byBookmaker": {
                    "draftkings": {
                        "odds": "-110",
                        "spread": "-5.5",
                        "available": True,
                        "lastUpdatedAt": "2099-09-28T20:00:00Z",
                        "deeplink": "https://example.invalid/dk/home",
                    },
                    "fanduel": {
                        "odds": "-108",
                        "spread": "-5.5",
                        "available": True,
                        "lastUpdatedAt": "2099-09-28T20:00:00Z",
                        "deeplink": "https://example.invalid/fd/home",
                    },
                },
            },
            "points-away-game-sp-away": {
                "oddID": "points-away-game-sp-away",
                "opposingOddID": "points-home-game-sp-home",
                "marketName": "Spread",
                "statEntityID": "away",
                "betTypeID": "sp",
                "sideID": "away",
                "started": False,
                "ended": False,
                "cancelled": False,
                "byBookmaker": {
                    "draftkings": {
                        "odds": "-110",
                        "spread": "5.5",
                        "available": True,
                        "lastUpdatedAt": "2099-09-28T20:00:00Z",
                    },
                    "fanduel": {
                        "odds": "-112",
                        "spread": "5.5",
                        "available": True,
                        "lastUpdatedAt": "2099-09-28T20:00:00Z",
                    },
                },
            },
            "points-player-1-game-ou-over": {
                "oddID": "points-player-1-game-ou-over",
                "opposingOddID": "points-player-1-game-ou-under",
                "marketName": "Player Points",
                "statEntityID": "player-1",
                "playerID": "player-1",
                "betTypeID": "ou",
                "sideID": "over",
                "started": False,
                "ended": False,
                "cancelled": False,
                "byBookmaker": {
                    "draftkings": {
                        "odds": "+105",
                        "overUnder": "27.5",
                        "available": True,
                        "lastUpdatedAt": "2099-09-28T20:00:00Z",
                    }
                },
            },
            "points-player-1-game-ou-under": {
                "oddID": "points-player-1-game-ou-under",
                "opposingOddID": "points-player-1-game-ou-over",
                "marketName": "Player Points",
                "statEntityID": "player-1",
                "playerID": "player-1",
                "betTypeID": "ou",
                "sideID": "under",
                "started": False,
                "ended": False,
                "cancelled": False,
                "byBookmaker": {
                    "draftkings": {
                        "odds": "-125",
                        "overUnder": "27.5",
                        "available": True,
                        "lastUpdatedAt": "2099-09-28T20:00:00Z",
                    }
                },
            },
        },
    }
