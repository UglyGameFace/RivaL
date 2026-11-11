"""Mobile-first RivaL opportunity screens based on actual persisted records.

Never treats descriptive candles, history imports, or unqualified model
probabilities as approved profitable trades or executable market prices.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.embed_text import fit_embed_field_value
from parlay_bot.discord_app.out_the_gate import (
    JUST_WATCH,
    below_line_label,
    lean_line,
    live_lean,
    otg_snapshot,
)
from parlay_bot.discord_app.presentation import display_book
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.parlays.models import ParlayBuildError, RiskMode
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS as RESEARCH_ASSETS
from parlay_bot.perpetuals.markets import HORIZONS
from parlay_bot.perpetuals.model_competition import CHALLENGER_PREFIXES, FAMILIES
from parlay_bot.perpetuals.readiness import _source_identity

_LOG = logging.getLogger(__name__)

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
    dedupe_horizons: bool = True, horizon_minutes: int | None = None,
) -> list[dict]:
    """Only genuinely issued, still pending and fresh-venue forecasts."""
    if (asset not in ASSETS or not 1 <= limit <= 40
            or (horizon_minutes is not None and horizon_minutes not in HORIZONS)):
        raise ValueError("unsupported opportunity request")
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("current time must be aware")
    instant = current.astimezone(UTC)
    # Display-only read: never initialize the forward monitor here. Schema
    # DDL belongs to process startup / the autonomous cycle; a display read
    # must not run it, and the no-trade withdrawal no longer lives in
    # initialize() at all (settlement path only).
    # A constant, bound-column predicate lets PostgreSQL use the existing
    # (asset,horizon_minutes,issued_at) index. Avoid an OR/NULL predicate,
    # which may force a scan across unrelated timeframes under load.
    horizon_filter = " AND horizon_minutes=?" if horizon_minutes is not None else ""
    params = (
        asset, *((horizon_minutes,) if horizon_minutes is not None else ()),
        instant.isoformat(), (instant - timedelta(minutes=20)).isoformat(),
    )
    with market.store.connect() as connection:
        rows = connection.execute(
            """SELECT venue,contract,quote,asset,horizon_minutes,
                      model_version,issued_at,target_opened_at,
                      reference_close,probability_up,training_evaluated
               FROM perp_forward_audit
               WHERE asset=? AND settled_at IS NULL"""
            + horizon_filter
            + """ AND issued_at<=? AND issued_at>=?
                ORDER BY issued_at DESC LIMIT 40""",
            params,
        ).fetchall()
    if not rows:
        return []
    # A paper-qualified family may replace only the corresponding read-only
    # prediction for its exact source + horizon. No old forecast is edited.
    # If the optional promotion ledger is unavailable, use the incumbent.
    promoted = {}
    family_rows = {}
    try:
        with market.store.connect() as connection:
            promotions = connection.execute(
                """SELECT venue,contract,quote,horizon_minutes,selected_family,
                          evaluated_at FROM rival_family_promotion
                   WHERE asset=? AND status='promoted_paper'""",
                (asset,),
            ).fetchall()
            for choice in promotions:
                family = str(choice["selected_family"])
                checked = _utc(str(choice["evaluated_at"]))
                if (family in FAMILIES or family.startswith(
                    CHALLENGER_PREFIXES
                )) and timedelta(0) <= instant-checked <= timedelta(hours=48):
                    key = (choice["venue"],choice["contract"],choice["quote"],
                           int(choice["horizon_minutes"]))
                    promoted[key] = family
            if promoted:
                candidates = connection.execute(
                    """SELECT venue,contract,quote,horizon_minutes,family,
                              reference_opened_at,issued_at,
                              target_opened_at,probability_up,training_count
                       FROM rival_model_shadow_forecasts
                       WHERE asset=? AND issued_at>=? AND issued_at<=?""",
                    (asset,(instant-timedelta(minutes=20)).isoformat(),
                     instant.isoformat()),
                ).fetchall()
                family_rows = {
                    (x["venue"],x["contract"],x["quote"],
                     int(x["horizon_minutes"]),x["reference_opened_at"],
                     x["issued_at"],x["family"]): x
                    for x in candidates
                }
        # Do not serve boosted/historical families simply because a promotion
        # row exists: re-check the current artifact in the same exact source.
        # Close the first database checkout before this separate registry read.
        promoter = FamilyPromotion(market.store)
        for scope, family in tuple(promoted.items()):
            venue, contract, quote, horizon = scope
            if promoter.selected(
                asset=asset, venue=venue, contract=contract, quote=quote,
                horizon=horizon, now=instant,
            ) != family:
                del promoted[scope]
    except Exception:  # noqa: BLE001 - optional family read fails to incumbent
        promoted = {}
        family_rows = {}
    feeds = {
        (str(x["venue"]), str(x["asset"])): x
        for x in market.feed_health()
    }
    # A 40-row candidate window often contains many forecasts for the
    # same source. Fetch its latest as-of candle once, not once per horizon.
    candles_by_source: dict[tuple[str, str, str], list[dict]] = {}
    accepted: list[dict] = []
    for row in rows:
        try:
            issued = _utc(str(row["issued_at"]))
            target = _utc(str(row["target_opened_at"]))
            horizon = int(row["horizon_minutes"])
            probability = float(row["probability_up"])
            selected_version = str(row["model_version"])
            selected_training = int(row["training_evaluated"])
            venue = str(row["venue"])
            scope = (venue,str(row["contract"]),str(row["quote"]),horizon)
            family = promoted.get(scope)
            if family:
                paired = family_rows.get(
                    (*scope,(target-timedelta(minutes=horizon)).isoformat(),
                     row["issued_at"],family),
                )
                if (paired is not None and
                        paired["target_opened_at"] == row["target_opened_at"]):
                    probability = float(paired["probability_up"])
                    selected_version = family
                    selected_training = int(paired["training_count"])
            if (
                horizon not in HORIZONS
                or (horizon_minutes is not None and horizon != horizon_minutes)
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
            source = (venue, str(row["contract"]), str(row["quote"]))
            if source not in candles_by_source:
                candles_by_source[source] = market.recent_candles(
                    venue=venue, contract=source[1], quote=source[2],
                    interval_minutes=5, count=1, as_of=instant,
                )
            candles = candles_by_source[source]
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
                "contract": str(row["contract"]), "quote": str(row["quote"]),
                "horizon_minutes": horizon,
                "issued": issued, "target": target, "price": price,
                "price_observed_at": observed,
                "model_version": selected_version,
                "probability_up": probability,
                "direction": "UP" if probability > 0.5 else "DOWN",
                "direction_probability": max(probability, 1 - probability),
                "training_evaluated": selected_training,
            })
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    # First serve through this reader: emit a one-time Discord receipt
    # (spec §2.7). Deduplicated by the receipts table; a receipt failure
    # never blocks serving.
    for watch in accepted:
        if watch["model_version"] != "flow_v3":
            continue
        try:
            from parlay_bot.perpetuals.flow_challenger_v3 import FlowChallengerV3
            from parlay_bot.perpetuals.promotion_receipts import (
                record_promotion_receipt,
            )
            with market.store.connect() as connection:
                promo = connection.execute(
                    """SELECT evaluated_at, evidence_json
                       FROM rival_family_promotion
                       WHERE asset=? AND venue=? AND contract=? AND quote=?
                        AND horizon_minutes=? AND status='promoted_paper'
                        AND selected_family='flow_v3'""",
                    (watch["asset"], watch["venue"], watch["contract"],
                     watch["quote"], watch["horizon_minutes"]),
                ).fetchone()
            if promo is None:
                continue
            evidence = json.loads(promo["evidence_json"])["flow_v3"]
            record_promotion_receipt(
                market.store, family="flow_v3", venue=watch["venue"],
                contract=watch["contract"], quote=watch["quote"],
                horizon=watch["horizon_minutes"], kind="first_serve",
                evidence={**evidence, "evaluated_at": promo["evaluated_at"]},
                calibration=FlowChallengerV3.calibration_status(
                    market.store, watch["asset"], watch["horizon_minutes"]),
                now=instant)
        except Exception:  # noqa: BLE001 - a receipt must never block serving
            _LOG.exception("RivaL flow_v3 first-serve receipt unavailable")
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
    dedupe_horizons: bool = True, horizon_minutes: int | None = None,
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

    if (asset not in ASSETS or not 1 <= limit <= 40
            or (horizon_minutes is not None and horizon_minutes not in HORIZONS)):
        raise ValueError("unsupported opportunity request")
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("current time must be aware")
    instant = current.astimezone(UTC)
    # Display-only read: no monitor initialize() here (see recent_watches).
    horizon_filter = " AND horizon_minutes=?" if horizon_minutes is not None else ""
    params = (
        asset, MULTI_MODEL,
        *((horizon_minutes,) if horizon_minutes is not None else ()),
        instant.isoformat(), (instant - timedelta(minutes=20)).isoformat(),
    )
    with market.store.connect() as connection:
        rows = connection.execute(
            """SELECT asset,horizon_minutes,target_opened_at,issued_at,
                      model_version,probability_up,reference_venue,
                      reference_contract,reference_quote,contributor_count
               FROM perp_multi_forward
               WHERE asset=? AND settled_at IS NULL
                 AND model_version=?"""
            + horizon_filter
            + """ AND issued_at<=? AND issued_at>=?
                ORDER BY issued_at DESC LIMIT 40""",
            params,
        ).fetchall()
    if not rows:
        return []
    feeds = {
        (str(x["venue"]), str(x["asset"])): x
        for x in market.feed_health()
    }
    # A 40-row candidate window often contains many forecasts for the
    # same source. Fetch its latest as-of candle once, not once per horizon.
    candles_by_source: dict[tuple[str, str, str], list[dict]] = {}
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
                or (horizon_minutes is not None and horizon != horizon_minutes)
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
            source = (venue, str(row["reference_contract"]),
                      str(row["reference_quote"]))
            if source not in candles_by_source:
                candles_by_source[source] = market.recent_candles(
                    venue=venue, contract=source[1], quote=source[2],
                    interval_minutes=5, count=1, as_of=instant,
                )
            candles = candles_by_source[source]
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


def polymarket_call_fields(store, *, asset: str, now: datetime) -> list[tuple[str, str]]:
    """The open Polymarket contracts' frozen first calls, with the measured record.

    Read-only. Shows only contracts still open whose first call was issued
    before now; never a guessed or recomputed call.
    """
    from parlay_bot.perpetuals.polymarket_learning import (
        MIN_DIRECTIONAL_MARGIN,
        MODEL,
        PolymarketOutcomeLearner,
    )

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT f.slug,f.issued_at,f.probability_up,f.market_baseline,"
            "v.horizon,v.expires_at FROM rival_polymarket_model_forward f "
            "JOIN rival_polymarket_observations v ON v.slug=f.slug "
            "WHERE v.asset=? AND f.model_version=? AND v.expires_at>? "
            "ORDER BY v.expires_at LIMIT 12",
            (asset, MODEL, now.isoformat()),
        ).fetchall()
    learner = PolymarketOutcomeLearner(store)
    fields, seen = [], set()
    for row in rows:
        try:
            horizon = int(row["horizon"])
            issued = datetime.fromisoformat(str(row["issued_at"]))
            closes = datetime.fromisoformat(str(row["expires_at"]))
            p, market = float(row["probability_up"]), float(row["market_baseline"])
        except (TypeError, ValueError, KeyError):
            continue
        if horizon in seen or not issued <= now < closes:
            continue
        seen.add(horizon)
        side = ("WAIT" if abs(p - .5) < MIN_DIRECTIONAL_MARGIN else "UP" if p > .5 else "DOWN")
        try:
            summary = learner.summary(asset=asset, horizon=horizon)
            n = int(summary.get("settled_forward_contracts") or 0)
            model, base = summary.get("forward_brier"), summary.get("market_baseline_brier")
            record = (f"Last 7 days, {n:,} settled calls: the model "
                      f"{'beat' if model < base else 'did not beat'} the market's own price "
                      f"(error {model:.4f} vs {base:.4f}, lower is better)."
                      if n and model is not None and base is not None
                      else "No settled calls to score yet.")
        except Exception:  # noqa: BLE001 - the record is context, never required
            record = "Track record unavailable right now."
        label = "1 hour" if horizon == 60 else f"{horizon} min"
        fields.append((
            horizon,
            f"🎰 Polymarket • {label} • call: {side}",
            (f"Frozen first call: **{side}** (model {p:.1%} UP; the market priced UP at "
            f"{market:.0%} at that moment). Closes {discord.utils.format_dt(closes, style='R')}. "
            f"[Open contract](https://polymarket.com/event/{row['slug']})\n"
            f"{record} **Not a proven edge.**"),
        ))
    return [(name, value) for _, name, value in sorted(fields)]


def crypto_desk_embed(
    market: PerpetualMarketStore | None, *, asset: str = "BTC",
    proof: bool = False, now: datetime | None = None,
) -> discord.Embed:
    if asset not in ASSETS:
        raise ValueError("unknown asset")
    stamp = now or datetime.now(UTC)
    embed = discord.Embed(
        title=f"RivaL • {asset} right now",
        description=("**Should you trade on this? No, not yet.** RivaL hasn't proven it "
                     "makes money after fees. These are its current guesses to watch, "
                     "not buy or sell advice."),
        color=discord.Color.orange(),
        timestamp=stamp,
    )
    if market is None:
        embed.add_field(
            name="⛔ Data unavailable",
            value="The live price database isn't connected, so nothing is shown.",
            inline=False,
        )
        return embed
    try:
        watches = recent_watches(market, asset=asset, now=stamp)
    except Exception:  # noqa: BLE001 - hide backend errors from users
        # Do not leak DB credentials or provider responses into a Discord embed.
        embed.add_field(
            name="⛔ Couldn't load live data",
            value="Predictions or prices couldn't be read right now. Try again shortly.",
            inline=False,
        )
        return embed
    # Polymarket first: it is where users actually place Up/Down bets.
    try:
        for name, value in polymarket_call_fields(market.store, asset=asset, now=stamp):
            embed.add_field(name=name, value=fit_embed_field_value(value), inline=False)
    except Exception:  # noqa: BLE001 - tables may not exist yet; exchange watch still shows
        logging.getLogger(__name__).info("Polymarket calls unavailable for Opportunities")
    if not watches:
        embed.add_field(
            name="⏳ Nothing to watch right now",
            value="There's no recent prediction that is still open. RivaL keeps collecting prices and learning in the background.",
            inline=False,
        )
    for watch in watches:
        # Scores below belong to the live model only; say so when a watch
        # comes from a different (unproven) model version.
        other = (f" • test model {str(watch.get('model_version'))[:28]} (no track record yet)"
                 if watch.get("model_version") not in (None, LIVE_MODEL) else "")
        # Out-the-gate call (immutable issuance snapshot) + live lean
        # (recomputed now). The OTG call is shown for every slot, including
        # sub-threshold leans (D1(a)); the WATCH verdict is unchanged.
        otg = otg_snapshot(watch, market=market)
        lean = live_lean(
            market, asset=asset, horizon=watch["horizon_minutes"],
            venue=watch["venue"], contract=watch.get("contract"),
            quote=watch.get("quote"), now=stamp,
            engine="live", model_version=watch.get("model_version"),
        )
        # Nov 11 audit: the card said the same chance three times ("How
        # sure it is", "Out the gate", "Live lean"). One chance, then a "now"
        # line only when the live lean has moved since the call was made.
        otg_lines = []
        made = otg.get("probability") if isinstance(otg, dict) else None
        # Compare as shown (whole percent), so the "Now" line never repeats
        # the same number.
        if lean is not None and (made is None or round(float(lean["probability"]) * 100)
                                 != round(float(made) * 100)):
            otg_lines.append(lean_line(lean, as_of_label="Now"))
        below = below_line_label(otg)
        if below is not None:
            otg_lines.append(below)
        from parlay_bot.discord_app.data_dashboard import VENUE_NAMES

        embed.add_field(
            name=fit_embed_field_value(
                f"👀 Leans {watch['direction']} • next {watch['horizon_minutes']} min • "
                f"{VENUE_NAMES.get(watch['venue'], watch['venue'])}{other}",
                budget=256,
            ),
            value=(
                f"Price at the last finished candle: **{_cash(watch['price'])}** "
                f"({discord.utils.format_dt(watch['price_observed_at'], style='R')})\n"
                f"Chance of {watch['direction']}: **{watch['direction_probability']:.1%}** "
                "(its own estimate; raw, uncalibrated — not proven accurate)\n"
                + "".join(line + "\n" for line in otg_lines)
                + f"Made {discord.utils.format_dt(watch['issued'], style='R')} · "
                f"Answer known {discord.utils.format_dt(watch['target'] + timedelta(minutes=5), style='R')}\n"
                f"**{JUST_WATCH}**"
            ),
            inline=False,
        )
    if proof:
        try:
            report = ForwardAccuracyMonitor(market).summary(asset=asset, now=stamp)
            for horizon in (5, 15, 60):
                info = report["last_7d"][horizon]
                decided = info["settled"]
                edge = info["model_beats_baseline"]
                verdict = ("Doing better than a simple guess" if edge is True
                           else "Not doing better than a simple guess" if edge is False
                           else "Too early to tell")
                period = "Last 7 days"
                try:
                    first = datetime.fromisoformat(str(info.get("first_issued")))
                    if first.tzinfo is not None and stamp - first < timedelta(days=6, hours=12):
                        period = f"Since {first:%b %d} (when this model started)"
                except (TypeError, ValueError):
                    pass
                embed.add_field(
                    name=f"{period} • {horizon}-min predictions • current model",
                    value=(
                        f"Scored: **{decided:,}** · "
                        f"Right: **{info['hit_rate']:.1%}** "
                        if info["hit_rate"] is not None else
                        f"Scored: **{decided:,}** · Not enough to give a % yet"
                    ) + f"\n**{verdict}**. Trading fees aren't counted here.",
                    inline=False,
                )
        except Exception:  # noqa: BLE001 - safe failure on unavailable DB
            embed.add_field(name="Track record", value="Couldn't load the scores right now.", inline=False)
    embed.set_footer(
        text="Look only • Prices are from the last finished candle, not live order prices • No trades are placed"
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
        embed.description = "**No pick right now (PASS)**\n" + str(exc)
        embed.set_footer(text="Needs live odds from a sportsbook in your state.")
        return embed
    leg = slip.legs[0]
    line = f" · Line **{leg.line_value:g}**" if leg.line_value is not None else ""
    embed.description = (
        f"**{leg.event_name}**\n"
        f"**{leg.market_name} · {leg.outcome_name}**{line}\n"
        f"At **{display_book(slip.bookmaker)}** · "
        f"Odds **{leg.price_american or f'{leg.price_decimal:.2f}'}** "
        f"({leg.price_decimal:.2f} decimal)\n"
        f"Sportsbooks' combined odds imply **{leg.market_fair_probability:.1%}** "
        f"(after removing their margin; not a measured win rate) · "
        f"Expected return at this price: **{leg.price_edge:+.1%}** (an estimate)\n"
        f"**Check the price yourself before betting.** This is a price that looks "
        "good on paper, not a proven winning bet."
    )
    embed.add_field(
        name="Why it was picked",
        value=(
            f"Its price looks at least 2% better than the average across "
            f"{leg.reference_books} other sportsbooks. No bet is placed for you."
        ), inline=False,
    )
    embed.set_footer(
        text="Only sportsbooks in your state • The odds' consensus isn't a guarantee • Prices can change"
    )
    return embed
