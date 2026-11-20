"""Compact, market-scoped Performance views with explicit XMarket lifecycle."""
from __future__ import annotations

import math
from datetime import UTC, datetime

import discord

from parlay_bot.discord_app.answer_cards import RESEARCH_FOOTER
from parlay_bot.perpetuals.markets import ASSET_NAMES

SECTIONS = {
    "overview": "Overview", "models": "Models", "exchanges": "Exchange results",
    "polymarket": "Polymarket results", "health": "Health", "details": "Full evidence",
}


def _number(value):
    return value if not isinstance(value, bool) and isinstance(value, (int, float)) \
        and math.isfinite(value) else None


def exchange_summary(reports, horizon):
    from parlay_bot.discord_app.home import score_consistency_problems

    report = reports.get("exchange")
    part = report.get("last_7d", {}).get(horizon) if isinstance(report, dict) else None
    if not isinstance(part, dict):
        return "Exchange performance unavailable", "Couldn't load this scope. Press Refresh."
    n, correct = int(part.get("decided") or 0), int(part.get("correct") or 0)
    model, baseline = _number(part.get("brier")), _number(part.get("baseline_brier"))
    if not 0 <= correct <= n or score_consistency_problems(part):
        return "Exchange counts need checking", "Counts are inconsistent; no accuracy shown."
    calls = f"Right **{correct/n:.1%}** · {n:,} graded calls" if n >= 20 else \
        f"{n:,} graded calls · too few to judge"
    if model is None or baseline is None:
        return "Exchange baseline comparison unavailable", calls + "\nNo paired error score available."
    state = "ahead of" if model < baseline else "behind" if model > baseline else "level with"
    return f"Main model: {state} the base-rate forecast", (
        calls + f"\nProbability error **{model:.3f}** vs **{baseline:.3f}** base-rate forecast; lower is better.")


def poly_summary(reports):
    record = reports.get("polymarket")
    if not isinstance(record, dict):
        return "Couldn't load this market. Press Refresh."
    count = int(record.get("settled_forward_contracts") or 0)
    model, market = _number(record.get("forward_brier")), _number(record.get("market_baseline_brier"))
    if not count or model is None or market is None:
        return f"{count:,} graded contracts · no odds comparison available."
    state = "ahead of" if model < market else "behind" if model > market else "level with"
    return (f"{state.capitalize()} market odds · {count:,} graded contracts\n"
            f"Probability error **{model:.3f}** vs market **{market:.3f}**.")


def after_fee_line(reports):
    """The live Polymarket model's after-fee money result, or the gap stated.

    The figure comes from polymarket_learning.summary(): per settled call,
    the favoured side bought at the recorded issuance book's depth-average
    ask, payout minus price minus the published-schedule taker fee. Calls
    whose recorded book cannot be verified as the issuance book are not
    priced, so the covered count is stated whenever it is partial.
    """
    record = reports.get("polymarket")
    if not isinstance(record, dict):
        return "After-fee money result: not measured yet"
    covered = int(record.get("after_fee_calls") or 0)
    total = int(record.get("settled_forward_contracts") or 0)
    mean = _number(record.get("after_fee_mean_net_cents"))
    if not covered or mean is None:
        return "After-fee money result: not measured yet"
    scope = (f"over {covered:,} graded calls" if covered >= total
             else f"over {covered:,} of {total:,} graded calls")
    return f"After-fee money result: {mean:+.1f}¢ per call {scope} (last 7 days)"


def xmarket_summary(record, *, horizon):
    if not isinstance(record, dict) or record.get("status") != "recorded":
        return "XMarket lifecycle unavailable; missing data is not zero models. Press Refresh."
    lines = []
    available = []
    for scope in record["scopes"]:
        h, status = scope["horizon_minutes"], scope["status"]
        if scope["usable_imports"]:
            available.append(h)
        text = {"no_qualifying_fit": "no qualifying fit", "fit_unavailable": "fit unavailable",
                "imported_no_shadows": "imported; waiting for first shadow",
                "shadow_stale": "no recent issuance", "shadowing": "shadowing"}[status]
        if scope["issued_24h"]:
            text += f" · {scope['issued_24h']:,} issued / {scope['settled_24h']:,} graded"
        lines.append(f"**{h}m** — {text}")
    if horizon not in available and available:
        lines.append("Qualified imports are on **" + "/".join(f"{h}m" for h in available)
                     + "**; this selected timeframe has no usable fit.")
    lines.append("Last 24h. Test-model forecasts change the main model only after promotion checks pass.")
    return "\n".join(lines)


def activity_summary(reports, now):
    cycles = reports.get("cycles")
    if not isinstance(cycles, dict):
        return "Forecast/learning heartbeat unavailable."
    lines = []
    for key, label in (("forecast", "Forecasts"), ("learning", "Learning/review")):
        part = cycles.get(key)
        if not isinstance(part, dict) or part.get("status") != "recorded":
            lines.append(f"{label}: unavailable")
            continue
        try:
            stamp = part["checked_at"]
            stamp = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(stamp)
            age = (now-stamp).total_seconds()
            if stamp.utcoffset() is None or age < 0:
                raise ValueError("invalid heartbeat clock")
            state = "recent" if age <= 600 else "stale"
            if key == "learning" and (not part.get("math_ok") or not part.get("model_review_ok")):
                state = "last cycle failed"
            lines.append(f"{label}: {state} · {int(age//60)} min ago")
        except (ValueError, TypeError, KeyError):
            lines.append(f"{label}: clock unavailable")
    return "\n".join(lines)


