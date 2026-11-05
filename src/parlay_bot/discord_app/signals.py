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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import discord

from parlay_bot.discord_app.opportunities import (
    ASSETS,
    recent_multi_watches,
    recent_watches,
)
from parlay_bot.perpetuals.event_intelligence import EventIntelligence
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import (
    MODEL as FORWARD_MODEL,
)
from parlay_bot.perpetuals.forward_audit import (
    ForwardAccuracyMonitor,
)
from parlay_bot.perpetuals.live_quotes import (
    BookQuote,
    PerpReferenceBook,
    PublicBestQuoteClient,
    QuoteUnavailable,
    VenueBookQuote,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import HORIZONS as RESEARCH_HORIZONS
from parlay_bot.perpetuals.model_competition import CHALLENGER_PREFIXES, FAMILIES
from parlay_bot.perpetuals.promotion import multi_summary, promoted_engine
from parlay_bot.perpetuals.readiness import _source_identity
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger

_LOG = logging.getLogger(__name__)
HORIZONS = RESEARCH_HORIZONS
MIN_REMAINING = timedelta(seconds=90)
WATCH_THRESHOLD = 0.56
_T = TypeVar("_T")


class SignalStageTimeout(TimeoutError):
    """A safe no-watch result: this named stage exceeded the shared budget."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"Qualified signal stage {stage} exceeded its deadline")




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
        title=f"⏳ {asset} • next {horizon} min • No prediction right now",
        description=reason + "\n\nNothing is traded for you.",
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
    now: datetime, model_version: str | None = None,
) -> dict:
    if model_version in FAMILIES or (model_version or "").startswith(
        CHALLENGER_PREFIXES
    ):
        contract, quote = _source_identity(venue, asset)
        return FamilyPromotion(market.store).venue_summary(
            asset=asset, venue=venue, contract=contract, quote=quote,
            horizon=horizon, now=now, model_version=model_version,
        )
    return ForwardAccuracyMonitor(market).venue_summary(
        asset=asset, venue=venue, horizon=horizon, now=now,
        model_version=model_version,
    )


async def signal_embed(
    market: PerpetualMarketStore | None,
    quotes: PublicBestQuoteClient,
    *, asset: str, horizon_minutes: int, now: datetime | None = None,
    stream_shadow: StreamShadowLedger | None = None,
    decision_out: dict | None = None,
    decision_only: bool = False,
    deadline: float | None = None,
) -> discord.Embed:
    """Refresh the selected market, its real book and measured forward results.

    At most one quote request per supported venue; no forecast or quote cache
    is used after its source-specific validity expires.

    When decision_out is given, it is filled with the chosen forecast's
    identity and verdict (venue, engine, model version, issued/target,
    direction, probability, is_watch, the validated book, and the
    source-matched after-cost status) before the embed returns. The
    Auto-Hunt qualified tier reads it instead of re-implementing the
    selection policy, so the push gate can never drift from the tap-time
    gate. Early no-signal returns leave it empty, which readers treat as
    not a watch. Background decision_only skips presentation-only proof
    and quote-shadow reports, not the source/venue/watch qualification.
    """
    async def bounded(
        stage: str, work: Callable[[], Awaitable[_T]], *, max_seconds: float,
    ) -> _T:
        """Every stage uses the SAME monotonic deadline, not fresh budgets."""
        if not decision_only or deadline is None:
            # Preserve the preexisting per-venue quote timeout for manual
            # menus and standalone decision checks without a shared budget.
            if stage.startswith("quote:"):
                try:
                    return await asyncio.wait_for(work(), timeout=max_seconds)
                except TimeoutError as exc:
                    raise SignalStageTimeout(stage) from exc
            return await work()
        remaining = deadline - time.monotonic() - 0.15
        if remaining <= 0:
            raise SignalStageTimeout(stage)
        try:
            return await asyncio.wait_for(
                work(), timeout=min(max_seconds, remaining),
            )
        except TimeoutError as exc:
            raise SignalStageTimeout(stage) from exc

    if asset not in ASSETS or horizon_minutes not in HORIZONS:
        raise ValueError("choose BTC, ETH, SOL or XRP and 5, 15 or 60 minutes")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("live signal clock requires timezone")
    stamp = stamp.astimezone(UTC)
    if market is None:
        return _no_signal(asset, horizon_minutes, "Market collection is disconnected.")

    # Only verified official-event windows trigger a deterministic NO CALL.
    # Event feeds are polled off-loop by AutonomousResearch, never here.
    try:
        event_context = await bounded(
            "macro-events",
            lambda: asyncio.to_thread(
                EventIntelligence(market.store).context,
                asset=asset, horizon=horizon_minutes, now=stamp,
            ),
            max_seconds=1.0,
        )
    except SignalStageTimeout:
        raise
    except Exception:  # noqa: BLE001 - don't invent an event all-clear
        _LOG.exception("RivaL macro-event risk lookup unavailable")
        event_context = {"decision": "not_verified", "status": "unavailable"}
    if event_context["decision"] == "pause":
        event = next(
            (e for e in event_context["events"] if e["risk_window_active"]), None,
        )
        label = event["title"][:115] if event else "Official economic event"
        if decision_out is not None:
            decision_out["event_risk"] = "official_release_window"
            decision_out["is_watch"] = False
        return _no_signal(
            asset, horizon_minutes,
            f"Scheduled or newly published official event: {label}. "
            "New entries are withheld during this event-risk window. "
            "This is a volatility precaution, not a predicted price direction.",
        )

    try:
        watches_started = time.monotonic()
        try:
            engine = await bounded(
                "promotion",
                lambda: asyncio.to_thread(
                    promoted_engine, market.store, asset=asset,
                    horizon=horizon_minutes, now=stamp,
                ),
                max_seconds=1.5,
            )
        except SignalStageTimeout:
            raise
        except Exception:  # noqa: BLE001 - promotion lookup never blocks signals
            _LOG.exception("RivaL promotion lookup unavailable")
            if decision_only:
                return _no_signal(
                    asset, horizon_minutes, "Model promotion evidence unavailable.",
                )
            engine = "live"
        reader = recent_multi_watches if engine == "multi" else recent_watches
        watches = await bounded(
            "forecast-read",
            lambda: asyncio.to_thread(
                reader, market, asset=asset, now=stamp,
                limit=40, dedupe_horizons=False,
                horizon_minutes=horizon_minutes,
            ),
            max_seconds=2.5,
        )
        marks = {"watches_ms": (time.monotonic() - watches_started) * 1000}
    except SignalStageTimeout:
        raise
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
            "The latest prediction is almost over, so RivaL won't show it as "
            "a new one. A fresh one comes every 5 minutes."
            if relevant else
            "There's no new prediction for this yet. RivaL makes them as new "
            "prices come in; old data can't stand in for one."
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

    async def _pick_books(
        venues: list[str] | None = None,
    ) -> dict[str, tuple[dict, BookQuote | PerpReferenceBook | VenueBookQuote]]:
        target_venues = candidate_venues if venues is None else venues
        if not target_venues:
            return {}
        started = time.monotonic()
        watch_by_venue = {
            venue: next(w for w in prioritized if w["venue"] == venue)
            for venue in target_venues
        }
        async def bounded_book(venue: str):
            # A slow DNS lookup on one remote exchange must not consume the
            # entire seven-second background Auto-Hunt decision budget.
            # Missing quotes fail closed; no cross-venue substitution.
            try:
                return await bounded(
                    f"quote:{venue}",
                    lambda: _venue_book(quotes, asset=asset, venue=venue),
                    max_seconds=2.5 if decision_only else 4.5,
                )
            except SignalStageTimeout as exc:
                _LOG.info(
                    "RivaL qualified quote timeout asset=%s horizon=%dm "
                    "venue=%s stage=%s; no stale quote substituted",
                    asset, horizon_minutes, venue, exc.stage,
                )
                raise QuoteUnavailable(
                    "Source-matched order book timed out"
                ) from exc

        outcomes = await asyncio.gather(
            *(bounded_book(venue) for venue in target_venues),
            return_exceptions=True,
        )
        marks["book_ms"] = (time.monotonic() - started) * 1000
        validated: dict[str, tuple[dict, BookQuote | PerpReferenceBook | VenueBookQuote]] = {}
        for venue, outcome in zip(target_venues, outcomes, strict=True):
            if isinstance(outcome, Exception):
                continue
            try:
                outcome.validate(now=stamp if now is not None else datetime.now(UTC))
            except QuoteUnavailable:
                continue
            validated[venue] = (watch_by_venue[venue], outcome)
        return validated

    def _default_evidence() -> dict:
        return {
            "settled": 0, "hit_rate": None, "model_beats_baseline": None,
            "watch_evidence_passes": False, "watch_days": 0,
            "watch_reason": "unavailable",
        }

    forecast_for_venue = {
        venue: next(w for w in prioritized if w["venue"] == venue)
        for venue in candidate_venues
    }
    shared_multi_proof: dict | None = None

    async def _venue_proof_or_default(venue: str) -> dict:
        if engine == "multi" and shared_multi_proof is not None:
            return shared_multi_proof
        # Never borrow scores from another model when a legacy forecast
        # omitted its model identity.
        if not forecast_for_venue[venue].get("model_version"):
            return _default_evidence()
        started = time.monotonic()
        try:
            return await bounded(
                f"venue-proof:{venue}",
                lambda: asyncio.to_thread(
                    _venue_proof, market, asset=asset, horizon=horizon_minutes,
                    venue=venue, now=stamp,
                    model_version=forecast_for_venue[venue].get("model_version"),
                ),
                max_seconds=2.5,
            )
        except SignalStageTimeout as exc:
            _LOG.info(
                "RivaL qualified evidence timeout asset=%s horizon=%dm "
                "stage=%s; venue not qualified",
                asset, horizon_minutes, exc.stage,
            )
            return _default_evidence()
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL signal venue evidence lookup unavailable")
            return _default_evidence()
        finally:
            marks[f"proof_{venue}_ms"] = (time.monotonic() - started) * 1000

    async def _proof_or_default() -> dict:
        if engine == "multi" and shared_multi_proof is not None:
            return shared_multi_proof
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

    if engine == "multi":
        try:
            shared_multi_proof = await bounded(
                "multi-proof",
                lambda: asyncio.to_thread(
                    multi_summary, market.store, asset=asset,
                    horizon=horizon_minutes, now=stamp,
                ),
                max_seconds=2.5,
            )
        except SignalStageTimeout as exc:
            _LOG.info("RivaL multi-source evidence timed out stage=%s", exc.stage)
            shared_multi_proof = _default_evidence()
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL multi proof lookup unavailable")
            shared_multi_proof = _default_evidence()

    if decision_only:
        # The background notifier needs only the EXACT same venue/book
        # qualification used by the screen. Full portfolio analytics and
        # after-cost report rendering are expensive, irrelevant to deciding
        # whether an alert is a WATCH, and compete with the research writer.
        # Venues whose forecast is not a directional watch, or whose 7-day
        # record does not beat baseline, can never become a qualified WATCH;
        # skip network order-book requests for them.
        watch_by_venue = {
            venue: next(w for w in prioritized if w["venue"] == venue)
            for venue in candidate_venues
        }
        strong_venues = [
            venue for venue in candidate_venues
            if model_strength(float(watch_by_venue[venue]["probability_up"]))
            == "directional watch"
            and float(watch_by_venue[venue]["probability_up"]) != 0.5
        ]
        if not strong_venues:
            return _no_signal(
                asset, horizon_minutes, "No directional watch candidate.",
            )
        outcomes = await asyncio.gather(
            *(_venue_proof_or_default(v) for v in strong_venues),
            return_exceptions=True,
        )
        venue_proofs = {
            venue: (item if not isinstance(item, BaseException)
                    else _default_evidence())
            for venue, item in zip(strong_venues, outcomes, strict=True)
        }
        beating_venues = [
            venue for venue in strong_venues
            if venue_proofs[venue].get("model_beats_baseline") is True
            and venue_proofs[venue].get("watch_evidence_passes") is True
        ]
        if not beating_venues:
            return _no_signal(
                asset, horizon_minutes, "No baseline-beating venue candidate.",
            )
        validated = await _pick_books(beating_venues)
        if not validated:
            return _no_signal(
                asset, horizon_minutes, "No verified matching live book.",
            )
        evidence = _default_evidence()
        shadow = None
    else:
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
        if isinstance(evidence, Exception):  # pragma: no cover - helper guards
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
        and venue_proofs[venue].get("watch_evidence_passes") is True
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
    if decision_only:
        if decision_out is not None:
            decision_out.update({
                "asset": asset,
                "horizon_minutes": horizon_minutes,
                "venue": chosen["venue"],
                "engine": engine,
                "model_version": chosen.get("model_version") or FORWARD_MODEL,
                "issued": chosen["issued"],
                "target": chosen["target"],
                "direction": direction,
                "probability": probability,
                "is_watch": is_watch,
                "bid": float(book.bid) if book is not None else None,
                "ask": float(book.ask) if book is not None else None,
                "quote_at": book.exchange_at if book is not None else None,
                # A book quote is not a proven net trade; the notifier must
                # never invent a fast-path after-cost performance number.
                "after_cost_bps": None,
                "after_cost_status": None,
            })
        return _no_signal(asset, horizon_minutes, "Screening completed.")

    if venue_beats is False:
        verdict = "⛔ No call: doing worse than a simple guess"
        message = (
            f"On {chosen['venue']}, its recent predictions haven't beaten a simple "
            "guess, so RivaL won't make a call."
        )
    elif book is None:
        verdict = "⏳ No call: can't get a live price"
        message = (
            "There's a prediction, but RivaL couldn't get a live price from the "
            "same exchange, and it won't use another exchange's price instead."
        )
    elif is_watch:
        verdict = f"👀 Leans {direction}: watch only"
        message = (
            "The model expects the price to go "
            + ("higher" if direction == "UP" else "lower")
            + ". It has done better than a simple guess on this exchange, but "
              "that doesn't prove it makes money. This is not advice to trade."
        )
    elif (strength == "directional watch" and venue_beats is True
            and not venue_evidence.get("watch_evidence_passes")
            and direction != "FLAT"):
        verdict = "⏳ No call: not enough proof yet"
        message = (
            "On average it beats a simple guess, but not on enough separate "
            "days to trust it yet."
        )
    elif (strength == "directional watch" and venue_beats is None
            and direction != "FLAT"):
        verdict = "⏳ No call: track record can't be checked"
        message = (
            f"There aren't enough finished predictions on {chosen['venue']} to "
            "check its record (or the lookup failed). The direction below is "
            "for information only."
        )
    else:
        verdict = (f"⏳ No call: only a slight {direction} lean" if direction != "FLAT"
                   else "⏳ No call: no direction")
        message = (
            f"The model is only {max(probability, 1 - probability):.1%} sure, too "
            "close to a coin flip to make a call."
        )

    embed = discord.Embed(
        title=f"{asset} • next {horizon_minutes} min • {verdict}"[:256],
        description=message,
        # Only verified watches receive directional colours. Unproven leans stay amber.
        color=(discord.Color.green() if is_watch and direction == "UP"
               else discord.Color.red() if is_watch and direction == "DOWN"
               else discord.Color.orange()),
    )
    # Four quick-glance fields mirror the agreed compact dashboard without
    # disguising share prices, raw model output or an unverified trade as proof.
    # Book prices MUST belong to this forecast's own matching venue.
    venue_count = int(venue_evidence["settled"]) if venue_evidence else 0
    venue_hit = venue_evidence.get("hit_rate") if venue_evidence else None
    measured = (
        f"{venue_hit:.1%} of {venue_count:,} finished\n"
        f"This exchange • last 7 days"
        if venue_hit is not None and venue_count >= 50
        else f"Not enough yet\n{venue_count:,} finished; needs 50"
    )
    matched_quote = (
        f"Buy ~${book.ask:,.{4 if asset == 'XRP' else 2}f}\n"
        f"Sell ~${book.bid:,.{4 if asset == 'XRP' else 2}f}"
        if book is not None else "Not available"
    )
    plain_strength = {"very weak": "very weak", "weak": "weak",
                      "directional watch": "strong enough to watch"}.get(strength, strength)
    for index, (name, value) in enumerate((
        ("💱 Live price (same exchange)", matched_quote),
        ("⏱️ Prediction ends", discord.utils.format_dt(
            chosen["target"] + timedelta(minutes=5), style="R",
        )),
        ("⚡ How sure the model is", (f"{max(probability, 1-probability):.1%}\n"
                                     f"{plain_strength} (its own estimate)")),
        ("📊 How often it's been right", measured),
    )):
        embed.add_field(name=name, value=value, inline=True)
        if index in (1, 3):
            embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name="Details",
        value=(
            f"**Call: {direction if is_watch else 'none'}** • leans {direction} "
            f"{max(probability, 1 - probability):.1%} "
            f"({plain_strength})\n"
            f"Exchange: **{chosen['venue']}** • "
            f"Made {discord.utils.format_dt(chosen['issued'], style='R')}\n"
            f"Ends {discord.utils.format_dt(chosen['target'] + timedelta(minutes=5), style='R')}"
            + (
                f"\nModel: **combined from {chosen.get('contributor_count', '?')} "
                f"exchanges** ({chosen.get('model_version', 'multi')})"
                if chosen.get("engine") == "multi" else ""
            )
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
        price_decimals = 4 if asset == "XRP" else 2
        embed.add_field(
            name="Live price right now 💱",
            value=(
                f"**{quote_label}**\n"
                f"Buy at about **${book.ask:,.{price_decimals}f}** · "
                f"Sell at about **${book.bid:,.{price_decimals}f}**\n"
                f"Seen {discord.utils.format_dt(book.exchange_at, style='R')} "
                "• Real orders can fill at a different price"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Live price",
            value=(
                "**Not available.** An old candle's price isn't a price you "
                "could actually trade at."
            ),
            inline=False,
        )
    track_lines: list[str] = []
    if venue_evidence is not None:
        venue_hit = venue_evidence.get("hit_rate")
        venue_count = int(venue_evidence["settled"])
        venue_label = "Combined model" if engine == "multi" else chosen["venue"]
        track_lines.append(
            f"{venue_label}: {venue_count:,} finished predictions"
            + (f" • right {venue_hit:.1%}"
               if venue_hit is not None else " • not enough scored yet")
            + (" • better than a simple guess" if venue_beats is True else
               " • worse than a simple guess" if venue_beats is False else
               " • too few to judge (needs 50)")
        )
        reason = str(venue_evidence.get("watch_reason", "unavailable")).replace("_", " ")
        track_lines.append(
            "Good on enough separate days? "
            + ("Yes" if venue_evidence.get("watch_evidence_passes")
               else f"Not yet ({reason})")
            + f" • {venue_evidence.get('watch_days', 0)} days count so far"
        )
    else:
        track_lines.append(
            f"{chosen['venue']}: record not checked (no live price from it)."
        )
    global_hit = evidence.get("hit_rate")
    global_count = int(evidence["settled"])
    if engine == "multi":
        challenger_brier = evidence.get("challenger_brier")
        incumbent_brier = evidence.get("incumbent_brier")
        if challenger_brier is not None and incumbent_brier is not None:
            track_lines.append(
                f"Vs the current model on the same candles: error score "
                f"{incumbent_brier:.4f} (current) vs {challenger_brier:.4f} (combined); "
                "lower is better."
            )
        else:
            track_lines.append("Couldn't compare with the current model.")
    else:
        track_lines.append(
            f"All exchanges: {global_count:,} finished"
        + (f" • right {global_hit:.1%}"
           if global_hit is not None else " • not enough scored yet")
    )
    track_lines.append(
        "Exchanges tend to move together, so these aren't fully separate "
        "results. It hasn't been shown to make money after fees."
    )
    embed.add_field(
        name="Track record (last 7 days)",
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
            "good enough to watch"
            if alt_beats is True
            and venue_proofs[venue].get("watch_evidence_passes") is True
            and model_strength(alt_prob) == "directional watch"
            else "worse than a simple guess" if alt_beats is False
            else "not enough record"
        )
        alternatives.append(
            f"{venue}: leans {_direction(alt_prob)} "
            f"{max(alt_prob, 1 - alt_prob):.0%} • {alt_state}"
        )
    if alternatives:
        embed.add_field(
            name="Other exchanges with a live price",
            value="\n".join(alternatives),
            inline=False,
        )
    if stream_shadow is not None:
        if shadow is None:
            embed.add_field(
                name="Would it have made money after fees?",
                value="Couldn't check this. Not proven.",
                inline=False,
            )
        else:
            try:
                severe = shadow["costs"]["severe"]["mean_net_bps"]
                status = {"NOT_PROVEN": "Not proven"}.get(
                    str(shadow["status"]), str(shadow["status"]).replace("_", " ").title())
                embed.add_field(
                    name="Would it have made money after fees?",
                    value=(
                        f"**{status}** • "
                        f"{shadow['closed']} of {shadow['eligible_windows']} pretend "
                        f"trades could be checked against later real prices"
                        f" • {shadow['days']} days"
                        + (f" • with high fees, average {severe / 100:+.2f}% per trade"
                           if severe is not None else
                           " • too few finished to say")
                        + "\nUses real prices seen later on the same exchange; "
                          "no actual trades, no guaranteed returns."
                    ),
                    inline=False,
                )
            except Exception:  # noqa: BLE001 - missing proof must not halt signals
                _LOG.exception("RivaL forward quote evidence unavailable")
                embed.add_field(
                    name="Would it have made money after fees?",
                    value="Couldn't check this. Not proven.",
                    inline=False,
                )
    embed.set_footer(
        text=(
            "For watching only • Refresh to check again • No buy/sell levels "
            "and no real orders"
        ),
    )
    _LOG.info(
        "RivaL signal asset=%s horizon=%d watches_ms=%.0f book_ms=%.0f "
        "book_venue=%s proof_ms=%.0f shadow_ms=%.0f selection=%s "
        "alternatives=%d venue_beats=%s engine=%s",
        asset, horizon_minutes, marks.get("watches_ms", -1),
        marks.get("book_ms", -1),
        chosen["venue"] if book is not None else "none",
        marks.get("proof_ms", -1), marks.get("shadow_ms", -1),
        selection, len(alternatives),
        ("true" if venue_beats is True else
         "false" if venue_beats is False else "none"),
        engine,
    )
    if decision_out is not None:
        after_cost_bps: float | None = None
        after_cost_status: str | None = None
        if shadow is not None:
            try:
                after_cost_status = str(shadow["status"])
                after_cost_bps = shadow["costs"]["severe"]["mean_net_bps"]
            except Exception:  # noqa: BLE001 - missing proof stays unknown
                after_cost_bps = None
                after_cost_status = None
        decision_out.update({
            "asset": asset,
            "horizon_minutes": horizon_minutes,
            "venue": chosen["venue"],
            "engine": engine,
            "model_version": chosen.get("model_version") or FORWARD_MODEL,
            "issued": chosen["issued"],
            "target": chosen["target"],
            "direction": direction,
            "probability": probability,
            "is_watch": is_watch,
            "bid": float(book.bid) if book is not None else None,
            "ask": float(book.ask) if book is not None else None,
            "quote_at": book.exchange_at if book is not None else None,
            "after_cost_bps": after_cost_bps,
            "after_cost_status": after_cost_status,
        })
    return embed
