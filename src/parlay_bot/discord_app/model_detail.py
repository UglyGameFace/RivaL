"""One model on trial, in detail: what it is, how accurate, where, and what is left.

Detailed Research lists every model on trial for the chosen coin; picking
one opens this page. Everything comes from records RivaL already keeps:

- the hourly promotion check (``rival_family_promotion``): the trial clock,
  the deduplicated paired gain over the main model per exchange, the
  after-fees comparison, and whether it passed;
- early stop (``rival_family_early_stop``): where it was cut for losing;
- the test-model ledger (``rival_model_shadow_forecasts``): its own calls
  and the main model's call on the same candle, so hit rates, confidence
  bands and market conditions (``perp_forward_regimes``) compare like with
  like.

Nothing here can promote a model or place a trade.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.trial_board import (
    FRESH,
    LEVEL_WITHIN,
    MIN_DAYS,
    MIN_RESULTS,
    TOO_FEW,
    is_retired,
    retired_families,
    short_name,
)
from parlay_bot.storage.optional_reads import optional_rows

HORIZONS = (5, 15, 60)
LEDGER_DAYS = 14
BANDS = ((0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 1.00))
CONDITIONS = ("quiet", "normal", "turbulent")

# Plain descriptions, written from each model's own module documentation.
HOW_IT_WORKS = {
    "Trend-only": (
        "Uses the main model's own call, but only speaks when the market is trending "
        "(the last 20 five-minute bars moved mostly one way). Tests whether the main "
        "model is only worth listening to in trends."),
    "Trend learn": (
        "Starts as a copy of the main model, then keeps learning only from trending "
        "markets, at the main model's learning speed."),
    "Fast trend": (
        "Same as Trend learn, but learns twice as fast from each trending result."),
    "Trend v2": (
        "A model trained offline only on past trending markets, used only when the "
        "market is trending now."),
    "Wild-only": (
        "Uses the main model's own call, but only in wild (high-volatility) markets, "
        "where an earlier audit saw the main model do best."),
    "Cross-exch": (
        "One learner per coin that sees every exchange at once, including how much "
        "their prices disagree, instead of one learner per exchange."),
    "Cross-all": (
        "Like Cross-exch, but one learner shared by every coin, so a lesson learned "
        "on one coin can help the others."),
    "Liquidity": (
        "Added live order-book and trading activity to the main model's price inputs, "
        "learned live. Retired Nov 11: far behind the main model and overconfident."),
    "Order-flow 1": "Price inputs plus Hyperliquid order flow and funding (retired).",
    "Order-flow 2": "A variant of Order-flow 1 (retired for overconfidence).",
    "Order-flow 3": (
        "Order flow and derivatives inputs, standardized and calibrated, the "
        "successor to the retired order-flow models."),
    "Calibrated": (
        "The main model's call pulled toward the recent up-rate, by an amount learned "
        "only from results already graded. Tests whether the main model is "
        "overconfident."),
    "6-year": (
        "A tree model trained offline on about six years of exchange history, checked "
        "against held-out data before it may run."),
    "Archive": "A model trained offline on RivaL's own verified archive of past candles.",
    "Boosted": "A gradient-boosted tree model trained offline on past candles.",
    "VolCal": (
        "One calibrated model per coin, trained offline on past candles with "
        "volatility, compression, volume-surprise, and confirmed-reversal "
        "features. Shadow-only until it earns promotion."),
    "XMarket": (
        "A separate calibrated model per coin and timeframe. Tests whether native "
        "15-minute Polymarket/Kalshi prices and exchange disagreement help predict "
        "perpetual direction. Missing inputs abstain; all calls remain shadow research."),
    "History": "A model admitted after passing a test on past data, now tested live.",
    "Naive Bayes": "A simple statistical model trained live on past results.",
    "Nearest-21": "Finds the 21 most similar past moments and predicts what they did.",
}


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _families(store, asset: str, now: datetime) -> dict[str, dict[int, list]]:
    """name -> horizon -> [(venue, contract, family, info)], best version per name."""
    since = (now - FRESH).isoformat()
    rows = optional_rows(
        store, """SELECT venue,contract,horizon_minutes,evidence_json
                  FROM rival_family_promotion WHERE asset=? AND evaluated_at>=?""",
        (asset, since), what="model page promotion read") or []
    retired = retired_families()
    found: dict[str, dict[int, dict[str, list]]] = {}
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict):
            continue
        for family, info in evidence.items():
            if (is_retired(family, retired) or not isinstance(info, dict)
                    or not isinstance(info.get("targets"), int)
                    or not isinstance(info.get("days"), int)):
                continue
            found.setdefault(short_name(family), {}).setdefault(
                int(row["horizon_minutes"]), {}).setdefault(family, []).append(
                    (str(row["venue"]), str(row["contract"]), family, info))
    out: dict[str, dict[int, list]] = {}
    for name, horizons in found.items():
        for horizon, versions in horizons.items():
            best = max(versions.values(), key=lambda scopes: (
                max(i["days"] for *_, i in scopes), max(i["targets"] for *_, i in scopes)))
            out.setdefault(name, {})[horizon] = best
    return out


def trial_models(store, asset: str, now: datetime | None = None) -> list[str]:
    """Every model on trial for this coin, longest-running first (one point read)."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    found = _families(store, asset, stamp)
    return sorted(found, key=lambda name: (
        -max(max(i["days"] for *_, i in scopes) for scopes in found[name].values()), name))


