from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard


class SportsGameOddsNormalizationError(ValueError):
    """A SportsGameOdds event cannot be normalized safely."""


def american_to_decimal(value: str | float) -> float:
    try:
        american = float(value)
    except (TypeError, ValueError) as exc:
        raise SportsGameOddsNormalizationError(f"Invalid American odds: {value!r}") from exc

    if american == 0:
        raise SportsGameOddsNormalizationError("American odds cannot be zero")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _name(entity: Any) -> str | None:
    if not isinstance(entity, dict):
        return None
    for key in ("name", "displayName"):
        value = entity.get(key)
        if isinstance(value, str) and value:
            return value
    names = entity.get("names")
    if isinstance(names, dict):
        for key in ("display", "long", "medium", "short"):
            value = names.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _player_identity(event: dict[str, Any], odd: dict[str, Any]) -> tuple[str, str | None]:
    explicit = odd.get("playerID")
    stat_entity = odd.get("statEntityID")
    candidate = str(explicit or stat_entity or "")

    players = event.get("players")
    if isinstance(players, dict) and candidate in players:
        return candidate, _name(players[candidate])

    if candidate and candidate.casefold() not in {"home", "away", "all", "draw"}:
        return candidate, None
    return "0", None


def _canonical_market_id(odd_id: str, opposing_id: str | None) -> str:
    if not opposing_id:
        return odd_id
    return "::".join(sorted((odd_id, opposing_id)))


def _line_values(book: dict[str, Any], bet_type: str | None) -> tuple[float | None, float | None]:
    raw: Any = None
    if book.get("spread") is not None:
        raw = book.get("spread")
    elif book.get("overUnder") is not None:
        raw = book.get("overUnder")

    if raw is None:
        return None, None
    try:
        line = float(raw)
    except (TypeError, ValueError):
        return None, None

    group = abs(line) if bet_type == "sp" else line
    return line, group


def _status(event: dict[str, Any]) -> tuple[int, str]:
    status = event.get("status") if isinstance(event.get("status"), dict) else {}
    if event.get("cancelled") is True or status.get("cancelled") is True:
        return 3, "Cancelled"
    if status.get("finalized") is True or status.get("ended") is True:
        return 2, "Finished"
    if status.get("started") is True:
        return 1, "Live"
    return 0, "Pre-Game"


def normalize_event(
    event: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> NormalizedOddsBoard:
    event_id = event.get("eventID")
    if not isinstance(event_id, str) or not event_id:
        raise SportsGameOddsNormalizationError("SportsGameOdds event is missing eventID")

    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    status = event.get("status") if isinstance(event.get("status"), dict) else {}
    teams = event.get("teams") if isinstance(event.get("teams"), dict) else {}
    odds = event.get("odds") if isinstance(event.get("odds"), dict) else {}

    status_id, status_name = _status(event)
    start_time = _datetime(status.get("startsAt") or event.get("startTime"))

    observations: list[MarketObservation] = []
    newest_change: datetime | None = None

    for odd_key, raw_odd in odds.items():
        if not isinstance(raw_odd, dict):
            continue
        odd_id = str(raw_odd.get("oddID") or odd_key)
        opposing = raw_odd.get("opposingOddID")
        opposing_id = str(opposing) if opposing else None
        market_id = _canonical_market_id(odd_id, opposing_id)
        market_name = raw_odd.get("marketName")
        side_id = str(raw_odd.get("sideID") or odd_id)
        outcome_name = side_id.replace("_", " ").replace("+", " + ").title()
        player_id, player_name = _player_identity(event, raw_odd)
        bet_type = str(raw_odd.get("betTypeID")) if raw_odd.get("betTypeID") else None

        odd_started = bool(raw_odd.get("started", False))
        odd_ended = bool(raw_odd.get("ended", False))
        odd_cancelled = bool(raw_odd.get("cancelled", False))

        books = raw_odd.get("byBookmaker")
        if not isinstance(books, dict):
            continue

        for bookmaker, raw_book in books.items():
            if not isinstance(raw_book, dict):
                continue
            price = raw_book.get("odds")
            if price in (None, ""):
                continue
            try:
                decimal_price = american_to_decimal(price)
            except SportsGameOddsNormalizationError:
                continue

            changed = _datetime(raw_book.get("lastUpdatedAt")) or observed
            if newest_change is None or changed > newest_change:
                newest_change = changed

            line_value, line_group_value = _line_values(raw_book, bet_type)
            observations.append(
                MarketObservation(
                    provider="sportsgameodds",
                    fixture_id=event_id,
                    sport_id=event.get("sportID"),
                    tournament_id=event.get("leagueID"),
                    bookmaker=str(bookmaker),
                    market_id=market_id,
                    market_name=str(market_name) if market_name else None,
                    outcome_id=odd_id,
                    outcome_name=outcome_name,
                    player_id=player_id,
                    player_name=player_name,
                    line_value=line_value,
                    line_group_value=line_group_value,
                    deeplink=(
                        str(raw_book["deeplink"])
                        if raw_book.get("deeplink") is not None
                        else None
                    ),
                    active=(
                        bool(raw_book.get("available", True))
                        and not odd_started
                        and not odd_ended
                        and not odd_cancelled
                    ),
                    main_line=True,
                    price_decimal=decimal_price,
                    price_american=str(price),
                    changed_at=changed,
                    bookmaker_changed_at=changed,
                    observed_at=observed,
                )
            )

    return NormalizedOddsBoard(
        provider="sportsgameodds",
        fixture_id=event_id,
        sport_id=event.get("sportID"),
        tournament_id=event.get("leagueID"),
        status_id=status_id,
        status_name=status_name,
        start_time=start_time,
        updated_at=newest_change,
        participant1_name=_name(teams.get("home")),
        participant2_name=_name(teams.get("away")),
        sport_name=str(event.get("sportID")) if event.get("sportID") else None,
        tournament_name=str(event.get("leagueID")) if event.get("leagueID") else None,
        observations=observations,
    )
