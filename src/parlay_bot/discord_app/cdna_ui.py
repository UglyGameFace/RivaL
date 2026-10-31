"""Private manual lookup dialogs for CDNA market data.

Only the Discord application owner can invoke this personal-use provider API.
No returned provider content reaches RivaL's forecasting, AI or persistence.
"""
from __future__ import annotations

import asyncio
import logging

import discord

from parlay_bot.discord_app.cdna_public import CDNAPersonalReader, CDNAUnavailable

_LOG = logging.getLogger(__name__)


async def verify_cdna_owner(interaction: discord.Interaction, owner_id: int) -> bool:
    """Both the private menu owner and the real application owner must match."""
    if interaction.user.id != owner_id:
        return False
    try:
        app = await asyncio.wait_for(interaction.client.application_info(), timeout=3)
    except (TimeoutError, discord.HTTPException):
        return False
    return getattr(getattr(app, "owner", None), "id", None) == owner_id


def cdna_coin_embed(
    asset: str, results: tuple[tuple[str, str, str, object], ...],
) -> discord.Embed:
    """Present provider observations only, with no trading recommendation."""
    embed = discord.Embed(
        title=f"Crypto.com Predictions (CDNA) | {asset} references",
        description=(
            "**Crypto.com Predictions (CDNA), NOT DraftKings. Independent public references only.** "
            "No contract has been matched to a specific DraftKings listing, "
            "strike, expiry, settlement rule, or actual entry price. "
            "No UP/DOWN bet is approved."
        ),
        color=discord.Color.orange(),
    )
    for symbol, title, event, price in results[:2]:
        if price is None:
            quote = "No verifiable quote; price must not be inferred."
        elif not price.fresh:
            quote = "STALE / NOT TRADING. Price withheld."
        else:
            quote = (
                f"Crypto.com bid {price.bid:.4f} • ask {price.ask:.4f}\\n"
                f"Source updated: {discord.utils.format_dt(price.updated_at, style='R')}"
            )
        embed.add_field(
            name=symbol[:100],
            value=(
                f"{title[:110]}\\nEvent: {event[:120]}\\n{quote}\\n"
                "DraftKings strike/expiry/proposition matching: NOT VERIFIED."
            ),
            inline=False,
        )

    if not results:
        embed.add_field(
            name="NO BET • No matching CDNA candidates",
            value=(
                "The public event search did not produce an exact coin match. "
                "Other exchange forecasts remain separate research."
            ), inline=False,
        )
    embed.set_footer(
        text="Owner-only · on-demand · no AI/CDNA data sharing, orders, or verified DK price"
    )
    return embed


class CDNAPersonalModal(discord.ui.Modal):
    def __init__(self, *, owner_id: int, reader: CDNAPersonalReader, search: bool) -> None:
        super().__init__(
            title="Find CDNA contracts" if search else "Check one CDNA quote",
            timeout=300,
        )
        self.owner_id = owner_id
        self.reader = reader
        self.search = search
        self.input = discord.ui.TextInput(
            label="Crypto keyword (BTC / ETH / SOL / XRP)" if search
                  else "Exact CDNA contract symbol",
            placeholder="BTC" if search else "Enter the official CDNA trading symbol",
            min_length=2, max_length=40 if search else 100,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This is someone else's private menu.", ephemeral=True,
            )
            return
        # Personal-use permission does not extend to every guild member.
        # Never fetch market data until ownership is independently checked.
        try:
            app = await asyncio.wait_for(
                interaction.client.application_info(), timeout=3,
            )
        except (TimeoutError, discord.HTTPException):
            await interaction.response.send_message(
                "Could not verify application ownership. CDNA lookup disabled.",
                ephemeral=True,
            )
            return
        if getattr(getattr(app, "owner", None), "id", None) != self.owner_id:
            await interaction.response.send_message(
                "CDNA's personal quote view is available only to the bot owner.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.search:
                results = await self.reader.search_contracts(str(self.input.value))
                embed = discord.Embed(
                    title="Crypto.com Predictions (CDNA) • NOT DraftKings",
                    description=(
                        "Provider contract discovery only. Symbols are NOT "
                        "verified DraftKings matches. Enter an exact symbol "
                        "under **Check CDNA quote** in the Crypto menu."
                    ),
                    color=discord.Color.blurple(),
                )
                if results:
                    for row in results[:6]:
                        embed.add_field(
                            name=row[0][:100],
                            value=f"{row[1][:110]}\nEvent: {row[2][:125]}",
                            inline=False,
                        )
                else:
                    embed.add_field(
                        name="No contracts found",
                        value="Search did not return a verifiable trading symbol.",
                        inline=False,
                    )
            else:
                price = await self.reader.quote(str(self.input.value).strip())
                embed = discord.Embed(
                    title=f"Crypto.com Predictions (CDNA) • {price.symbol}",
                    description=(
                        f"**{price.title}**\n"
                        "**Crypto.com Predictions reference, NOT a DraftKings executable quote.** "
                        "Use the exact DraftKings contract, settlement rules, "
                        "and actual entry price to assess any bet."
                    ),
                    color=discord.Color.blurple() if price.fresh
                          else discord.Color.orange(),
                )
                embed.add_field(
                    name="Crypto.com reference • unverified DraftKings terms",
                    value=(
                        (
                            f"Bid **{price.bid:.4f}** • Ask **{price.ask:.4f}**\\n"
                            if price.fresh else
                            "**STALE • Price withheld. NOT TRADING.**\\n"
                        )
                        + f"Provider status: **{price.status[:35]}**\\n"
                        + f"Source update: {discord.utils.format_dt(price.updated_at, style='R')}\\n"
                        + "Strike, expiry, proposition and settlement mapping to "
                          "DraftKings: **NOT VERIFIED**."
                    ),
                    inline=False,
                )
                embed.add_field(
                    name="Use in RivaL?",
                    value=(
                        "Personal reference only. No contract matching, "
                        "model training, predictions, AI prompts, downloads, "
                        "Drive exports or orders are triggered by this lookup. "
                        "Provider asks are not guaranteed fills."
                    ),
                    inline=False,
                )
            embed.set_footer(
                text="Owner-only Crypto.com reference • NOT DraftKings • no AI or orders"
            )
            await interaction.edit_original_response(embed=embed)
        except CDNAUnavailable as exc:
            await interaction.edit_original_response(
                content=f"CDNA reference unavailable: {exc}. No borrowed odds or AI call.",
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("Private CDNA personal data lookup failed")
            await interaction.edit_original_response(
                content="CDNA provider lookup failed. No quote or prediction substituted.",
            )
