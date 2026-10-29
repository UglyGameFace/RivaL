"""Final-score ingestion and provider-ID keyed sport-specific research.

The active sportsbook snapshots are not training labels. Only provider-finalized
events are accepted. Missing team IDs, contradictory scores, and corrections
cannot silently modify original evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from parlay_bot.storage.hot import RelationalHotStore

SUPPORTED = {"SOCCER", "BASKETBALL"}
MAX_SCORES = {"SOCCER": 25, "BASKETBALL": 250}
MODELS = {"SOCCER": "soccer-poisson-home-away-v1",
          "BASKETBALL": "basketball-normal-margin-v1"}


class ResultIntegrityError(ValueError):
    """Source evidence is missing, contradicted, or not final."""


def _instant(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ResultIntegrityError("Invalid fixture start time") from exc
    if stamp.utcoffset() is None:
        raise ResultIntegrityError("Fixture requires timezone")
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class FinalScore:
    event_id: str
    sport: str
    league: str
    home_id: str
    away_id: str
    home_score: int
    away_score: int
    started_at: datetime
    observed_at: datetime
    digest: str

    @property
    def outcome(self) -> int:
        return 1 if self.home_score > self.away_score else (
            -1 if self.home_score < self.away_score else 0
        )


def parse_final_score(event: dict, *, observed_at: datetime) -> FinalScore:
    """Require explicit provider IDs, finalized flag and non-bool integer scores."""
    if observed_at.utcoffset() is None:
        raise ResultIntegrityError("Observation clock must be aware")
    status = event.get("status")
    if not isinstance(status, dict) or status.get("finalized") is not True:
        raise ResultIntegrityError("Event is not provider-finalized")
    if status.get("cancelled") is True or event.get("cancelled") is True:
        raise ResultIntegrityError("Cancelled event cannot be a settled win")
    sport, league = event.get("sportID"), event.get("leagueID")
    if sport not in SUPPORTED or not isinstance(league, str) or not league:
        raise ResultIntegrityError("No qualified sport/league model")
    teams = event.get("teams")
    scores = event.get("scores")
    if sport == "SOCCER":
        # The provider's results.game includes extra time and potentially
        # shootouts. Most 1X2 betting markets settle on regulation only.
        # NEVER train regulation win/draw probabilities on shootout scores.
        results = event.get("results")
        period = results.get("reg") if isinstance(results, dict) else None
        if not isinstance(period, dict):
            raise ResultIntegrityError("Soccer regulation-only scores unavailable")
        home_reg = period.get("home")
        away_reg = period.get("away")
        if not isinstance(home_reg, dict) or not isinstance(away_reg, dict):
            raise ResultIntegrityError("Missing soccer regulation points")
        scores = {
            "home": home_reg.get("points"),
            "away": away_reg.get("points"),
        }
    if not isinstance(teams, dict) or not isinstance(scores, dict):
        raise ResultIntegrityError("Missing final score or team IDs")
    home, away = teams.get("home"), teams.get("away")
    if not isinstance(home, dict) or not isinstance(away, dict):
        raise ResultIntegrityError("Missing team identity")
    home_id, away_id = home.get("teamID"), away.get("teamID")
    if (not isinstance(home_id, str) or not isinstance(away_id, str)
        or not home_id or not away_id or home_id == away_id):
        raise ResultIntegrityError("Unstable home/away team identifiers")
    h, a = scores.get("home"), scores.get("away")
    limit = MAX_SCORES[sport]
    if type(h) is not int or type(a) is not int or not (
        0 <= h <= limit and 0 <= a <= limit
    ):
        raise ResultIntegrityError("Final scores must be bounded integers")
    event_id = event.get("eventID")
    if not isinstance(event_id, str) or not event_id:
        raise ResultIntegrityError("Missing provider event ID")
    start = _instant(str(status.get("startsAt")))
    seen = observed_at.astimezone(UTC)
    if seen < start:
        raise ResultIntegrityError("Final result cannot predate game")
    evidence = {
        "event_id": event_id, "sport": sport, "league": league,
        "home_id": home_id, "away_id": away_id,
        "home_score": h, "away_score": a, "started_at": start.isoformat(),
    }
    digest = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return FinalScore(
        event_id, sport, league, home_id, away_id, h, a,
        start, seen, digest,
    )


def normal_home_win(margin: float, sd: float) -> tuple[float, float]:
    if not math.isfinite(margin) or not math.isfinite(sd) or sd <= 0:
        raise ValueError("Invalid Gaussian model")
    p = 0.5 * (1 + math.erf(margin / (sd * math.sqrt(2))))
    return max(0.0001, min(0.9999, p)), 0.0


def soccer_home_draw_away(home_goals: float, away_goals: float) -> tuple[float, float, float]:
    if not all(math.isfinite(x) and 0 < x <= 6 for x in (home_goals, away_goals)):
        raise ValueError("Invalid Poisson intensities")
    h = [math.exp(-home_goals)]
    a = [math.exp(-away_goals)]
    for i in range(1, 19):
        h.append(h[-1] * home_goals / i)
        a.append(a[-1] * away_goals / i)
    wins = sum(h[i] * a[j] for i in range(19) for j in range(i))
    draws = sum(h[i] * a[i] for i in range(19))
    away = sum(h[i] * a[j] for i in range(19) for j in range(i + 1, 19))
    total = wins + draws + away
    return wins / total, draws / total, away / total


def model_probabilities(
    rows: list[FinalScore], *, sport: str, league: str, home_id: str, away_id: str,
) -> tuple[float, ...] | None:
    """Fixed parsimonious models; no tuning to evaluation results."""
    prior = [r for r in rows if r.sport == sport and r.league == league]
    if len(prior) < 50:
        return None
    home_games = [r for r in prior if r.home_id == home_id]
    away_games = [r for r in prior if r.away_id == away_id]
    if len(home_games) < 6 or len(away_games) < 6:
        return None
    if sport == "SOCCER":
        home_base = (sum(r.home_score for r in prior) + 5) / (len(prior) + 5)
        away_base = (sum(r.away_score for r in prior) + 5) / (len(prior) + 5)
        def shrink(goals: float, count: int, prior_mean: float) -> float:
            return (goals + 6 * prior_mean) / (count + 6)
        offense_h = shrink(sum(r.home_score for r in home_games),
                           len(home_games), home_base)
        defense_a = shrink(sum(r.home_score for r in away_games),
                           len(away_games), home_base)
        offense_a = shrink(sum(r.away_score for r in away_games),
                           len(away_games), away_base)
        defense_h = shrink(sum(r.away_score for r in home_games),
                           len(home_games), away_base)
        h_rate = min(6, max(0.05, offense_h * defense_a / max(home_base, 0.1)))
        a_rate = min(6, max(0.05, offense_a * defense_h / max(away_base, 0.1)))
        return soccer_home_draw_away(h_rate, a_rate)
    if sport == "BASKETBALL":
        margins = [r.home_score - r.away_score for r in prior]
        home_advantage = sum(margins) / len(margins)
        home_form = sum(r.home_score - r.away_score for r in home_games) / len(home_games)
        away_form = sum(r.home_score - r.away_score for r in away_games) / len(away_games)
        expected = (home_form + away_form) / 2
        variance = sum((x - home_advantage) ** 2 for x in margins) / len(margins)
        sd = max(7.0, math.sqrt(variance))
        p, _ = normal_home_win(expected, sd)
        return p, 1 - p
    return None


@dataclass(frozen=True)
class SportsModelEvidence:
    sport: str
    league: str
    model_version: str
    status: str
    train_events: int
    evaluated: int
    evaluated_days: int
    model_brier: float | None
    baseline_brier: float | None
    reason: str


def evaluate_sports_model(
    rows: list[FinalScore], *, sport: str, league: str,
) -> SportsModelEvidence:
    """Historical rolling-origin research, NOT a prospective production edge."""
    if sport not in MODELS:
        raise ValueError("Unsupported sport model")
    group = sorted(
        (x for x in rows if x.sport == sport and x.league == league),
        key=lambda x: (x.started_at, x.event_id),
    )
    n = len(group)
    def fail(reason: str, tested: int = 0, days: int = 0,
             brier: float | None = None, baseline: float | None = None):
        return SportsModelEvidence(
            sport, league, MODELS[sport], "NOT_QUALIFIED",
            n, tested, days, brier, baseline, reason,
        )
    if n < 130:
        return fail("Require at least 130 provider-finalized games in this league")
    scores: list[tuple[str, float, float]] = []
    # Chronological holdout never contributes training examples to its own
    # prediction; unobserved corrections cannot be backdated into training.
    for idx in range(80, n):
        current = group[idx]
        past = [
            x for x in group[:idx]
            if x.observed_at < current.started_at
        ]
        predicted = model_probabilities(
            past, sport=sport, league=league,
            home_id=current.home_id, away_id=current.away_id,
        )
        if predicted is None:
            continue
        label = (
            (1, 0, 0) if current.outcome == 1 else
            (0, 1, 0) if current.outcome == 0 else (0, 0, 1)
        ) if sport == "SOCCER" else (
            (1, 0) if current.outcome == 1 else
            (0, 1) if current.outcome == -1 else None
        )
        if label is None:
            continue
        baseline = [x for x in past if x.sport == sport and x.league == league]
        count = len(predicted)
        mapping = (
            lambda x: 0 if x.outcome == 1 else 1 if x.outcome == 0 else 2
        ) if sport == "SOCCER" else (
            lambda x: 0 if x.outcome == 1 else 1
        )
        counts = [0] * count
        for past_game in baseline:
            if sport == "BASKETBALL" and past_game.outcome == 0:
                continue
            counts[mapping(past_game)] += 1
        base = [(v + 2) / (sum(counts) + 2 * count) for v in counts]
        scores.append((
            current.started_at.date().isoformat(),
            sum((p - y) ** 2 for p, y in zip(predicted, label, strict=True)),
            sum((p - y) ** 2 for p, y in zip(base, label, strict=True)),
        ))
    days = len({x[0] for x in scores})
    if len(scores) < 50 or days < 14:
        return fail("Need 50 chronologically held-out forecasts over 14 dates",
                    len(scores), days)
    model = sum(x[1] for x in scores) / len(scores)
    base = sum(x[2] for x in scores) / len(scores)
    if base - model <= 0.01:
        return fail("No independent Brier improvement over league base rate",
                    len(scores), days, model, base)
    return SportsModelEvidence(
        sport, league, MODELS[sport], "HISTORICAL_SHADOW",
        n, len(scores), days, model, base,
        "Historical rolling-origin Brier gain; not a prospective profitable bet",
    )


class SportsResultStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_sport_results (
                     event_id TEXT PRIMARY KEY,
                     provider TEXT NOT NULL,
                     sport TEXT NOT NULL,
                     league TEXT NOT NULL,
                     home_id TEXT NOT NULL,
                     away_id TEXT NOT NULL,
                     home_score INTEGER NOT NULL,
                     away_score INTEGER NOT NULL,
                     started_at TEXT NOT NULL,
                     first_observed_at TEXT NOT NULL,
                     digest TEXT NOT NULL
                   );
                   CREATE INDEX IF NOT EXISTS idx_rival_sport_result_league
                   ON rival_sport_results (sport,league,started_at);
                   CREATE TABLE IF NOT EXISTS rival_sport_result_checks (
                     event_id TEXT PRIMARY KEY,
                     checked_at TEXT NOT NULL
                   );
                   CREATE TABLE IF NOT EXISTS rival_sport_forecasts (
                     event_id TEXT PRIMARY KEY,
                     sport TEXT NOT NULL,
                     league TEXT NOT NULL,
                     home_id TEXT NOT NULL,
                     away_id TEXT NOT NULL,
                     model_version TEXT NOT NULL,
                     issued_at TEXT NOT NULL,
                     starts_at TEXT NOT NULL,
                     probability_json TEXT NOT NULL,
                     baseline_json TEXT NOT NULL,
                     settled_at TEXT,
                     label INTEGER,
                     model_brier DOUBLE PRECISION,
                     baseline_brier DOUBLE PRECISION
                   );
                   CREATE INDEX IF NOT EXISTS idx_rival_sport_forward_model
                   ON rival_sport_forecasts (sport,league,model_version,settled_at);
                   CREATE TABLE IF NOT EXISTS rival_sport_champions (
                     sport TEXT NOT NULL,
                     league TEXT NOT NULL,
                     model_version TEXT NOT NULL,
                     promoted_at TEXT NOT NULL,
                     disabled_at TEXT,
                     qualification_targets INTEGER NOT NULL,
                     PRIMARY KEY (sport,league,model_version)
                   );"""
            )

    def save_finalized(self, event: dict, *, observed_at: datetime) -> str:
        result = parse_final_score(event, observed_at=observed_at)
        self.initialize()
        with self.store.connect() as conn:
            old = conn.execute(
                "SELECT digest FROM rival_sport_results WHERE event_id=?",
                (result.event_id,),
            ).fetchone()
            if old:
                if old["digest"] != result.digest:
                    raise ResultIntegrityError("Final scores contradict immutable record")
                return "unchanged"
            conn.execute(
                """INSERT INTO rival_sport_results
                   (event_id,provider,sport,league,home_id,away_id,
                    home_score,away_score,started_at,first_observed_at,digest)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (
                    result.event_id, "sportsgameodds",
                    result.sport, result.league, result.home_id, result.away_id,
                    result.home_score, result.away_score,
                    result.started_at.isoformat(),
                    result.observed_at.isoformat(), result.digest,
                ),
            )
        # The external finalized result alone may settle a previously issued
        # prospectively recorded research forecast. Final scores never manufacture
        # a prediction after kickoff.
        self.settle_forecast(result)
        return "inserted"

    def settle_forecast(self, result: FinalScore) -> bool:
        self.initialize()
        with self.store.connect() as conn:
            record = conn.execute(
                "SELECT * FROM rival_sport_forecasts WHERE event_id=?",
                (result.event_id,),
            ).fetchone()
            if record is None or record["settled_at"] is not None:
                return False
            if (
                record["sport"] != result.sport or record["league"] != result.league
                or record["home_id"] != result.home_id
                or record["away_id"] != result.away_id
            ):
                raise ResultIntegrityError("Result identity disagrees with prospective model")
            if _instant(record["issued_at"]) >= result.started_at:
                raise ResultIntegrityError("Forecast was not issued before kickoff")
            prediction = json.loads(record["probability_json"])
            baseline = json.loads(record["baseline_json"])
            label = 0 if result.outcome == 1 else (
                1 if result.outcome == 0 and result.sport == "SOCCER"
                else 2 if result.sport == "SOCCER" else 1
            )
            if result.sport == "BASKETBALL" and result.outcome == 0:
                return False
            if (
                len(prediction) != len(baseline)
                or len(prediction) != (3 if result.sport == "SOCCER" else 2)
            ):
                raise ResultIntegrityError("Incompatible immutable forecast")
            model_loss = sum((float(p) - int(i == label)) ** 2
                             for i, p in enumerate(prediction))
            base_loss = sum((float(p) - int(i == label)) ** 2
                            for i, p in enumerate(baseline))
            changed = conn.execute(
                """UPDATE rival_sport_forecasts
                   SET settled_at=?,label=?,model_brier=?,baseline_brier=?
                   WHERE event_id=? AND settled_at IS NULL""",
                (
                    result.observed_at.isoformat(), label,
                    model_loss, base_loss, result.event_id,
                ),
            )
            return changed.rowcount == 1

    def issue_forecast(
        self, event: dict, *, observed_at: datetime,
    ) -> str:
        """Only immutable pregame forecasts with qualified *historical* evidence."""
        if observed_at.utcoffset() is None:
            raise ValueError("Timestamp must have UTC offset")
        status = event.get("status")
        if not isinstance(status, dict) or (
            status.get("started") is True or status.get("cancelled") is True
            or status.get("finalized") is True
        ):
            return "not_pregame"
        sport, league = event.get("sportID"), event.get("leagueID")
        if sport not in SUPPORTED or not isinstance(league, str) or not league:
            return "unsupported"
        teams = event.get("teams")
        if not isinstance(teams, dict):
            return "missing_identity"
        home, away = teams.get("home"), teams.get("away")
        if not isinstance(home, dict) or not isinstance(away, dict):
            return "missing_identity"
        home_id, away_id = home.get("teamID"), away.get("teamID")
        if (not isinstance(home_id, str) or not isinstance(away_id, str)
            or not home_id or not away_id or home_id == away_id):
            return "missing_identity"
        event_id = event.get("eventID")
        if not isinstance(event_id, str) or not event_id:
            return "missing_identity"
        try:
            start = _instant(str(status.get("startsAt")))
        except ResultIntegrityError:
            return "missing_start"
        stamp = observed_at.astimezone(UTC)
        if stamp >= start:
            return "started"
        self.initialize()
        with self.store.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM rival_sport_forecasts WHERE event_id=?",
                (event_id,),
            ).fetchone():
                return "already_issued"
        prior = [
            x for x in self.all_results(sport=sport, league=league)
            if x.started_at < stamp and x.observed_at < stamp
        ]
        if evaluate_sports_model(prior, sport=sport, league=league).status != (
            "HISTORICAL_SHADOW"
        ):
            return "not_qualified"
        probability = model_probabilities(
            prior, sport=sport, league=league, home_id=home_id, away_id=away_id,
        )
        if probability is None:
            return "not_enough_team_history"
        labels = 3 if sport == "SOCCER" else 2
        counts = [2] * labels
        for result in prior:
            if result.outcome == 1:
                counts[0] += 1
            elif result.outcome == -1:
                counts[-1] += 1
            elif sport == "SOCCER":
                counts[1] += 1
        baseline = tuple(x / sum(counts) for x in counts)
        with self.store.connect() as conn:
            result = conn.execute(
                """INSERT INTO rival_sport_forecasts
                   (event_id,sport,league,home_id,away_id,model_version,
                    issued_at,starts_at,probability_json,baseline_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (
                    event_id, sport, league, home_id, away_id, MODELS[sport],
                    stamp.isoformat(), start.isoformat(),
                    json.dumps(probability), json.dumps(baseline),
                ),
            )
        return "issued" if result.rowcount == 1 else "already_issued"

    def promotion_status(self, *, sport: str, league: str,
                         now: datetime | None = None) -> dict:
        """Automatic research-only champion/rollback from real forward ledger."""
        if sport not in SUPPORTED or not league:
            raise ValueError("Invalid model promotion query")
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("Naive promotion clock")
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT starts_at,model_brier,baseline_brier
                   FROM rival_sport_forecasts
                   WHERE sport=? AND league=? AND model_version=?
                     AND settled_at IS NOT NULL
                   ORDER BY starts_at DESC LIMIT 500""",
                (sport, league, MODELS[sport]),
            ).fetchall()
            active = conn.execute(
                """SELECT * FROM rival_sport_champions
                   WHERE sport=? AND league=? AND model_version=?
                     AND disabled_at IS NULL""",
                (sport, league, MODELS[sport]),
            ).fetchone()
        unique_days = len({
            _instant(str(x["starts_at"])).date().isoformat() for x in rows
        })
        gain = (
            sum(float(x["baseline_brier"]) - float(x["model_brier"])
                for x in rows) / len(rows)
            if rows else None
        )
        qualified = len(rows) >= 100 and unique_days >= 14 and (
            gain is not None and gain >= 0.01
        )
        # An existing shadow champion can be revoked automatically when
        # its actual prospective performance deteriorates.
        action = "NOT_QUALIFIED"
        with self.store.connect() as conn:
            if active is not None and gain is not None and gain <= 0:
                conn.execute(
                    """UPDATE rival_sport_champions
                       SET disabled_at=? WHERE sport=? AND league=?
                       AND model_version=? AND disabled_at IS NULL""",
                    (stamp.astimezone(UTC).isoformat(), sport, league, MODELS[sport]),
                )
                action = "REVOKED"
            elif active is not None:
                action = "SHADOW_CHAMPION"
            elif qualified:
                conn.execute(
                    """INSERT INTO rival_sport_champions
                       (sport,league,model_version,promoted_at,disabled_at,
                        qualification_targets) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(sport,league,model_version) DO UPDATE SET
                       promoted_at=excluded.promoted_at,
                       disabled_at=NULL,
                       qualification_targets=excluded.qualification_targets""",
                    (
                        sport, league, MODELS[sport],
                        stamp.astimezone(UTC).isoformat(), None, len(rows),
                    ),
                )
                action = "SHADOW_CHAMPION"
        return {
            "status": action,
            "settled_unique_events": len(rows),
            "utc_days": unique_days,
            "mean_paired_brier_gain": gain,
            "live_trading_approved": False,
            "production_inference_changed": False,
        }

    def all_results(self, *, sport: str, league: str, limit: int = 2000) -> list[FinalScore]:
        if sport not in SUPPORTED or not league or not 1 <= limit <= 5000:
            raise ValueError("Invalid sport history query")
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_sport_results WHERE sport=? AND league=?
                   ORDER BY started_at DESC LIMIT ?""",
                (sport, league, limit),
            ).fetchall()
        return [FinalScore(
            x["event_id"], x["sport"], x["league"],
            x["home_id"], x["away_id"], int(x["home_score"]),
            int(x["away_score"]), _instant(x["started_at"]),
            _instant(x["first_observed_at"]), x["digest"],
        ) for x in reversed(rows)]

    def evaluate(self, *, sport: str, league: str) -> SportsModelEvidence:
        return evaluate_sports_model(self.all_results(sport=sport, league=league),
                                     sport=sport, league=league)

    def due_fixtures(self, *, now: datetime, limit: int = 8) -> list[str]:
        """Only previously ingested, actually started games, with 6h retry."""
        if not 1 <= limit <= 20:
            raise ValueError("Result request budget exceeded")
        self.initialize()
        cutoff = (now - timedelta(hours=6)).isoformat()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT f.fixture_id FROM fixtures AS f
                   LEFT JOIN rival_sport_results AS r ON r.event_id=f.fixture_id
                   LEFT JOIN rival_sport_result_checks AS p
                     ON p.event_id=f.fixture_id
                   WHERE f.provider='sportsgameodds'
                     AND f.sport_id IN ('SOCCER','BASKETBALL')
                     AND f.start_time IS NOT NULL AND f.start_time<?
                     AND r.event_id IS NULL
                     AND (p.checked_at IS NULL OR p.checked_at<=?)
                   ORDER BY p.checked_at ASC,f.start_time DESC LIMIT ?""",
                (now.isoformat(), cutoff, limit),
            ).fetchall()
        return [str(r["fixture_id"]) for r in rows]

    def mark_checked(self, ids: list[str], *, now: datetime) -> None:
        if not ids or len(ids) > 20:
            raise ValueError("Invalid result poll batch")
        self.initialize()
        with self.store.connect() as conn:
            for event_id in ids:
                conn.execute(
                    """INSERT INTO rival_sport_result_checks(event_id,checked_at)
                       VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET
                       checked_at=excluded.checked_at""",
                    (event_id, now.isoformat()),
                )
