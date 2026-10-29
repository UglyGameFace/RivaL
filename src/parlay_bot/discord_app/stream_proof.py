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
    gates = report.get("readiness")
    if gates is not None:
        descriptions = {
            "forward_windows": "150 qualified forward forecasts",
            "observed_exits": "150 completed entry/exit pairs",
            "distinct_target_windows": "150 distinct target windows",
            "observed_days": "21 days with real paired outcomes",
            "quote_coverage": "At least 80% quote coverage",
            "after_cost_stress": "Profitable after both cost stresses",
        }
        embed.add_field(
            name="Qualification progress",
            value="\n".join(
                f"{'✅' if passed else '⏳'} {descriptions[key]}"
                for key, passed in gates.items()
            ),
            inline=False,
        )
        issued = report.get("issued_windows", 0)
        strong = report.get("eligible_windows", 0)
        embed.add_field(
            name="Where evidence is missing",
            value=(
                f"Actual forecasts issued: **{issued}** • "
                f"Directional: **{strong}** • "
                f"Weak: **{report.get('weak_windows', max(issued-strong,0))}**\n"
                f"Qualified forecasts without entry: "
                f"**{report.get('missing_entries', max(strong-report['entries'],0))}**\n"
                f"Distinct complete target windows: "
                f"**{report.get('distinct_target_windows', 0)}**"
                f" • still needed: **{report.get('needed_unique_exits', 150)}**\n"
                f"Days still needed: **{report.get('needed_days', 21)}**"
            ),
            inline=False,
        )
        if issued == 0:
            embed.add_field(
                name="Why the trial cannot progress",
                value=(
                    "No eligible issuer history for this exact coin, horizon "
                    "and supported exchange. Check /rival-data, "
                    "/rival-stream-status and autonomous collector health."
                ),
                inline=False,
            )
        elif strong == 0:
            embed.add_field(
                name="Why the trial cannot progress",
                value=(
                    "Models are issuing predictions, but none meets the "
                    "predeclared 56% directional threshold. Do not lower the "
                    "threshold just to make the sample look better."
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
