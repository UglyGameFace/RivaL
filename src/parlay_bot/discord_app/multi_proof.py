"""Private Discord evidence for the genuine multi-exchange shadow challenger."""
from __future__ import annotations

import discord


def multi_proof_embed(report: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"RivaL | {report['asset']} {report['horizon_minutes']}m source ensemble",
        description=(
            "RESEARCH ONLY • Incumbent still active, no automatic model "
            "replacement or real trade. Ten is the maximum configured source "
            "count, not a guarantee ten are fresh at once."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Real prospective sample",
        value=(
            f"Frozen predictions **{report['issued']}** • "
            f"settled matched targets **{report['settled']}** • "
            f"distinct days **{report['days']}**\n"
            f"Contributing venues per forecast: "
            f"**{report['min_sources']}–{report['max_sources']}**"
        ),
        inline=False,
    )
    if report["settled"]:
        embed.add_field(
            name="Same-window forecast error (lower is better)",
            value=(
                f"10-source challenger: **{report['challenger_brier']:.4f}**\n"
                f"Existing venue model: **{report['incumbent_brier']:.4f}**\n"
                f"Difference: **{report['brier_improvement']:+.4f}**"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Same-window forecast error",
            value="No future outcomes scored yet. History is not a live test.",
            inline=False,
        )
    embed.add_field(
        name="Same-market after-cost observations",
        value=(
            f"Quote entries **{report['quote_entries']}** • "
            f"paired exits **{report['quote_pairs']}** • "
            f"missed exits **{report['quote_missed']}**"
        ),
        inline=False,
    )
    for label, values in report["after_cost"].items():
        first = values["challenger_mean_net_bps"]
        second = values["incumbent_mean_net_bps"]
        embed.add_field(
            name=f"{label.title()} costs (matched quote pairs)",
            value=(
                f"Challenger **{first:+.2f} bps** • "
                f"Incumbent **{second:+.2f} bps**"
                if first is not None and second is not None
                else "Not enough same-venue, actual later order-book pairs."
            ),
            inline=False,
        )
    embed.set_footer(
        text="No promotion: 150+ new paired targets, 21+ days, positive after "
             "cost and reliable independent advantage must first be established.",
    )
    return embed
