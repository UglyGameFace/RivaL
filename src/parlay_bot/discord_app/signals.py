"""One authoritative, market-matched live signal view for the Discord bot.

Research watches are not profitable trading instructions. An exchange quote
only informs the matching venue's prediction; blocked sources never borrow
another venue's bid/ask or an old candle masquerading as a live quote.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.opportunities import ASSETS, recent_watches
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import (
    BookQuote,
    PerpReferenceBook,
    PublicBestQuoteClient,
    QuoteUnavailable,
    VenueBookQuote,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger

_LOG = logging.getLogger(__name__)
HORIZONS = (5, 15)
MIN_REMAINING = timedelta(seconds=90)
WATCH_THRESHOLD = 0.56


def model_strength(probability: float) -> str:
    """Label raw model direction without calling it calibrated confidence."""
    if not math.isfinite(probability) or not 0 < probability < 1:
        return "invalid"
    score = max(probability, 1 - probability)
    if score < 0.53:
        return "very weak"
    if score < WATCH_THRESHOLD:
        return "weak"
    return "directional watch"


def _no_signal(asset: str, horizon: int, reason: str) -> discord.Embed:
    return discord.Embed(
        title=f"⏳ {asset} | {horizon}m • NO LIVE SIGNAL",
        description=reason + "\n\nNo trade is approved or placed.",
        color=discord.Color.orange(),
    )


async def _venue_book(
    quotes: PublicBestQuoteClient, *, asset: str, venue: str,
) -> BookQuote | PerpReferenceBook | VenueBookQuote:
    if venue == "binance":
        return await quotes.get(asset)
    if venue == "hyperliquid":
        return await quotes.get_reference(asset)
    if venue in ("okx", "bitget"):
        return await quotes.get_venue(asset, venue)
    raise QuoteUnavailable("No verified live order-book source for this venue.")


def _proof(
    market: PerpetualMarketStore, *, asset: str, horizon: int, now: datetime,
) -> dict:
    return ForwardAccuracyMonitor(market).summary(asset=asset, now=now)[
        "last_7d"
    ][horizon]


def _venue_proof(
    market: PerpetualMarketStore, *, asset: str, horizon: int, venue: str,
    now: datetime,
) -> dict:
    return ForwardAccuracyMonitor(market).venue_summary(
        asset=asset, venue=venue, horizon=horizon, now=now,
    )


async def signal_embed(
    market: PerpetualMarketStore | None,
    quotes: PublicBestQuoteClient,
    *, asset: str, horizon_minutes: int, now: datetime | None = None,
    stream_shadow: StreamShadowLedger | None = None,
) -> discord.Embed:
    """Refresh the selected market, its real book and measured forward results.

    At most one quote request per supported venue; no forecast or quote cache
    is used after its source-specific validity expires.
    """
    if asset not in ASSETS or horizon_minutes not in HORIZONS:
        raise ValueError("choose BTC, ETH or SOL and 5 or 15 minutes")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("live signal clock requires timezone")
    stamp = stamp.astimezone(UTC)
    if market is None:
        return _no_signal(asset, horizon_minutes, "Market collection is disconnected.")

    try:
        watches_started = time.monotonic()
        watches = await asyncio.to_thread(
            recent_watches, market, asset=asset, now=stamp,
            limit=40, dedupe_horizons=False,
        )
        marks = {"watches_ms": (time.monotonic() - watches_started) * 1000}
    except Exception:  # noqa: BLE001 - do not leak DB details into Discord
        _LOG.exception("RivaL signal ledger cannot be inspected")
        return _no_signal(
            asset, horizon_minutes,
            "Forecast history cannot be verified. No signal is issued.",
        )

    relevant = [
        watch for watch in watches
        if watch["horizon_minutes"] == horizon_minutes
    ]
    live = [
        watch for watch in relevant
        if watch["target"] + timedelta(minutes=5) - stamp >= MIN_REMAINING
    ]
    if not live:
        reason = (
            "The latest prediction is too close to expiration. "
            "RivaL will not present a nearly finished forecast as a new entry."
            if relevant else
            "No new forward prediction qualifies for this market yet. "
            "The background collector must first produce a genuine, "
            "unexpired forecast; historical data cannot substitute."
        )
        return _no_signal(asset, horizon_minutes, reason)

    # Choose a forecast with its OWN current market book. A different venue's
    # quote cannot be substituted just to display a made-up executable setup.
    # The venues are raced, not tried one after another: a slow first venue
    # used to consume the whole signal budget before the fallback was even
    # tried, which produced SIGNAL DELAYED on otherwise healthy markets.
    # Selection policy (deterministic, documented): among the venues with a
    # validated book, a venue is QUALIFIED only if its own forecast is a
    # directional watch AND its own venue-specific 7-day record beats the
    # baseline (50+ settled forecasts). The chosen venue is the first
    # qualified venue in priority order (Hyperliquid, Binance, OKX, Bitget);
    # venue priority breaks ties among qualified venues, it never overrides
    # qualification. When nothing qualifies, the fallback is the first
    # priority venue with a validated book, and its own verdict applies:
    # PASS when its venue trails the baseline, UNVERIFIED when its venue
    # record is unknown, LEAN when its forecast is too weak. A fallback is
    # never a WATCH. Other validated venues are listed, not hidden.
    prioritized = sorted(
        live,
        key=lambda row: (
            0 if row["venue"] == "hyperliquid" else
            1 if row["venue"] == "binance" else
            2 if row["venue"] == "okx" else
            3 if row["venue"] == "bitget" else 4,
            -row["issued"].timestamp(),
        ),
    )
    candidate_venues: list[str] = []
    for watch in prioritized:
        venue = watch["venue"]
        if (venue in ("hyperliquid", "binance", "okx", "bitget")
                and venue not in candidate_venues):
            candidate_venues.append(venue)

    async def _pick_books() -> dict[str, tuple[dict, BookQuote | PerpReferenceBook | VenueBookQuote]]:
        if not candidate_venues:
            return {}
        started = time.monotonic()
        watch_by_venue = {
            venue: next(w for w in prioritized if w["venue"] == venue)
            for venue in candidate_venues
        }
        outcomes = await asyncio.gather(
            *(_venue_book(quotes, asset=asset, venue=venue)
              for venue in candidate_venues),
            return_exceptions=True,
        )
        marks["book_ms"] = (time.monotonic() - started) * 1000
        validated: dict[str, tuple[dict, BookQuote | PerpReferenceBook | VenueBookQuote]] = {}
        for venue, outcome in zip(candidate_venues, outcomes, strict=True):
            if isinstance(outcome, Exception):
                continue
            try:
                outcome.validate(now=stamp if now is not None else datetime.now(UTC))
            except QuoteUnavailable:
                continue
            validated[venue] = (watch_by_venue[venue], outcome)
        return validated

    def _default_evidence() -> dict:
        return {"settled": 0, "hit_rate": None, "model_beats_baseline": None}

    async def _venue_proof_or_default(venue: str) -> dict:
        started = time.monotonic()
        try:
            return await asyncio.to_thread(
                _venue_proof, market, asset=asset, horizon=horizon_minutes,
                venue=venue, now=stamp,
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL signal venue evidence lookup unavailable")
            return _default_evidence()
        finally:
            marks[f"proof_{venue}_ms"] = (time.monotonic() - started) * 1000

    async def _proof_or_default() -> dict:
        started = time.monotonic()
        try:
            return await asyncio.to_thread(
                _proof, market, asset=asset, horizon=horizon_minutes, now=stamp,
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL signal forward-evidence lookup unavailable")
            return _default_evidence()
        finally:
            marks["proof_ms"] = (time.monotonic() - started) * 1000

    async def _shadow_or_none() -> dict | None:
        if stream_shadow is None:
            return None
        started = time.monotonic()
        try:
            return await asyncio.to_thread(
                stream_shadow.report, asset=asset,
                horizon_minutes=horizon_minutes, now=stamp,
            )
        except Exception:  # noqa: BLE001 - missing proof must not halt signals
            _LOG.exception("RivaL forward quote evidence unavailable")
            return None
        finally:
            marks["shadow_ms"] = (time.monotonic() - started) * 1000

    gathered = await asyncio.gather(
        _pick_books(),
        *(_venue_proof_or_default(venue) for venue in candidate_venues),
        _proof_or_default(), _shadow_or_none(),
        return_exceptions=True,
    )
    validated = gathered[0] if not isinstance(gathered[0], Exception) else {}
    venue_proofs: dict[str, dict] = {}
    for venue, outcome in zip(
        candidate_venues, gathered[1:1 + len(candidate_venues)], strict=True,
    ):
        venue_proofs[venue] = (
            outcome if not isinstance(outcome, Exception)
            else _default_evidence()
        )
    evidence = gathered[-2]
    if isinstance(evidence, Exception):  # pragma: no cover - helper already guards
        evidence = _default_evidence()
    shadow = gathered[-1] if not isinstance(gathered[-1], Exception) else None

    def _direction(probability: float) -> str:
        return "UP" if probability > 0.5 else (
            "DOWN" if probability < 0.5 else "FLAT"
        )

    qualified_venues = [
        venue for venue in candidate_venues
        if venue in validated
        and model_strength(float(validated[venue][0]["probability_up"]))
        == "directional watch"
        and venue_proofs[venue].get("model_beats_baseline") is True
        and _direction(float(validated[venue][0]["probability_up"])) != "FLAT"
    ]
    if qualified_venues:
        chosen_venue = qualified_venues[0]
        selection = "qualified"
    elif validated:
        chosen_venue = next(
            venue for venue in candidate_venues if venue in validated
        )
        selection = "fallback"
    else:
        chosen_venue = None
        selection = "none"
    if chosen_venue is not None:
        chosen, book = validated[chosen_venue]
        venue_evidence = venue_proofs[chosen_venue]
    else:
        chosen, book = prioritized[0], None
        venue_evidence = venue_proofs.get(chosen["venue"])
    probability = float(chosen["probability_up"])
    strength = model_strength(probability)
    direction = _direction(probability)
    venue_beats = (
        venue_evidence.get("model_beats_baseline")
        if venue_evidence is not None else None
    )
    is_watch = (
        selection == "qualified" and book is not None
        and strength == "directional watch" and direction != "FLAT"
    )
    if venue_beats is False:
        verdict = "⛔ PASS • MODEL UNDERPERFORMING"
        message = (
            f"Recent {chosen['venue']} forward scores do not beat a simple "
            "baseline."
        )
    elif book is None:
        verdict = "⏳ WAIT • MARKET PRICE UNAVAILABLE"
        message = (
            "A forecast exists, but no matching current exchange order book "
            "could be verified. No other exchange price was substituted."
        )
    elif is_watch:
        verdict = f"👀 {direction} WATCH • RESEARCH ONLY"
        message = (
            "A genuine forward model leans "
            + ("higher" if direction == "UP" else "lower")
            + ". This is an observed directional watch, not a validated "
              "profitable entry or a trade recommendation."
        )
    elif (strength == "directional watch" and venue_beats is None
            and direction != "FLAT"):
        verdict = f"🔎 {direction} RESEARCH • UNVERIFIED"
        message = (
            f"The {chosen['venue']} track record could not be verified: "
            "too few settled forecasts for this venue, or the lookup "
            "failed. This raw direction is shown as research only, never "
            "as an evidence-qualified watch."
        )
    else:
        verdict = f"🟠 {direction} LEAN • NO SIGNAL" if direction != "FLAT" else (
            "⏳ WAIT • NO DIRECTION"
        )
        message = (
            f"Raw {max(probability, 1 - probability):.1%} model probability is "
            "too close to a coin flip for an actionable signal."
        )

    embed = discord.Embed(
        title=f"{asset} | {horizon_minutes}m • {verdict}",
        description=message,
        color=discord.Color.blurple() if is_watch else discord.Color.orange(),
    )
    embed.add_field(
        name="Model and expiry",
        value=(
            f"**{direction}** • raw {max(probability, 1 - probability):.1%} "
            f"({strength})\n"
            f"Source: **{chosen['venue']}** • "
            f"Issued {discord.utils.format_dt(chosen['issued'], style='R')}\n"
            f"Expires {discord.utils.format_dt(chosen['target'] + timedelta(minutes=5), style='R')}"
        ),
        inline=False,
    )
    if book is not None:
        quote_labels = {
            "binance": "Binance USD-M USDT contract",
            "hyperliquid": "Hyperliquid USD-perp reference",
            "okx": "OKX USDT-SWAP contract",
            "bitget": "Bitget USDT-FUTURES contract",
        }
        quote_label = quote_labels.get(
            chosen["venue"], f"{chosen['venue']} contract",
        )
        embed.add_field(
            name="Fresh matching-market bid / ask",
            value=(
                f"**{quote_label}**\n"
                f"Bid **${book.bid:,.2f}** · Ask **${book.ask:,.2f}**\n"
                f"Observed {discord.utils.format_dt(book.exchange_at, style='R')} "
                "• Not a guaranteed fill"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Matching live price",
            value=(
                "**Unavailable.** A previous completed candle is not a "
                "valid real-time entry price."
            ),
            inline=False,
        )
    track_lines: list[str] = []
    if venue_evidence is not None:
        venue_hit = venue_evidence.get("hit_rate")
        venue_count = int(venue_evidence["settled"])
        track_lines.append(
            f"{venue_count} settled {chosen['venue']} forecasts"
            + (f" • Direction correct {venue_hit:.1%}"
               if venue_hit is not None else " • Not enough scored outcomes")
            + (" • Beats baseline" if venue_beats is True else
               " • Trails baseline" if venue_beats is False else
               " • Unverified (needs 50 settled)")
        )
    else:
        track_lines.append(
            f"{chosen['venue']} venue record not evaluated "
            "(no matching book)."
        )
    global_hit = evidence.get("hit_rate")
    global_count = int(evidence["settled"])
    track_lines.append(
        f"All venues: {global_count} settled"
        + (f" • Direction correct {global_hit:.1%}"
           if global_hit is not None else " • Not enough scored outcomes")
    )
    track_lines.append(
        "Cross-exchange outcomes are correlated. Neither an "
        "independent edge nor after-cost profitability is established."
    )
    embed.add_field(
        name="Model track record (last 7 days)",
        value="\n".join(track_lines),
        inline=False,
    )
    alternatives: list[str] = []
    for venue in candidate_venues:
        if venue == chosen_venue or venue not in validated:
            continue
        alt_watch, _ = validated[venue]
        alt_prob = float(alt_watch["probability_up"])
        alt_beats = venue_proofs[venue].get("model_beats_baseline")
        alt_state = (
            "qualified"
            if alt_beats is True
            and model_strength(alt_prob) == "directional watch"
            else "unqualified" if alt_beats is False else "unverified"
        )
        alternatives.append(
            f"{venue} {_direction(alt_prob)} "
            f"{max(alt_prob, 1 - alt_prob):.0%} • {alt_state}"
        )
    if alternatives:
        embed.add_field(
            name="Other venues with verified books",
            value="\n".join(alternatives),
            inline=False,
        )
    if stream_shadow is not None:
        if shadow is None:
            embed.add_field(
                name="Source-matched after-cost proof",
                value="Cannot verify real forward quote evidence. NOT PROVEN.",
                inline=False,
            )
        else:
            try:
                severe = shadow["costs"]["severe"]["mean_net_bps"]
                embed.add_field(
                    name="Source-matched after-cost proof",
                    value=(
                        f"**{shadow['status']}** • "
                        f"{shadow['closed']}/{shadow['eligible_windows']} "
                        "prospective quote windows paired"
                        f" • {shadow['days']} days"
                        + (f" • Severe-cost mean {severe:+.1f} bps"
                           if severe is not None else
                           " • Too few observed exits")
                        + "\\nOnly genuine later same-venue books; "
                          "no authenticated fills or guaranteed returns."
                    ),
                    inline=False,
                )
            except Exception:  # noqa: BLE001 - missing proof must not halt signals
                _LOG.exception("RivaL forward quote evidence unavailable")
                embed.add_field(
                    name="Source-matched after-cost proof",
                    value="Cannot verify real forward quote evidence. NOT PROVEN.",
                    inline=False,
                )
    embed.set_footer(
        text=(
            "Research watch only • Refresh to recheck • No approved "
            "entry/stop/target or real order"
        ),
    )
    _LOG.info(
        "RivaL signal asset=%s horizon=%d watches_ms=%.0f book_ms=%.0f "
        "book_venue=%s proof_ms=%.0f shadow_ms=%.0f selection=%s "
        "alternatives=%d venue_beats=%s",
        asset, horizon_minutes, marks.get("watches_ms", -1),
        marks.get("book_ms", -1),
        chosen["venue"] if book is not None else "none",
        marks.get("proof_ms", -1), marks.get("shadow_ms", -1),
        selection, len(alternatives),
        ("true" if venue_beats is True else
         "false" if venue_beats is False else "none"),
    )
    return embed