def _ledger(store, *, asset: str, horizon: int, family: str, since: str,
            until: str) -> list | None:
    """Graded calls grouped by exchange, confidence band and condition.

    None when the read failed (not "no calls"): the page says so.
    """
    from parlay_bot.perpetuals.forward_audit import MODEL

    return optional_rows(store,
        """SELECT s.venue,
                  CASE WHEN ABS(s.probability_up-0.5)<0.05 THEN 0
                       WHEN ABS(s.probability_up-0.5)<0.10 THEN 1
                       WHEN ABS(s.probability_up-0.5)<0.15 THEN 2 ELSE 3 END AS band,
                  COALESCE(r.regime,'unknown') AS regime,
                  COUNT(*) AS n,
                  SUM(CASE WHEN s.probability_up>0.5 THEN s.probability_up
                           ELSE 1-s.probability_up END) AS said,
                  SUM(CASE WHEN (s.probability_up>0.5 AND s.outcome_up=1)
                             OR (s.probability_up<0.5 AND s.outcome_up=0)
                           THEN 1 ELSE 0 END) AS right_calls,
                  SUM(CASE WHEN (s.incumbent_up>0.5 AND s.outcome_up=1)
                             OR (s.incumbent_up<0.5 AND s.outcome_up=0)
                           THEN 1 ELSE 0 END) AS main_right,
                  SUM(s.model_brier) AS model_sum,
                  SUM(s.incumbent_brier) AS main_sum
           FROM rival_model_shadow_forecasts s
           LEFT JOIN perp_forward_regimes r
             ON r.venue=s.venue AND r.contract=s.contract AND r.quote=s.quote
            AND r.horizon_minutes=s.horizon_minutes AND r.model_version=?
            AND r.reference_opened_at=s.reference_opened_at
           WHERE s.asset=? AND s.horizon_minutes=? AND s.family=?
             AND s.issued_at>=? AND s.issued_at<=?
             AND s.settled_at IS NOT NULL AND s.outcome_up IN (0,1)
             AND s.probability_up BETWEEN 0 AND 1 AND s.probability_up<>0.5
             AND s.model_brier BETWEEN 0 AND 1 AND s.incumbent_brier BETWEEN 0 AND 1
           GROUP BY 1,2,3""",
        (MODEL, asset, horizon, family, since, until), what="model page ledger read")


def _empty() -> dict:
    return {"n": 0, "said": 0.0, "right": 0, "main_right": 0, "model_sum": 0.0,
            "main_sum": 0.0}


