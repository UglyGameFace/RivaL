from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.parlays.math import (
    combined_decimal_odds,
    expected_return_edge,
    independent_joint_probability,
    no_vig_probabilities,
)
from parlay_bot.parlays.models import ParlayBuildError, ParlayLeg, ParlaySlip, RiskMode
from parlay_bot.storage.catalog import MarketCatalogStore
from parlay_bot.storage.hot import RelationalHotStore


@dataclass(frozen=True)
class _PricedCandidate:
    bookmaker: str
    fixture_id: str
    event_name: str
    market_id: str
    market_name: str
    outcome_id: str
    outcome_name: str
    player_id: str
    player_name: str | None
    line_value: float | None
    line_group_value: float | None
    deeplink: str | None
    price_decimal: float
    price_american: str | None
    fair_probability: float
    price_edge: float
    reference_books: int
    score: float


_RISK_MIN_PROBABILITY = {
    RiskMode.LOWER: 0.58,
    RiskMode.BALANCED: 0.48,
    RiskMode.AGGRESSIVE: 0.35,
    RiskMode.LONGSHOT: 0.22,
}


class ParlayBuilder:
    """Build executable single-book slips from cached, active main-line prices.

    Fair probabilities are bookmaker-consensus no-vig estimates. They are not
    a trained predictive model and are deliberately labeled as market-derived.
    Same-fixture combinations are excluded until RivaL has a correlation model.
    """

    def __init__(
        self,
        *,
        store: RelationalHotStore,
        registry: JurisdictionRegistry,
        catalog: MarketCatalogStore | None = None,
        max_price_age: timedelta = timedelta(minutes=30),
        minimum_reference_books: int = 2,
    ) -> None:
        if max_price_age <= timedelta(0):
            raise ValueError("max_price_age must be positive")
        if minimum_reference_books < 1:
            raise ValueError("minimum_reference_books must be positive")
        self.store = store
        self.registry = registry
        self.catalog = catalog or MarketCatalogStore(store)
        self.max_price_age = max_price_age
        self.minimum_reference_books = minimum_reference_books
        # Counts from the most recent _candidates() run, kept for the UI so
        # a PASS can name the gate that eliminated everything. This is
        # diagnostics only; it never changes which slips are built.
        self.last_diagnostics: dict[str, int] = {}

    @staticmethod
    def _score(*, probability: float, edge: float, price: float, risk: RiskMode) -> float:
        edge_points = max(min(edge, 0.20), -0.10) * 100
        if risk is RiskMode.LOWER:
            return probability * 100 + edge_points * 0.20
        if risk is RiskMode.BALANCED:
            return probability * 70 + edge_points * 0.45
        if risk is RiskMode.AGGRESSIVE:
            return probability * 45 + edge_points * 0.70 + min(price, 4.0) * 2
        return probability * 25 + edge_points * 0.75 + min(price, 8.0) * 4

    def _rows(self, eligible_books: set[str]) -> list[dict]:
        if not eligible_books:
            return []
        self.store.initialize()
        placeholders = ",".join("?" for _ in eligible_books)
        now = datetime.now(UTC)
        freshest = now - self.max_price_age
        with self.store.connect() as connection:
            # A missing start time proves nothing about whether the event
            # has started, so unknown-start fixtures never qualify as
            # pregame picks, even when their odds look attractive.
            rows = connection.execute(
                f"""
                SELECT
                    c.*,
                    f.participant1_name,
                    f.participant2_name,
                    f.sport_name,
                    f.tournament_name
                FROM current_odds AS c
                JOIN fixtures AS f
                  ON f.provider = c.provider AND f.fixture_id = c.fixture_id
                WHERE c.active = 1
                  AND c.main_line = 1
                  AND c.bookmaker IN ({placeholders})
                  AND c.observed_at >= ?
                  AND f.start_time > ?
                """,
                (*sorted(eligible_books), freshest.isoformat(), now.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def _candidates(self, *, eligible_books: set[str], risk: RiskMode) -> list[_PricedCandidate]:
        rows = self._rows(eligible_books)
        diagnostics = {
            "rows_seen": len(rows),
            "single_outcome_groups": 0,
            "invalid_probability": 0,
            "duplicate_vectors": 0,
            "insufficient_reference_books": 0,
            "below_minimum_probability": 0,
            "negative_edge": 0,
            "below_advertised_edge": 0,
            "duplicate_fixture_skips": 0,
            "books_short_of_legs": 0,
        }
        self.last_diagnostics = diagnostics
        if not rows:
            return []

        catalog = self.catalog.metadata()

        by_book_market: dict[
            tuple[str, str, str, str, float | None],
            list[dict],
        ] = defaultdict(list)
        for row in rows:
            key = (
                str(row["bookmaker"]),
                str(row["fixture_id"]),
                str(row["market_id"]),
                str(row["player_id"]),
                (
                    round(float(row["line_group_value"]), 6)
                    if row.get("line_group_value") is not None
                    else None
                ),
            )
            by_book_market[key].append(row)

        probability_samples: dict[
            tuple[str, str, str, str, float | None],
            list[tuple[str, float]],
        ] = defaultdict(list)
        seen_vectors: dict[
            tuple[str, str, str, float | None],
            set[tuple[tuple[str, float], ...]],
        ] = defaultdict(set)

        for (
            _book,
            fixture,
            market_id,
            player_id,
            line_group_value,
        ), group in by_book_market.items():
            if len(group) < 2:
                diagnostics["single_outcome_groups"] += 1
                continue
            ordered = sorted(group, key=lambda row: str(row["outcome_id"]))
            try:
                probabilities = no_vig_probabilities(
                    float(row["price_decimal"]) for row in ordered
                )
            except ValueError:
                diagnostics["invalid_probability"] += 1
                continue

            vector = tuple(
                (str(row["outcome_id"]), round(probability, 8))
                for row, probability in zip(ordered, probabilities, strict=True)
            )
            vector_key = (fixture, market_id, player_id, line_group_value)
            if vector in seen_vectors[vector_key]:
                diagnostics["duplicate_vectors"] += 1
                continue
            seen_vectors[vector_key].add(vector)

            for row, probability in zip(ordered, probabilities, strict=True):
                logical = (
                    fixture,
                    market_id,
                    str(row["outcome_id"]),
                    player_id,
                    line_group_value,
                )
                probability_samples[logical].append((_book, probability))

        candidates: list[_PricedCandidate] = []
        minimum = _RISK_MIN_PROBABILITY[risk]
        for row in rows:
            logical = (
                str(row["fixture_id"]),
                str(row["market_id"]),
                str(row["outcome_id"]),
                str(row["player_id"]),
                (
                    round(float(row["line_group_value"]), 6)
                    if row.get("line_group_value") is not None
                    else None
                ),
            )
            samples = probability_samples.get(logical, [])
            if len(samples) < self.minimum_reference_books:
                diagnostics["insufficient_reference_books"] += 1
                continue

            # Leave-one-book-out benchmark: the candidate's own bookmaker
            # never counts toward its own reference set. With only two
            # configured books the benchmark is the single external book;
            # that is independent, if noisy, and the slip labels the count
            # honestly as external references.
            external = [
                probability for book, probability in samples
                if book != str(row["bookmaker"])
            ]
            if not external:
                diagnostics["insufficient_reference_books"] += 1
                continue
            fair = statistics.median(external)
            if fair < minimum:
                diagnostics["below_minimum_probability"] += 1
                continue

            price = float(row["price_decimal"])
            edge = expected_return_edge(fair_probability=fair, decimal_odds=price)
            if edge < -0.025:
                diagnostics["negative_edge"] += 1
                continue

            market_meta = catalog.get(str(row["market_id"]), {})
            outcome_name = (
                row.get("outcome_name")
                or market_meta.get("outcomes", {}).get(str(row["outcome_id"]))
                or f"Outcome {row['outcome_id']}"
            )
            event_parts = [
                part
                for part in (row["participant1_name"], row["participant2_name"])
                if part
            ]
            event_name = " vs ".join(event_parts) or str(row["fixture_id"])
            candidates.append(
                _PricedCandidate(
                    bookmaker=str(row["bookmaker"]),
                    fixture_id=str(row["fixture_id"]),
                    event_name=event_name,
                    market_id=str(row["market_id"]),
                    market_name=str(
                        row.get("market_name")
                        or market_meta.get("market_name")
                        or f"Market {row['market_id']}"
                    ),
                    outcome_id=str(row["outcome_id"]),
                    outcome_name=str(outcome_name),
                    player_id=str(row["player_id"]),
                    player_name=row["player_name"],
                    line_value=(
                        float(row["line_value"])
                        if row.get("line_value") is not None
                        else None
                    ),
                    line_group_value=(
                        float(row["line_group_value"])
                        if row.get("line_group_value") is not None
                        else None
                    ),
                    deeplink=row.get("deeplink"),
                    price_decimal=price,
                    price_american=row["price_american"],
                    fair_probability=fair,
                    price_edge=edge,
                    reference_books=len(external),
                    score=self._score(
                        probability=fair,
                        edge=edge,
                        price=price,
                        risk=risk,
                    ),
                )
            )
        return candidates

    def build_for_state(
        self,
        *,
        state_code: str,
        leg_count: int = 3,
        risk: RiskMode = RiskMode.BALANCED,
    ) -> ParlaySlip:
        if leg_count < 1 or leg_count > 6:
            raise ValueError("leg_count must be between 1 and 6")

        eligible = set(self.registry.eligible_books(state_code))
        candidates = self._candidates(eligible_books=eligible, risk=risk)
        if leg_count == 1:
            # A profitable-looking price must beat the cross-book benchmark;
            # negative-edge prices are never advertised as single opportunities.
            self.last_diagnostics["below_advertised_edge"] = sum(
                1 for c in candidates if c.price_edge < 0.02
            )
            candidates = [c for c in candidates if c.price_edge >= 0.02]
        else:
            # Preserve single-leg diagnostics; never auto-suggest negative
            # individual prices in multi-leg combinations.
            self.last_diagnostics["nonpositive_multi_leg"] = sum(
                1 for c in candidates if c.price_edge <= 0
            )
            candidates = [c for c in candidates if c.price_edge > 0]
        if not candidates:
            raise ParlayBuildError(
                "No current eligible sportsbook prices pass the fresh positive-edge screen. "
                "PASS rather than force a bet."
            )

        by_book: dict[str, list[_PricedCandidate]] = defaultdict(list)
        for candidate in candidates:
            by_book[candidate.bookmaker].append(candidate)

        slips: list[tuple[float, ParlaySlip]] = []
        for bookmaker, book_candidates in by_book.items():
            selected: list[_PricedCandidate] = []
            used_fixtures: set[str] = set()
            for candidate in sorted(
                book_candidates,
                key=(lambda item: (item.price_edge, item.score))
                if leg_count == 1 else (lambda item: item.score),
                reverse=True,
            ):
                if candidate.fixture_id in used_fixtures:
                    self.last_diagnostics["duplicate_fixture_skips"] += 1
                    continue
                selected.append(candidate)
                used_fixtures.add(candidate.fixture_id)
                if len(selected) == leg_count:
                    break

            if len(selected) != leg_count:
                self.last_diagnostics["books_short_of_legs"] += 1
                continue

            prices = [item.price_decimal for item in selected]
            probabilities = [item.fair_probability for item in selected]
            combined_price = combined_decimal_odds(prices)
            joint_probability = independent_joint_probability(probabilities)
            combined_edge = expected_return_edge(
                fair_probability=joint_probability,
                decimal_odds=combined_price,
            )
            # These are indicative no-vig consensus probabilities, not a
            # calibrated guarantee. Never auto-suggest a losing-price slip.
            if combined_edge <= 0:
                self.last_diagnostics["negative_combined_edge"] = (
                    self.last_diagnostics.get("negative_combined_edge", 0) + 1
                )
                continue

            legs = [
                ParlayLeg(
                    fixture_id=item.fixture_id,
                    event_name=item.event_name,
                    bookmaker=bookmaker,
                    market_id=item.market_id,
                    market_name=item.market_name,
                    outcome_id=item.outcome_id,
                    outcome_name=item.outcome_name,
                    player_id=item.player_id,
                    player_name=item.player_name,
                    line_value=item.line_value,
                    deeplink=item.deeplink,
                    price_decimal=item.price_decimal,
                    price_american=item.price_american,
                    market_fair_probability=item.fair_probability,
                    price_edge=item.price_edge,
                    reference_books=item.reference_books,
                )
                for item in selected
            ]
            slip = ParlaySlip(
                bookmaker=bookmaker,
                risk_mode=risk,
                legs=legs,
                combined_decimal_odds=combined_price,
                market_fair_probability=joint_probability,
                market_implied_edge=combined_edge,
            )
            quality = sum(item.score for item in selected) + combined_edge * 50
            slips.append((quality, slip))

        if not slips:
            raise ParlayBuildError(
                f"RivaL does not have {leg_count} qualifying independent fixtures "
                "at one eligible sportsbook yet."
            )

        return max(slips, key=lambda item: item[0])[1]

    def build_for_user(
        self,
        *,
        platform: str,
        user_id: str,
        leg_count: int = 3,
        risk: RiskMode = RiskMode.BALANCED,
    ) -> ParlaySlip:
        saved = self.store.get_user_jurisdiction(platform=platform, user_id=user_id)
        if saved is None:
            raise ParlayBuildError("Set your betting state before building a slip.")
        return self.build_for_state(
            state_code=str(saved["state_code"]),
            leg_count=leg_count,
            risk=risk,
        )
