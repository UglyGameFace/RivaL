from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard


class OddsNormalizationError(ValueError):
    """OddsPapi payload is missing a structural field required for safe normalization."""


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OddsNormalizationError(f"Expected ISO timestamp string, got {type(value).__name__}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise OddsNormalizationError("OddsPapi timestamp was not timezone-aware")
    return parsed.astimezone(UTC)


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OddsNormalizationError(f"OddsPapi {field} must be an object")
    return value


def normalize_current_odds(
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> NormalizedOddsBoard:
    """Flatten one OddsPapi /v4/odds response into canonical selections.

    OddsPapi nests current prices as:
    bookmakerOdds -> bookmaker -> markets -> market -> outcomes -> outcome
    -> players -> player.
    """

    fixture_id = payload.get("fixtureId")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise OddsNormalizationError("OddsPapi odds payload is missing fixtureId")

    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    bookmaker_odds = _mapping(payload.get("bookmakerOdds", {}), field="bookmakerOdds")
    observations: list[MarketObservation] = []

    for bookmaker, bookmaker_payload in bookmaker_odds.items():
        book = _mapping(bookmaker_payload, field=f"bookmakerOdds.{bookmaker}")
        markets = _mapping(book.get("markets", {}), field=f"{bookmaker}.markets")

        for market_id, market_payload in markets.items():
            market = _mapping(market_payload, field=f"{bookmaker}.markets.{market_id}")
            outcomes = _mapping(
                market.get("outcomes", {}),
                field=f"{bookmaker}.markets.{market_id}.outcomes",
            )

            for outcome_id, outcome_payload in outcomes.items():
                outcome = _mapping(
                    outcome_payload,
                    field=f"{bookmaker}.{market_id}.{outcome_id}",
                )
                players = _mapping(
                    outcome.get("players", {}),
                    field=f"{bookmaker}.{market_id}.{outcome_id}.players",
                )

                for player_id, player_payload in players.items():
                    player = _mapping(
                        player_payload,
                        field=f"{bookmaker}.{market_id}.{outcome_id}.{player_id}",
                    )
                    price = player.get("price")
                    changed_at = _parse_datetime(player.get("changedAt"))
                    if not isinstance(price, (int, float)) or changed_at is None:
                        # A selection without a price or provider change timestamp cannot
                        # safely participate in line-movement history.
                        continue

                    limit = player.get("limit")
                    if limit is not None and not isinstance(limit, (int, float)):
                        limit = None

                    observations.append(
                        MarketObservation(
                            provider="oddspapi",
                            fixture_id=fixture_id,
                            sport_id=payload.get("sportId"),
                            tournament_id=payload.get("tournamentId"),
                            bookmaker=str(bookmaker),
                            market_id=str(market_id),
                            outcome_id=str(outcome_id),
                            player_id=str(player_id),
                            player_name=player.get("playerName"),
                            active=bool(player.get("active", False)),
                            main_line=bool(player.get("mainLine", False)),
                            price_decimal=float(price),
                            price_american=(
                                str(player["priceAmerican"])
                                if player.get("priceAmerican") is not None
                                else None
                            ),
                            price_fractional=(
                                str(player["priceFractional"])
                                if player.get("priceFractional") is not None
                                else None
                            ),
                            limit=float(limit) if limit is not None else None,
                            changed_at=changed_at,
                            bookmaker_changed_at=_parse_datetime(
                                player.get("bookmakerChangedAt")
                            ),
                            observed_at=observed,
                            exchange_meta=player.get("exchangeMeta"),
                        )
                    )

    return NormalizedOddsBoard(
        provider="oddspapi",
        fixture_id=fixture_id,
        sport_id=payload.get("sportId"),
        tournament_id=payload.get("tournamentId"),
        status_id=payload.get("statusId"),
        status_name=payload.get("statusName"),
        start_time=_parse_datetime(payload.get("startTime")),
        updated_at=_parse_datetime(payload.get("updatedAt")),
        participant1_name=payload.get("participant1Name"),
        participant2_name=payload.get("participant2Name"),
        sport_name=payload.get("sportName"),
        tournament_name=payload.get("tournamentName"),
        observations=observations,
    )
