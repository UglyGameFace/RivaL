"""Mobile-first RivaL opportunity screens based on actual persisted records.

Never treats descriptive candles, history imports, or unqualified model
probabilities as approved profitable trades or executable market prices.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.presentation import display_book
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS as RESEARCH_ASSETS
from parlay_bot.perpetuals.markets import HORIZONS
from parlay_bot.perpetuals.readiness import _source_identity

ASSETS = RESEARCH_ASSETS


def _utc(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return instant.astimezone(UTC)


def _cash(price: float) -> str:
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:,.4f}"
    return f"${price:.7f}"


def recent_watches(
    market: PerpetualMarketStore, *, asset: str,
    now: datetime | None = None, limit: int = 3,
    dedupe_horizons: bool = True,
) -> list[dict]:
    """Only genuinely issued, still pending and fresh-venue forecasts."""
    if asset not in ASSETS or not 1 <= limit <= 40:
        raise ValueError("unsupported opportunity request")
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("current time must be aware")
    instant = current.astimezone(UTC)
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    with market.store.connect() as connection:
        rows = connection.execute(
            """SELECT venue,contract,quote,asset,horizon_minutes,
                      model_version,issued_at,target_opened_at,
                      reference_close,probability_up,training_evaluated
               FROM perp_forward_audit
               WHERE asset=? AND settled_at IS NULL
                 AND issued_at<=? AND issued_at>=?
               ORDER BY issued_at DESC LIMIT 40""",
            (asset, instant.isoformat(),
             (instant - timedelta(minutes=20)).isoformat()),
        ).fetchall()
    feeds = {
        (str(x["venue"]), str(x["asset"])): x
        for x in market.feed_health()
    }
    accepted: list[dict] = []
    for row in rows:
        try:
            issued = _utc(str(row["issued_at"]))
            target = _utc(str(row["target_opened_at"]))
            horizon = int(row["horizon_minutes"])
            probability = float(row["probability_up"])
            venue = str(row["venue"])
            if (
                horizon not in HORIZONS
                or not 0 < probability < 1
                or not math.isfinite(probability)
                # Outcomes always settle from the target FIVE-minute candle. The
                # horizon selects how far ahead its opening is, not its length.
                or instant >= target + timedelta(minutes=5)
                or _source_identity(venue, asset) != (
                    str(row["contract"]), str(row["quote"])
                )
            ):
                continue
            health = feeds.get((venue, asset))
            if health is None or health.get("status") != "ok":
                continue
            last_poll = _utc(str(health["last_success_at"]))
            if not timedelta(0) <= instant - last_poll <= timedelta(minutes=10):
                continue
            candles = market.recent_candles(
                venue=venue, contract=str(row["contract"]),
                quote=str(row["quote"]), interval_minutes=5,
                count=1, as_of=instant,
            )
            if not candles:
                continue
            candle = candles[-1]
            observed = _utc(str(candle["first_observed_at"]))
            if not timedelta(0) <= instant - observed <= timedelta(minutes=10):
                continue
            price = float(candle["close_price"])
            if not math.isfinite(price) or price <= 0:
                continue
            accepted.append({
                "asset": asset, "venue": venue,
                "horizon_minutes": horizon,
                "issued": issued, "target": target, "price": price,
                "price_observed_at": observed,
                "model_version": str(row["model_version"]),
                "probability_up": probability,
                "direction": "UP" if probability > 0.5 else "DOWN",
                "direction_probability": max(probability, 1 - probability),
                "training_evaluated": int(row["training_evaluated"]),
            })
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    # Signal selection needs all genuine venue alternatives to match a
    # forecast to an actually reachable live order book. Legacy dashboards
    # still show one candidate per horizon for a compact private screen.
    if not dedupe_horizons:
        return accepted[:limit]
    # Keep newest (not necessarily strongest) watch per horizon.
    by_horizon: dict[int, dict] = {}
    for watch in accepted:
        by_horizon.setdefault(watch["horizon_minutes"], watch)
    return list(by_horizon.values())[:limit]


def recent_multi_watches(
    market: PerpetualMarketStore, *, asset: str,
    now: datetime | None = None, limit: int = 3,
    dedupe_horizons: bool = True,
) -> list[dict]:
    """Promoted fused-challenger forecasts, under the same freshness gates.

    A fused forecast is priced through its reference venue's live book,
    never another venue's quote, and only when that venue's feed and
    latest candle are fresh. The returned dicts match recent_watches
    plus engine provenance; fused rows carry no walk-forward training
    count, so training_evaluated is 0 and the contributor count is the
    provenance that matters.
    """
    from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL

    if asset not in ASSETS or not 1 <= limit <= 40:
        raise ValueError("unsupported opportunity request")
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("current time must be aware")
    instant = current.astimezone(UTC)
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    with market.store.connect() as connection:
        rows = connection.execute(
            """SELECT asset,horizon_minutes,target_opened_at,issued_at,
                      model_version,probability_up,reference_venue,
                      reference_contract,reference_quote,contributor_count
               FROM perp_multi_forward
               WHERE asset=? AND settled_at IS NULL
                 AND model_version=?
                 AND issued_at<=? AND issued_at>=?
               ORDER BY issued_at DESC LIMIT 40""",
            (asset, MULTI_MODEL, instant.isoformat(),
             (instant - timedelta(minutes=20)).isoformat()),
        ).fetchall()
    feeds = {
        (str(x["venue"]), str(x["asset"])): x
        for x in market.feed_health()
    }
    accepted: list[dict] = []
    for row in rows:
        try:
            issued = _utc(str(row["issued_at"]))
            target = _utc(str(row["target_opened_at"]))
            horizon = int(row["horizon_minutes"])
            probability = float(row["probability_up"])
            venue = str(row["reference_venue"])
            if (
                horizon not in HORIZONS
                or not 0 < probability < 1
                or not math.isfinite(probability)
                or instant >= target + timedelta(minutes=5)
                or _source_identity(venue, asset) != (
                    str(row["reference_contract"]), str(row["reference_quote"])
                )
            ):
                continue
            health = feeds.get((venue, asset))
            if health is None or health.get("status") != "ok":
                continue
            last_poll = _utc(str(health["last_success_at"]))
            if not timedelta(0) <= instant - last_poll <= timedelta(minutes=10):
                continue
            candles = market.recent_candles(
                venue=venue, contract=str(row["reference_contract"]),
                quote=str(row["reference_quote"]), interval_minutes=5,
                count=1, as_of=instant,
            )
            if not candles:
                continue
            candle = candles[-1]
            observed = _utc(str(candle["first_observed_at"]))
            if not timedelta(0) <= instant - observed <= timedelta(minutes=10):
                continue
            price = float(candle["close_price"])
            if not math.isfinite(price) or price <= 0:
                continue
            accepted.append({
                "asset": asset, "venue": venue,
                "horizon_minutes": horizon,
                "issued": issued, "target": target, "price": price,
                "price_observed_at": observed,
                "probability_up": probability,
                "direction": "UP" if probability > 0.5 else "DOWN",
                "direction_probability": max(probability, 1 - probability),
                "training_evaluated": 0,
                "engine": "multi",
                "model_version": str(row["model_version"]),
                "contributor_count": int(row["contributor_count"]),
            })
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    if not dedupe_horizons:
        return accepted[:limit]
    by_horizon: dict[int, dict] = {}
    for watch in accepted:
        by_horizon.setdefault(watch["horizon_minutes"], watch)
    return list(by_horizon.values())[:limit]


def crypto_desk_embed(
    market: PerpetualMarketStore | None, *, asset: str = "BTC",
    proof: bool = False, now: datetime | None = None,
) -> discord.Embed:
    if asset not in ASSETS:
        raise ValueError("unknown asset")
    stamp = now or datetime.now(UTC)
    embed = discord.Embed(
        title=f"RivaL • {asset} Opportunity Desk",
        description="**Decision: PASS on real-money entries** until a forward-tested, after-cost edge qualifies. Live model watches are not buy/sell orders.",
        color=discord.Color.orange(),
        timestamp=stamp,
    )
    if market is None:
        embed.add_field(
            name="⛔ Data unavailable",
            value="Live PostgreSQL collection is not connected. No entry or price inferred.",
            inline=False,
        )
        return embed
    try:
        watches = recent_watches(market, asset=asset, now=stamp)
    except Exception:  # noqa: BLE001 - hide backend errors from users
        # Do not leak DB credentials or provider responses into a Discord embed.
        embed.add_field(
            name="⛔ Cannot verify live data",
            value="Forecast ledger or venue prices unavailable. PASS until restored.",
            inline=False,
        )
        return embed
    if not watches:
        embed.add_field(
            name="⏳ No qualified live watch",
            value="No *fresh, unexpired* pre-outcome prediction from an actively collecting exchange. PASS. Collection and learning continue automatically.",
            inline=False,
        )
    for watch in watches:
        embed.add_field(
            name=f"👀 {watch['direction']} watch • {watch['horizon_minutes']}m • {watch['venue']}",
            value=(
                f"Last completed candle: **{_cash(watch['price'])}** "
                f"({discord.utils.format_dt(watch['price_observed_at'], style='R')})\n"
                f"Raw model probability for {watch['direction']}: **{watch['direction_probability']:.1%}** "
                "(not profit-calibrated)\n"
                f"Issued {discord.utils.format_dt(watch['issued'], style='R')} · "
                f"Target {discord.utils.format_dt(watch['target'], style='R')}\n"
                "**Action: WATCH, not an approved entry.**"
            ),
            inline=False,
        )
    if proof:
        try:
            report = ForwardAccuracyMonitor(market).summary(asset=asset, now=stamp)
            for horizon in (5, 15):
                info = report["last_7d"][horizon]
                decided = info["settled"]
                edge = info["model_beats_baseline"]
                verdict = ("Beating baseline" if edge is True
                           else "Not beating baseline" if edge is False
                           else "Not enough independent proof")
                embed.add_field(
                    name=f"Past seven days • {horizon}m",
                    value=(
                        f"Settled predictions: **{decided}** · "
                        f"Hit rate: **{info['hit_rate']:.1%}** "
                        if info["hit_rate"] is not None else
                        f"Settled predictions: **{decided}** · No reliable hit rate"
                    ) + f"\n**{verdict}**. Overlapping events and costs not yet cleared.",
                    inline=False,
                )
        except Exception:  # noqa: BLE001 - safe failure on unavailable DB
            embed.add_field(name="Forward proof", value="Unable to verify scores.", inline=False)
    embed.set_footer(
        text="Read-only • Prices are last completed candles, not executable quotes • No automatic orders"
    )
    return embed


def sportsbook_desk_embed(
    builder: ParlayBuilder, *, user_id: str,
) -> discord.Embed:
    embed = discord.Embed(title="RivaL • Sportsbook Opportunity",
                          color=discord.Color.green())
    try:
        slip = builder.build_for_user(
            platform="discord", user_id=user_id, leg_count=1, risk=RiskMode.BALANCED,
        )
    except ParlayBuildError as exc:
        embed.description = "**Decision: PASS**\n" + str(exc)
        embed.set_footer(text="Live prices or verified sportsbook availability required.")
        return embed
    leg = slip.legs[0]
    line = f" · Line **{leg.line_value:g}**" if leg.line_value is not None else ""
    embed.description = (
        f"**{leg.event_name}**\n"
        f"**{leg.market_name} · {leg.outcome_name}**{line}\n"
        f"At **{display_book(slip.bookmaker)}** · "
        f"Odds **{leg.price_american or f'{leg.price_decimal:.2f}'}** "
        f"({leg.price_decimal:.2f} decimal)\n"
        f"Cross-book no-vig estimate **{leg.market_fair_probability:.1%}** · "
        f"Indicative ROI at that price **{leg.price_edge:+.1%}**\n"
        f"**Compare price before placing anything.** This is a screened quotation, "
        "not a verified profitable bet."
    )
    embed.add_field(
        name="Decision check",
        value=(
            f"Price screen: ≥2% indicative edge · "
            f"{leg.reference_books} independent reference price samples. "
            "No bet is placed for you."
        ), inline=False,
    )
    embed.set_footer(
        text="Jurisdiction-filtered • No-vig market consensus ≠ proven prediction • Prices can change"
    )
    return embed
