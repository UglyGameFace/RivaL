from __future__ import annotations

from datetime import date

from parlay_bot.jurisdiction.models import SportsbookAvailability

_VERIFIED = date(2026, 9, 28)

_BOOKS: dict[str, tuple[set[str], str]] = {
    "draftkings": (
        {
            "AZ", "AR", "CO", "CT", "DC", "IL", "IN", "IA", "KS", "KY", "LA",
            "ME", "MD", "MA", "MI", "MO", "NH", "NJ", "NY", "NC", "OH", "OR",
            "PA", "TN", "VT", "VA", "WV", "WY",
        },
        "https://sportsbook.draftkings.com/is-draftkings-available-nationwide-for-sports",
    ),
    "fanduel": (
        {
            "AZ", "CO", "CT", "DC", "IL", "IN", "IA", "KS", "KY", "LA", "MD",
            "MA", "MI", "MO", "NJ", "NY", "NC", "OH", "PA", "TN", "VT", "VA",
            "WV", "WY",
        },
        "https://www.fanduel.com/about/state-of-play",
    ),
    "fanatics": (
        {
            "AZ", "CO", "CT", "DC", "IA", "IL", "IN", "KS", "KY", "LA", "MA",
            "MD", "MI", "MO", "NC", "NJ", "NY", "OH", "PA", "TN", "VA", "VT",
            "WV", "WY",
        },
        "https://betfanatics.com/blog/where-sports-betting-is-legal",
    ),
    "bet365": (
        {
            "AZ", "CO", "DC", "IL", "IN", "IA", "KS", "KY", "LA", "MD", "MI",
            "MO", "NJ", "NC", "OH", "PA", "TN", "VA", "WV",
        },
        "https://www.bet365.com/hub/en-us/states",
    ),
    "hardrockbet": (
        {"AZ", "CO", "FL", "IL", "IN", "MI", "NJ", "OH", "TN", "VA"},
        "https://www.hardrock.bet/sportsbook",
    ),
}


def verified_us_sportsbooks() -> list[SportsbookAvailability]:
    """Return only operator/state combinations verified from operator-owned sources."""

    entries: list[SportsbookAvailability] = []
    for sportsbook, (states, source_url) in _BOOKS.items():
        entries.extend(
            SportsbookAvailability(
                jurisdiction=state,
                sportsbook=sportsbook,
                online_available=True,
                source_url=source_url,
                last_verified=_VERIFIED,
            )
            for state in states
        )
    return entries