def build(coin, horizon, reports, *, section, now, venues=(), details_renderer):
    # Reuse the authoritative detailed renderers and saved-copy annotations.
    from parlay_bot.discord_app.home import (
        _add_field,
        _best_sections,
        _calibration_sections,
        _contest_text,
        _health_text,
        _scoreboard_text,
        _trials_sections,
    )

    if section == "details":
        embeds = details_renderer(coin, horizon, reports, now=now, venues=venues)
        record = reports.get("xmarket")
        if isinstance(record, dict) and record.get("status") == "recorded":
            _add_field(embeds[1], reports.get("_stale") or {}, "XMarket activity · all timeframes",
                       xmarket_summary(record, horizon=horizon), ("xmarket",))
            for prefix in ("🏆 Best model", "🧬 Model Lab", "🎲 Polymarket contest"):
                if sum(len(e) for e in embeds) <= 5900:
                    break
                for e in embeds:
                    for index, f in enumerate(e.fields):
                        if f.name.startswith(prefix):
                            e.remove_field(index)
                            break
        return embeds
    section = section if section in SECTIONS else "overview"
    label = ASSET_NAMES.get(coin, coin)
    headline, main = exchange_summary(reports, horizon)
    embed = discord.Embed(title=f"📊 {label} · {horizon}-min · {SECTIONS[section]}",
                          color=discord.Color.blurple())
    stale = reports.get("_stale") or {}

    def field(name, value, *sources):
        _add_field(embed, stale, name, value, sources)

    if section == "overview":
        # Product pass PR-2: the Overview leads with the existing Polymarket
        # verdict (the same headline the Full evidence scoreboard computes);
        # the exchange-model line demotes to its field. PR-2b: the Polymarket
        # field carries the model's after-fee money result (after_fee_line),
        # falling back to stating the gap plainly when it is not measured.
        verdict, _scoreboard = _scoreboard_text(coin, horizon, reports, now,
                                                polymarket_only=True)
        embed.description = f"{verdict}\nTest models are evaluated separately."
        field("Exchange model · last 7 days", f"{headline}\n{main}", "exchange")
        field("Polymarket · last 7 days",
              poly_summary(reports) + "\n" + after_fee_line(reports),
              "polymarket")
        field(f"XMarket · {coin}", xmarket_summary(reports.get("xmarket"), horizon=horizon), "xmarket")
        board = reports.get("trials")
        if isinstance(board, dict):
            rows = board.get("rows") or []
            ahead = sum(r.status in ("ahead", "PASSED") for r in rows)
            passed = sum(r.passed for r in rows)
            text = (f"{len(rows)} test models scored · {ahead} ahead of the main model · {passed} passed.\n"
                    "Open Models for the comparisons and import status.")
        else:
            text = "Test model comparisons unavailable. Open Models to check again."
        field(f"Test models · {horizon}m", text, "trials")
        field("Activity", activity_summary(reports, now))
    elif section == "models":
        trials = _trials_sections(reports)
        embed.description = f"**{coin} · {horizon}m comparisons**\n" + trials["leader"]
        field("XMarket activity · all timeframes", xmarket_summary(reports.get("xmarket"), horizon=horizon),
              "xmarket")
        rows = (reports.get("trials") or {}).get("rows", []) if isinstance(reports.get("trials"), dict) else []
        status = reports.get("xmarket")
        scope = next((s for s in status.get("scopes", []) if s["horizon_minutes"] == horizon), None) \
            if isinstance(status, dict) else None
        if scope and scope["issued_24h"] and not any(r.family == "xmarket_v1" for r in rows):
            field("XMarket comparison pending", "Forecasts are recorded. The hourly comparison "
                  "has not supplied a scored XMarket row for this scope yet.", "xmarket", "trials")
        if trials["table"]:
            field(trials["name"], trials["table"] + trials["missing"], "trials")
        field("Comparison scope", trials["how_to_read"] or "No scored trial comparisons yet.", "trials")
        leaders, lab = _best_sections(reports)
        if leaders:
            field(f"Leaders · {coin}", leaders, "best")
        if lab:
            field("Model Lab", lab, "best")
    elif section == "exchanges":
        embed.description = f"**{headline}.**\nMain model only · last 7 days."
        field("Measured performance", main, "exchange")
        confidence, conditions = _calibration_sections(reports)
        field("Confidence calibration", confidence, "calibration")
        if conditions:
            field("Market conditions", conditions, "calibration")
    elif section == "polymarket":
        headline, scores = _scoreboard_text(coin, horizon, reports, now, polymarket_only=True)
        embed.description = headline + "\nPolymarket contracts only · last 7 days."
        field("Against the market's own odds", scores.replace("Its bets", "Practice bets"), "polymarket")
        contest = _contest_text(reports)
        if contest:
            field("Polymarket contest", contest)
    else:
        embed.description = "Operational activity and baseline evidence are different checks."
        field("Running and baseline checks", _health_text(coin, horizon, reports, now, venues), "benefit")
        field("XMarket lifecycle", xmarket_summary(reports.get("xmarket"), horizon=horizon), "xmarket")
    if stale:
        embed.description = "⚠️ Sections marked OLD COPY are cached after a failed read.\n" + embed.description
    embed.timestamp = now.astimezone(UTC)
    embed.set_footer(text=f"{RESEARCH_FOOTER} · Snapshot — Refresh checks again · Full evidence keeps the detailed tables")
    return [embed]
