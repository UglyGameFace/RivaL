"""One honest Discord view for source-matched forward after-cost proof."""
from __future__ import annotations

import discord


def stream_proof_embed(report: dict) -> discord.Embed:
    asset = report["asset"]
    horizon = report["horizon_minutes"]
    embed = discord.Embed(
        title=f"📊 RivaL | {asset} {horizon}m after-cost audit",
        description=(
            f"**{report['status']}**\n"
            "Forward-only, same-exchange bid/ask entries and exits. "
            "Fees and slippage are assumed, not signed exchange fills. "
            "No real-money profit is established."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Evidence coverage",
        value=(
            f"Eligible exchange/window cases: **{report['eligible_windows']}**\n"
            f"Entry quotes: **{report['entries']}** • "
            f"Paired exits: **{report['closed']}**\n"
            f"Missed exits: **{report['missed_exits']}** • "
            f"Observed coverage: **{report['coverage']:.1%}** • "
            f"UTC days: **{report['days']}**"
        ),
        inline=False,
    )
    for label, stats in report["costs"].items():
        mean = stats["mean_net_bps"]
        lower = stats["daily_lower_bps"]
        summary = (
            f"Extra assumed costs: {stats['assumed_extra_cost_bps']:.0f} bps\n"
            + (f"Net mean: **{mean:+.2f} bps**" if mean is not None
               else "No verified complete quote windows yet")
            + (f" • day-block lower: {lower:+.2f} bps"
               if lower is not None else " • lower bound unavailable")
        )
        embed.add_field(
            name=f"{label.title()} cost stress",
            value=summary,
            inline=False,
        )
    embed.set_footer(
        text="Research only • 150 forward outcomes, 21 days, 80% coverage "
             "and positive severe-cost tests before shadow qualification.",
    )
    return embed
