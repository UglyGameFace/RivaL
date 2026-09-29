from __future__ import annotations

import discord

from parlay_bot.brand import APP_NAME, APP_TAGLINE
from parlay_bot.jurisdiction.onboarding import OnboardingLocation
from parlay_bot.jurisdiction.states import state_name

_BOOK_NAMES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "fanatics": "Fanatics Sportsbook",
    "bet365": "bet365",
    "hardrockbet": "Hard Rock Bet",
}


def display_book(book: str) -> str:
    return _BOOK_NAMES.get(book, book.replace("_", " ").title())


def onboarding_embed() -> discord.Embed:
    embed = discord.Embed(
        title=APP_NAME,
        description=(
            f"**{APP_TAGLINE}**\n\n"
            "Set your betting state once and RivaL will automatically hide "
            "sportsbooks that are not verified for that jurisdiction."
        ),
    )
    embed.add_field(
        name="Privacy",
        value=(
            "Use a ZIP code or enter your state directly. If you use a ZIP, "
            "**RivaL saves only the state** and discards the ZIP."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Your saved state filters displayed books; it does not verify your physical location."
    )
    return embed


def location_embed(location: OnboardingLocation) -> discord.Embed:
    books = (
        "\n".join(f"• {display_book(book)}" for book in location.eligible_books)
        if location.eligible_books
        else "No online sportsbooks are currently verified in RivaL's catalog for this state."
    )
    embed = discord.Embed(
        title=f"{APP_NAME} • {location.state_name}",
        description=f"**{APP_TAGLINE}**",
    )
    embed.add_field(
        name="Verified sportsbook options",
        value=books,
        inline=False,
    )
    embed.add_field(
        name="Location setting",
        value=(
            f"{location.state_name} ({location.state_code})\n"
            "RivaL uses this to filter prices and books shown to you."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Sportsbooks perform their own legal eligibility and physical-location checks."
    )
    return embed


def saved_location_embed(*, state_code: str, eligible_books: list[str]) -> discord.Embed:
    return location_embed(
        OnboardingLocation(
            state_code=state_code,
            state_name=state_name(state_code),
            eligible_books=tuple(eligible_books),
            source="saved",
        )
    )