def _add(into: dict, row) -> None:
    into["n"] += int(row["n"])
    into["said"] += float(row["said"] or 0)
    into["right"] += int(row["right_calls"] or 0)
    into["main_right"] += int(row["main_right"] or 0)
    into["model_sum"] += float(row["model_sum"] or 0)
    into["main_sum"] += float(row["main_sum"] or 0)


def model_report(store, asset: str, name: str, now: datetime | None = None) -> dict | None:
    """Everything the model page shows, or None if this model is not on trial here.

    One promotion read, one stop read, and one grouped ledger read per
    timeframe (14 days, on the (asset,horizon,family,issued_at) index).
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    found = _families(store, asset, stamp).get(name)
    if not found:
        return None
    since = (stamp - timedelta(days=LEDGER_DAYS)).isoformat()
    horizons: dict[int, dict] = {}
    # Every read on its own connection (see storage.optional_reads): one
    # slow ledger read must not turn the other timeframes into "no calls".
    stops = {(str(r["venue"]), str(r["contract"]), int(r["horizon_minutes"]),
              str(r["family"]))
             for r in optional_rows(
                 store, "SELECT venue,contract,horizon_minutes,family "
                        "FROM rival_family_early_stop",
                 what="model page early-stop read") or []}
    for horizon in HORIZONS:
        scopes = found.get(horizon)
        if not scopes:
            continue
        family = scopes[0][2]
        weighted = weight = 0.0
        exchanges = []
        fees: dict[str, list[float]] = {}
        for venue, contract, _family, info in scopes:
            gain, n = _finite(info.get("forward_gain")), int(info["targets"])
            if gain is not None and n:
                weighted += gain * n
                weight += n
            cut = (venue, contract, horizon, family) in stops
            exchanges.append({"venue": venue, "results": n, "gain": gain,
                              "days": int(info["days"]), "cut": cut,
                              "passed": bool(info.get("qualified"))})
            for scenario, proof in (info.get("cost_proof") or {}).items():
                bps = _finite((proof or {}).get("paired_gain_bps"))
                windows = int((proof or {}).get("windows") or 0)
                if bps is not None and windows:
                    part = fees.setdefault(str(scenario), [0.0, 0])
                    part[0] += bps * windows
                    part[1] += windows
        rows = _ledger(store, asset=asset, horizon=horizon, family=family,
                       since=since, until=stamp.isoformat())
        ledger_ok = rows is not None
        rows = rows or []
        overall, bands = _empty(), [_empty() for _ in BANDS]
        conditions: dict[str, dict] = {}
        for row in rows:
            _add(overall, row)
            _add(bands[int(row["band"])], row)
            _add(conditions.setdefault(str(row["regime"]), _empty()), row)
        horizons[horizon] = {
            "family": family,
            "days": max(e["days"] for e in exchanges),
            "results": max(e["results"] for e in exchanges),
            "gain": weighted / weight if weight else None,
            "ahead": sum((e["gain"] or 0) > LEVEL_WITHIN for e in exchanges),
            "exchanges": sorted(exchanges, key=lambda e: -e["results"]),
            "cut": sum(e["cut"] for e in exchanges),
            "passed": any(e["passed"] for e in exchanges),
            "fees": {k: v[0] / v[1] for k, v in fees.items() if v[1]},
            "fee_windows": {k: v[1] for k, v in fees.items()},
            "ledger": overall, "ledger_ok": ledger_ok,
            "bands": bands, "conditions": conditions,
        }
    return {"asset": asset, "name": name, "horizons": horizons, "ledger_days": LEDGER_DAYS}


# --- page -------------------------------------------------------------------

def _pts(gain: float | None) -> str:
    if gain is None:
        return "—"
    points = gain * 1000
    return f"{points:+.1f}" if abs(points) < 100 else f"{points:+.0f}"[:5]


def _rate(right: int, n: int) -> str:
    if n < TOO_FEW:
        return "few"
    rate = right / n
    return "100%" if rate >= .9995 else f"{rate:.1%}"   # at most 5 characters


def _status(h: dict) -> str:
    if h["passed"]:
        return "PASSED"
    if h["exchanges"] and h["cut"] >= len(h["exchanges"]):
        return "cut"
    if h["results"] < TOO_FEW or h["gain"] is None:
        return "early"
    return ("ahead" if h["gain"] > LEVEL_WITHIN
            else "behind" if h["gain"] < -LEVEL_WITHIN else "level")


def _lead(report: dict) -> tuple[int, dict]:
    """The timeframe it has the most evidence on."""
    return max(report["horizons"].items(), key=lambda kv: (kv[1]["days"], kv[1]["results"]))


def _standing(report: dict) -> str:
    horizon, h = _lead(report)
    status = _status(h)
    where = (f"ahead of the main model on {h['ahead']} of {len(h['exchanges'])} exchanges"
             if h["gain"] is not None else "no paired score yet")
    if status == "PASSED":
        verdict = "✅ **Passed every test** and can replace the main model here."
    elif status == "cut":
        verdict = "🛑 **Cut for losing** on every exchange it ran on."
    else:
        verdict = {"ahead": "🟢 **Ahead so far**", "level": "⚪ **About level**",
                   "behind": "🔴 **Behind the main model**",
                   "early": "⏳ **Too early to judge**"}[status] + ", not proven."
    return (f"{verdict}\n{horizon}-min: day {h['days']}/{MIN_DAYS} · "
            f"{h['results']:,}/{MIN_RESULTS} results · {where}")


def _timeframes(report: dict) -> str:
    # 34 characters: one line per timeframe on a phone.
    lines = [f"{'TF':<4} {'Right':>5} {'Main':>5} {'Edge':>5} {'Res':>5} {'Day':>5}"]
    for horizon in HORIZONS:
        h = report["horizons"].get(horizon)
        if h is None:
            lines.append(f"{horizon:>3}m {'—':>5} {'—':>5} {'—':>5} {'0':>5} {'—':>5}")
            continue
        led = h["ledger"]
        results = f"{h['results']:,}" if h["results"] < 10_000 else f"{h['results'] // 1000}k"
        right, main = ((_rate(led["right"], led["n"]), _rate(led["main_right"], led["n"]))
                       if h.get("ledger_ok", True) else ("?", "?"))
        lines.append(f"{horizon:>3}m {right:>5} "
                     f"{main:>5} {_pts(h['gain']):>5} "
                     f"{results:>5} {min(h['days'], 99):>2}/{MIN_DAYS}")
    return ("```\n" + "\n".join(lines) + "\n```\n"
            f"-# Right = its calls that came true, Main = the main model on the same "
            f"candles (last {report['ledger_days']} days). Edge = less error than the main "
            "model, in points. ? = couldn't read right now, try Refresh.")


def _exchanges(report: dict) -> tuple[str, str]:
    horizon, h = _lead(report)
    lines = [f"{'Exchange':<12} {'Res':>5} {'Edge':>5} Now"]
    for e in h["exchanges"][:12]:
        now = ("PASSED" if e["passed"] else "cut" if e["cut"]
               else "early" if e["results"] < TOO_FEW or e["gain"] is None
               else "ahead" if e["gain"] > LEVEL_WITHIN
               else "behind" if e["gain"] < -LEVEL_WITHIN else "level")
        lines.append(f"{e['venue'][:12]:<12} {e['results']:>5,} {_pts(e['gain']):>5} {now}")
    return f"🏦 By exchange ({horizon}-min)", "```\n" + "\n".join(lines) + "\n```"


def _confidence(report: dict) -> str:
    _, h = _lead(report)
    judged = [b for b in h["bands"] if b["n"] >= TOO_FEW]
    if not judged:
        return "Too few graded calls to check its confidence yet."
    lines = [f"{'It said':<8} {'Came true':>9} {'Main':>6} {'Calls':>6}"]
    for (low, high), band in zip(BANDS, h["bands"]):
        label = f"{low:.0%}–{high:.0%}" if high < 1 else f"{low:.0%}+"
        enough = band["n"] >= TOO_FEW
        true = f"{band['right'] / band['n']:.0%}" if enough else "too few"
        main = f"{band['main_right'] / band['n']:.0%}" if enough else "—"
        lines.append(f"{label:<8} {true:>9} {main:>6} {band['n']:>6,}")
    calls = sum(b["n"] for b in judged)
    said = sum(b["said"] for b in judged) / calls
    happened = sum(b["right"] for b in judged) / calls
    noise = 2 * math.sqrt(max(happened * (1 - happened), 1e-9) / calls)
    if abs(happened - said) <= noise:
        verdict = f"✅ **Honest:** says {said:.0%} on average, right {happened:.0%}."
    elif happened < said:
        verdict = (f"⚠️ **Overconfident:** says {said:.0%} on average, right only "
                   f"{happened:.0%}.")
    else:
        verdict = f"**Underconfident:** says {said:.0%} on average, right {happened:.0%}."
    return (verdict + "\n```\n" + "\n".join(lines) + "\n```\n"
            "-# Main = how often the main model was right on those same calls.")


def _conditions(report: dict) -> str | None:
    _, h = _lead(report)
    rows = [(k, h["conditions"][k]) for k in CONDITIONS if k in h["conditions"]]
    if not rows:
        return None
    lines = [f"{'Market':<9} {'Right':>6} {'Main':>6} {'Edge':>5} {'Calls':>6}"]
    for key, c in rows:
        enough = c["n"] >= TOO_FEW
        edge = (c["main_sum"] - c["model_sum"]) / c["n"] if enough else None
        lines.append(f"{key.title():<9} {_rate(c['right'], c['n']):>6} "
                     f"{_rate(c['main_right'], c['n']):>6} {_pts(edge):>5} {c['n']:>6,}")
    return "```\n" + "\n".join(lines) + "\n```\n-# A lead in one condition is a clue, not proof."


_PLAIN_CONDITION = {"quiet": "calm", "normal": "normal", "turbulent": "wild"}


def why(report: dict) -> str | None:
    """Why this model is ahead of or behind the main model, from its own results.

    Same graded calls as the tables above (last LEDGER_DAYS days): which
    market condition its edge (or loss) comes from, whether its confident
    calls beat the main model's on the same targets, and its best timeframe.
    Only parts with at least TOO_FEW calls are used; nothing is guessed.
    """
    horizon, h = _lead(report)
    led = h["ledger"]
    if led["n"] < TOO_FEW or not h.get("ledger_ok", True):
        return None
    total = (led["main_sum"] - led["model_sum"]) / led["n"]
    direction = ("ahead of" if total > LEVEL_WITHIN else "behind" if total < -LEVEL_WITHIN
                 else "level with")
    lines = [(f"On its {horizon}-min calls it is **{direction}** the main model "
              f"({_pts(total)} points over {led['n']:,} graded calls).")]
    parts = []
    for key in CONDITIONS:
        c = h["conditions"].get(key)
        if c and c["n"] >= TOO_FEW:
            parts.append((key, (c["main_sum"] - c["model_sum"]) / c["n"], c["n"],
                          c["main_sum"] - c["model_sum"]))
    if parts:
        # The condition that moves the total most, in the total's direction.
        lead = (max(parts, key=lambda p: p[3]) if total >= 0
                else min(parts, key=lambda p: p[3]))
        others = [p for p in parts if p[0] != lead[0]]
        line = (f"Most of that comes from **{_PLAIN_CONDITION[lead[0]]} markets** "
                f"({_pts(lead[1])} on {lead[2]:,} calls)")
        if others:
            # Name where it does worst when it is ahead (and best when it is
            # behind): that is where its weakness (or hope) is.
            against = [p for p in others if (p[1] < 0) == (total >= 0)]
            other = (min(against, key=lambda p: p[1]) if total >= 0 and against
                     else max(against, key=lambda p: p[1]) if against
                     else max(others, key=lambda p: p[2]))
            word = ("but it loses in" if total >= 0 and other[1] < 0
                    else "but it wins in" if total < 0 and other[1] > 0 else "in")
            line += (f"; {word} {_PLAIN_CONDITION[other[0]]} markets "
                     f"({_pts(other[1])} on {other[2]:,} calls)")
        lines.append(line + ".")
    sure = [b for b in h["bands"][2:] if b["n"] >= TOO_FEW]
    if sure:
        n = sum(b["n"] for b in sure)
        mine = sum(b["right"] for b in sure) / n
        main = sum(b["main_right"] for b in sure) / n
        verb = ("beat" if mine > main + 0.01 else "trailed" if mine < main - 0.01
                else "matched")
        lines.append(f"When it is confident (60%+), its calls came true {mine:.0%} vs the "
                     f"main model's {main:.0%} on the same targets ({n:,} calls): it {verb} it.")
    gains = {hz: x["gain"] for hz, x in report["horizons"].items()
             if x["gain"] is not None and x["results"] >= TOO_FEW}
    if len(gains) > 1:
        best = max(gains, key=gains.get)
        lines.append(f"Best timeframe so far: {best}-min ({_pts(gains[best])} points).")
    lines.append(f"-# Last {report['ledger_days']} days of graded calls. A pattern here is a "
                 "lead to watch, not proof.")
    return "\n".join(lines)


def _checklist(report: dict) -> str:
    _, h = _lead(report)
    severe = h["fees"].get("severe")
    def tick(ok: bool) -> str:
        return "✅" if ok else "⬜"
    lines = [
        f"{tick(h['days'] >= MIN_DAYS)} {MIN_DAYS} days of live results (has {h['days']})",
        (f"{tick(h['results'] >= MIN_RESULTS)} {MIN_RESULTS} independent results "
         f"(has {h['results']:,})"),
        (f"{tick((h['gain'] or 0) > 0)} less error than the main model "
         f"({_pts(h['gain'])} points)"),
        f"{tick(severe is not None and severe > 0)} ahead after heavy trading costs "
        + (f"({severe:+.1f} bps per trade over {h['fee_windows']['severe']:,} windows)"
           if severe is not None else "(not measured yet)"),
        ("🛑 cut for losing on " + f"{h['cut']} of {len(h['exchanges'])} exchanges"
         if h["cut"] else "✅ never cut for losing"),
    ]
    conservative = h["fees"].get("conservative")
    if conservative is not None:
        lines.append(f"-# With normal costs: {conservative:+.1f} bps per trade vs the main model.")
    return "\n".join(lines)


def model_embed(report: dict | None, *, asset: str, name: str) -> discord.Embed:
    if report is None or not report["horizons"]:
        return discord.Embed(
            title=f"🧪 {name} · {asset}",
            description="This model has no recent trial results here. It may have been "
                        "retired, or the hourly check hasn't scored it yet.",
            color=discord.Color.light_grey())
    how = HOW_IT_WORKS.get(name)
    if how is None and name.startswith("Lab "):
        from parlay_bot.perpetuals.model_lab import describe_family

        family = next((h["family"] for h in report["horizons"].values()), "")
        found = describe_family(family)
        how = ("A design RivaL's Model Lab picked itself. " + found) if found else None
    embed = discord.Embed(
        title=f"🧪 {name} · {asset} · model detail",
        description=(f"{how or 'A test model running beside the main model.'}"
                     f"\n\n{_standing(report)}"),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="📏 Accuracy by timeframe", value=_timeframes(report), inline=False)
    title, table = _exchanges(report)
    embed.add_field(name=title, value=table, inline=False)
    embed.add_field(name="🎚️ Is its confidence honest?", value=_confidence(report),
                    inline=False)
    reason = why(report)
    if reason:
        embed.add_field(name="💡 Why it's ahead or behind", value=reason, inline=False)
    conditions = _conditions(report)
    if conditions:
        embed.add_field(name="🌦️ Calm vs wild markets", value=conditions, inline=False)
    embed.add_field(name="🏁 What it needs to replace the main model",
                    value=_checklist(report), inline=False)
    embed.set_footer(text="Research only. No real trades. · A test model never trades "
                          "and can only take over by passing every check above.")
    return embed
