"""Private, phone-friendly Discord menu for RivaL's actual market data.

Every screen reads the authoritative PostgreSQL-backed market store. A healthy
label requires fresh successful collection, not merely a stale 'ok' database row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)

VENUE_NAMES = {
    "hyperliquid": "Hyperliquid",
    "dydx": "dYdX",
    "bitget": "Bitget Futures",
    "gate": "Gate Futures",
}
MARKET_SYMBOLS = {
    "hyperliquid": ("{asset}", "USDC"),
    "dydx": ("{asset}-USD", "USD"),
    "bitget": ("{asset}USDT", "USDT"),
    "gate": ("{asset}_USDT", "USDT"),
}


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _relative(value: Any) -> str:
    stamp = _date(value)
    return discord.utils.format_dt(stamp, style="R") if stamp is not None else "Never"


def _is_fresh(
    row: dict[str, Any] | None, *, now: datetime, interval_seconds: int,
) -> bool:
    if row is None or row.get("status") != "ok":
        return False
    stamp = _date(row.get("last_success_at"))
    return stamp is not None and timedelta(0) <= now - stamp <= timedelta(
        seconds=interval_seconds * 3
    )


def _health(
    row: dict[str, Any] | None, *, now: datetime, interval_seconds: int,
) -> str:
    if row is None:
        return "⚪ Waiting for first check"
    if row.get("status") != "ok":
        reason = str(row.get("status", "unknown")).replace("_", " ")
        return f"🔴 {reason.title()}"
    return "🟢 Collecting" if _is_fresh(
        row, now=now, interval_seconds=interval_seconds
    ) else "🟡 Stale"


def _index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(str(r["venue"]), str(r["asset"])): r for r in rows}


def _header(title: str, *, accent: bool = True) -> discord.Embed:
    return discord.Embed(
        title=title,
        color=discord.Color.green() if accent else discord.Color.orange(),
    )


def overview_embed(
    rows: list[dict[str, Any]], venues: tuple[str, ...],
    assets: tuple[str, ...], interval_seconds: int,
    *, enabled: bool, now: datetime | None = None,
) -> discord.Embed:
    clock = now or datetime.now(UTC)
    by_key = _index(rows)
    embed = _header("RivaL  |  Data Center", accent=enabled)
    if not enabled:
        embed.description = (
            "Collection is disabled or PostgreSQL is not configured. "
            "No live source activity is being reported."
        )
        embed.set_footer(text="Use Exchanges or Markets to inspect saved data.")
        return embed

    good = [
        (venue, asset)
        for venue in venues for asset in assets
        if _is_fresh(by_key.get((venue, asset)), now=clock,
                     interval_seconds=interval_seconds)
    ]
    live_venues = len({venue for venue, _ in good})
    stored = sum(
        max(0, int(row.get("total_new_candles") or 0))
        for row in rows if row.get("venue") in venues
    )
    newest = max(
        (_date(r.get("last_success_at")) for r in rows
         if _date(r.get("last_success_at")) is not None),
        default=None,
    )
    embed.description = (
        "**Your live collection overview**\n"
        "Monitor source availability and stored candles in one place."
    )
    embed.add_field(
        name="🌐 Exchanges collecting",
        value=f"**{live_venues} / {len(venues)}** configured",
        inline=True,
    )
    embed.add_field(
        name="📈 Fresh market feeds",
        value=f"**{len(good)} / {len(venues) * len(assets)}** pairs",
        inline=True,
    )
    embed.add_field(
        name="🗃️ Candles saved",
        value=f"**{stored:,}** total historical + new",
        inline=True,
    )
    embed.add_field(
        name="🕒 Last successful sync",
        value=discord.utils.format_dt(newest, style="R") if newest else "Not yet",
        inline=True,
    )
    embed.add_field(
        name="🎯 Source goal",
        value=f"**{len(venues)} / 10** coded integrations\n"
              "Working sources shown above; missing integrations are not counted.",
        inline=False,
    )
    if len(good) < len(venues) * len(assets):
        embed.add_field(
            name="Needs attention",
            value="Some feeds are unavailable, stale or awaiting their first run. "
                  "Open **Exchanges** for the reason.",
            inline=False,
        )
    embed.set_footer(
        text="Read-only market data • Past candles do not prove forecast accuracy"
    )
    return embed


def exchange_embed(
    rows: list[dict[str, Any]], venue: str, assets: tuple[str, ...],
    interval_seconds: int, *, now: datetime | None = None,
) -> discord.Embed:
    clock = now or datetime.now(UTC)
    by_key = _index(rows)
    successes = sum(
        _is_fresh(by_key.get((venue, asset)), now=clock,
                  interval_seconds=interval_seconds)
        for asset in assets
    )
    embed = _header(f"RivaL  |  {VENUE_NAMES.get(venue, venue.title())}")
    embed.description = (
        f"**{successes}/{len(assets)} markets actively collecting**\n"
        "Exchange-specific status, as recorded in PostgreSQL."
    )
    for asset in assets[:10]:
        row = by_key.get((venue, asset))
        status = _health(row, now=clock, interval_seconds=interval_seconds)
        embed.add_field(
            name=f"{asset} perpetual",
            value=(
                f"{status}\n"
                f"Last success: {_relative(row.get('last_success_at') if row else None)}\n"
                f"Candles saved: **{int(row.get('total_new_candles') or 0) if row else 0:,}**"
            ),
            inline=False,
        )
    embed.set_footer(
        text="Choose a different exchange below • Red and stale sources are excluded from fresh counts"
    )
    return embed


def market_embed(
    rows: list[dict[str, Any]], asset: str,
    latest: dict[str, dict[str, Any] | None],
    venues: tuple[str, ...], interval_seconds: int,
    *, now: datetime | None = None,
) -> discord.Embed:
    clock = now or datetime.now(UTC)
    by_key = _index(rows)
    embed = _header(f"RivaL  |  {asset} Market")
    embed.description = (
        "**Price observations by exchange**\n"
        "Each venue retains its own contract and quote currency. "
        "Prices are not combined into a fake market average."
    )
    for venue in venues:
        row = by_key.get((venue, asset))
        candle = latest.get(venue)
        symbol_template, quote = MARKET_SYMBOLS.get(venue, ("{asset}", "?"))
        if candle:
            price = float(candle["close_price"])
            close = _relative(candle.get("closed_at"))
            detail = f"Last saved close: **{price:,.4f} {quote}** ({close})"
        else:
            detail = "No stored candle yet"
        embed.add_field(
            name=f"{VENUE_NAMES.get(venue, venue.title())}  •  "
                 f"{symbol_template.format(asset=asset)}",
            value=f"{_health(row, now=clock, interval_seconds=interval_seconds)}\n{detail}",
            inline=False,
        )
    embed.set_footer(text="Historical observations only • Not a buy/sell signal")
    return embed


class _NavButton(discord.ui.Button["MarketDataDashboard"]):
    def __init__(self, screen: str, label: str, emoji: str) -> None:
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                         custom_id=f"rival:data:{screen}", row=0)
        self.screen = screen

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.show(interaction, self.screen)


class _RefreshButton(discord.ui.Button["MarketDataDashboard"]):
    def __init__(self) -> None:
        super().__init__(label="Refresh", emoji="🔄",
                         style=discord.ButtonStyle.primary,
                         custom_id="rival:data:refresh", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.show(interaction, self.view.screen)


class _ChooseItem(discord.ui.Select["MarketDataDashboard"]):
    def __init__(self, kind: str, values: tuple[str, ...], selected: str) -> None:
        options = [
            discord.SelectOption(
                label=VENUE_NAMES.get(value, value),
                value=value,
                default=value == selected,
            )
            for value in values[:25]
        ]
        super().__init__(
            placeholder="Choose an exchange" if kind == "exchange"
                        else "Choose BTC, ETH, SOL…",
            options=options, min_values=1, max_values=1,
            custom_id=f"rival:data:choose:{kind}", row=1,
        )
        self.kind = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        if self.kind == "exchange":
            self.view.selected_venue = self.values[0]
        else:
            self.view.selected_asset = self.values[0]
        await self.view.show(interaction, self.view.screen)


class MarketDataDashboard(discord.ui.View):
    """One ephemeral dashboard, navigating in-place; only invoking user controls it."""

    def __init__(
        self, *, owner_id: int, market: PerpetualMarketStore | None,
        venues: tuple[str, ...], assets: tuple[str, ...],
        interval_seconds: int, enabled: bool,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.market = market
        self.venues = venues
        self.assets = assets
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.screen = "overview"
        self.selected_venue = venues[0] if venues else ""
        self.selected_asset = assets[0] if assets else ""
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Open your own private data dashboard with /rival-data.",
                ephemeral=True,
            )
            return False
        return True

    def _rebuild(self) -> None:
        self.clear_items()
        for screen, label, emoji in (
            ("overview", "Overview", "🏠"),
            ("exchanges", "Exchanges", "🌐"),
            ("markets", "Markets", "📊"),
        ):
            item = _NavButton(screen, label, emoji)
            if self.screen == screen:
                item.style = discord.ButtonStyle.success
                item.disabled = True
            self.add_item(item)
        self.add_item(_RefreshButton())
        if self.screen == "exchanges" and self.venues:
            self.add_item(_ChooseItem("exchange", self.venues, self.selected_venue))
        if self.screen == "markets" and self.assets:
            self.add_item(_ChooseItem("market", self.assets, self.selected_asset))

    def _snapshot(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any] | None]]:
        if self.market is None:
            return [], {}
        rows = self.market.feed_health()
        candles: dict[str, dict[str, Any] | None] = {}
        if self.screen == "markets" and self.selected_asset:
            for venue in self.venues:
                symbol, quote = MARKET_SYMBOLS.get(venue, ("{asset}", "?"))
                market_rows = self.market.recent_candles(
                    venue=venue, contract=symbol.format(asset=self.selected_asset),
                    quote=quote, interval_minutes=5, count=1,
                )
                candles[venue] = market_rows[-1] if market_rows else None
        return rows, candles

    async def render(self) -> discord.Embed:
        try:
            rows, candles = await asyncio.to_thread(self._snapshot)
        except Exception:  # noqa: BLE001 - no DB secrets shown to Discord users
            _LOG.exception("RivaL data dashboard failed to read market snapshot")
            embed = _header("RivaL  |  Data Center", accent=False)
            embed.description = "Market data is temporarily unavailable. Retry in a moment."
            return embed
        if self.screen == "exchanges" and self.selected_venue:
            return exchange_embed(
                rows, self.selected_venue, self.assets, self.interval_seconds,
            )
        if self.screen == "markets" and self.selected_asset:
            return market_embed(
                rows, self.selected_asset, candles, self.venues, self.interval_seconds,
            )
        return overview_embed(
            rows, self.venues, self.assets, self.interval_seconds,
            enabled=self.enabled,
        )

    async def show(self, interaction: discord.Interaction, screen: str) -> None:
        await interaction.response.defer()
        self.screen = screen
        self._rebuild()
        await interaction.edit_original_response(embed=await self.render(), view=self)
