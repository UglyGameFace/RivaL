"""One authoritative, market-matched live signal view for the Discord bot.

Research watches are not profitable trading instructions. An exchange quote
only informs the matching venue's prediction; blocked sources never borrow
another venue's bid/ask or an old candle masquerading as a live quote.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.opportunities import ASSETS, recent_watches
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import (
    BookQuote,
    PerpReferenceBook,
    PublicBestQuoteClient,
    QuoteUnavailable,
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
) -> BookQuote | PerpReferenceBook:
    if venue == "binance":
        return await quotes.get(asset)
    if venue == "hyperliquid":
        return await quotes.get_reference(asset)
    raise QuoteUnavailable("No verified live order-book source for this venue.")


def _proof(
    market: PerpetualMarketStore, *, asset: str, horizon: int, now: datetime,
) -> dict:
    return ForwardAccuracyMonitor(market).summary(asset=asset, now=now)[
        "last_7d"
    ][horizon]


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
        watches = await asyncio.to_thread(
            recent_watches, market, asset=asset, now=stamp,
            limit=40, dedupe_horizons=False,
        )
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
    # Prefer Hyperliquid when Binance USDM is blocked in the hosting region.
    # The venue loop keeps its priority short-circuit: at most one quote
    # request per supported venue, and Binance is never queried once a
    # Hyperliquid book validates. The book fetch runs concurrently with the
    # independent forward-evidence lookups below instead of before them, so
    # a slow database no longer stretches every tap by its own round trip.
    prioritized = sorted(
        live,
        key=lambda row: (
            0 if row["venue"] == "hyperliquid" else
            1 if row["venue"] == "binance" else 2,
            -row["issued"].timestamp(),
        ),
    )

    async def _pick_book() -> tuple[dict, BookQuote | PerpReferenceBook | None]:
        checked_venues: set[str] = set()
        for watch in prioritized:
            venue = watch["venue"]
            if venue not in ("hyperliquid", "binance") or venue in checked_venues:
                continue
            checked_venues.add(venue)
            try:
                candidate_book = await _venue_book(quotes, asset=asset, venue=venue)
                candidate_book.validate(now=stamp if now is not None else datetime.now(UTC))
            except QuoteUnavailable:
                continue
            return watch, candidate_book
        return prioritized[0], None

    async def _proof_or_default() -> dict:
        try:
            return await asyncio.to_thread(
                _proof, market, asset=asset, horizon=horizon_minutes, now=stamp,
            )
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL signal forward-evidence lookup unavailable")
            return {"settled": 0, "hit_rate": None, "model_beats_baseline": None}

    async def _shadow_or_none() -> dict | None:
        if stream_shadow is None:
            return None
        try:
            return await asyncio.to_thread(
                stream_shadow.report, asset=asset,
                horizon_minutes=horizon_minutes, now=stamp,
            )
        except Exception:  # noqa: BLE001 - missing proof must not halt signals
            _LOG.exception("RivaL forward quote evidence unavailable")
            return None

    book_outcome, evidence, shadow = await asyncio.gather(
        _pick_book(), _proof_or_default(), _shadow_or_none(),
        return_exceptions=True,
    )
    if isinstance(book_outcome, Exception):
        raise book_outcome
    if isinstance(evidence, Exception):  # pragma: no cover - helper already guards
        evidence = {"settled": 0, "hit_rate": None, "model_beats_baseline": None}
    chosen, book = book_outcome
    probability = float(chosen["probability_up"])
    strength = model_strength(probability)
    direction = "UP" if probability > 0.5 else (
        "DOWN" if probability < 0.5 else "FLAT"
    )
    count = int(evidence["settled"])
    fails_baseline = evidence.get("model_beats_baseline") is False
    is_watch = (
        book is not None and strength == "directional watch"
        and not fails_baseline and direction != "FLAT"
    )
    if fails_baseline:
        verdict = "⛔ PASS • MODEL UNDERPERFORMING"
        message = "Recent forward scores do not beat a simple baseline."
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
        quote_label = (
            "Binance USD-M USDT contract" if chosen["venue"] == "binance"
            else "Hyperliquid USD-perp reference"
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
    hit = evidence.get("hit_rate")
    embed.add_field(
        name="Model track record (last 7 days)",
        value=(
            f"{count} settled source forecasts"
            + (f" • Direction correct {hit:.1%}" if hit is not None else
               " • Not enough scored outcomes")
            + "\nCross-exchange outcomes are correlated. Neither an "
              "independent edge nor after-cost profitability is established."
        ),
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
    return embed
