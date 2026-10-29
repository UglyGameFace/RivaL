"""Discover actual sports and leagues from fresh bookmaker data.

No invented universality: the catalog comes from the configured provider's
verified, unstarted, fresh fixture/selection rows, not a hardcoded sport list.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.storage.hot import RelationalHotStore


def discovered_leagues(
    store: RelationalHotStore, *, now: datetime | None = None,
    max_age: timedelta = timedelta(minutes=30), limit: int = 12,
) -> list[dict]:
    if max_age <= timedelta(0) or not 1 <= limit <= 25:
        raise ValueError("invalid live sports discovery budget")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    stamp = stamp.astimezone(UTC)
    store.initialize()
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT f.provider, f.sport_id, f.sport_name,
                      f.tournament_id, f.tournament_name,
                      COUNT(DISTINCT f.fixture_id) AS events,
                      COUNT(DISTINCT c.bookmaker) AS books,
                      MAX(c.observed_at) AS observed_at
               FROM fixtures AS f
               JOIN current_odds AS c
                 ON c.provider=f.provider AND c.fixture_id=f.fixture_id
               WHERE c.active=1 AND c.main_line=1 AND c.price_decimal>1
                 AND c.observed_at>=? AND c.observed_at<=?
                 AND (f.start_time IS NULL OR f.start_time>?)
               GROUP BY f.provider,f.sport_id,f.sport_name,
                        f.tournament_id,f.tournament_name
               ORDER BY events DESC,observed_at DESC LIMIT ?""",
            (
                (stamp - max_age).isoformat(), stamp.isoformat(),
                stamp.isoformat(), limit,
            ),
        ).fetchall()
    return [dict(row) for row in rows]


def sports_discovery_embed(
    store: RelationalHotStore, *, configured_leagues: tuple[str, ...],
    collector_enabled: bool, now: datetime | None = None,
) -> discord.Embed:
    stamp = now or datetime.now(UTC)
    embed = discord.Embed(
        title="RivaL • All Sports",
        color=discord.Color.blurple(), timestamp=stamp,
        description=(
            "**Actually observed sportsbook markets**, not a claim that every "
            "sport is covered. Availability is provider, quota, location and "
            "bookmaker dependent."
        ),
    )
    if not collector_enabled:
        embed.add_field(
            name="No live sportsbook connection",
            value="SportsGameOdds collector is not configured. No current odds verified.",
            inline=False,
        )
        return embed
    try:
        leagues = discovered_leagues(store, now=stamp)
    except Exception:  # noqa: BLE001 - do not leak DB/provider details
        embed.add_field(
            name="Unavailable", value="Could not verify live sportsbook coverage.",
            inline=False,
        )
        return embed
    if not leagues:
        embed.add_field(
            name="No recent markets",
            value="No fresh, future-fixture book prices verified. PASS.",
            inline=False,
        )
    else:
        lines = []
        for row in leagues:
            sport = str(row.get("sport_name") or row.get("sport_id") or "Unknown sport")
            league = str(
                row.get("tournament_name") or row.get("tournament_id") or "Unspecified"
            )
            lines.append(
                f"**{discord.utils.escape_markdown(sport[:35])} · "
                f"{discord.utils.escape_markdown(league[:40])}** — "
                f"{int(row['events'])} fixtures, {int(row['books'])} books"
            )
        embed.add_field(
            name="Live markets found",
            value="\n".join(lines)[:1000], inline=False,
        )
    embed.add_field(
        name="Provider league filters",
        value=", ".join(configured_leagues)[:900] or "Not configured",
        inline=False,
    )
    embed.set_footer(
        text="Freshness ≤30m • Unstarted fixtures • Not proof of a profitable or legal bet"
    )
    return embed
