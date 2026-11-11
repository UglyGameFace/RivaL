"""The Model Lab: RivaL searches for better models on its own.

Until now every test model was written by hand. Early stop cut the losers,
but nothing took their place, and only 5-minute calls ever got new ideas.
The lab closes that loop:

- **Slots.** Each timeframe (5, 15, 60 min) has ``SLOTS_PER_HORIZON`` lab
  slots. Each slot holds one *design* (a "genome"): which inputs it reads,
  whether it adds a few interaction terms, how fast it learns, how hard
  it is pulled back to zero, which market condition it trades in, how far
  it trusts its own confidence, and where its weights start.
- **Live learning, no look-ahead.** A design is an online logistic model
  that learns per exchange stream from graded outcomes only (labels known
  before ``now``), exactly like the main model's own learner, and issues
  test calls under its own family name (``lab_<h>m_<sha>``). Its calls are
  graded against the same future candles as every other model, by the
  same ledger, early stop and Route B promotion check.
- **Review.** Every ``REVIEW_EVERY`` the lab scores each active design
  across all exchanges (daily blocks, so correlated exchanges are not
  counted as independent) and closes it when it is clearly losing, cut by
  early stop on most of its exchanges, or still has no edge after
  ``NO_EDGE_AFTER``. A design that passes Route B is kept and no longer
  holds a slot.
- **Search.** A free slot is refilled by a design the lab has never tried:
  usually a small change to one of the best designs so far on that
  timeframe, sometimes a fresh one. Choices are weighted by what has
  worked on that timeframe (each setting's average result), so the lab
  learns which settings suit 5, 15 and 60 minutes.
- **No lucky winners.** The more designs tried, the more likely one looks
  good by chance. A lab design must also clear ``search_bar``: the same
  daily-block lower bound as Route B with a stricter z for the number of
  designs tried on that timeframe (a Sidak-style correction).

The lab never trades, never changes a gate for any other model, and a lab
design that passes is a research result: serving it still goes through
``FamilyPromotion.selected``, which does not serve lab designs yet.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import statistics
import threading
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.regime_gate import ER_THRESHOLD, ER_WINDOW, efficiency_ratio

_LOG = logging.getLogger(__name__)

PREFIX = "lab_"
VERSION = 1
HORIZONS = (5, 15, 60)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
# Two per timeframe to start: each design adds a row per stream and cycle to
# the shared ledger, and settlement has a 20 s budget per cycle. Raise it
# once the forecast-cycle timing log shows settlement keeping up.
SLOTS_PER_HORIZON = 2

# The 10 live features (features.feature_vector), grouped so a design reads
# whole ideas rather than single columns.
FEATURE_NAMES = ("last move", "window move", "volatility", "bar range", "volume spike",
                 "RSI", "MACD", "Bollinger", "ATR", "volume regime")
GROUPS = {
    "momentum": (0, 1),
    "volatility": (2, 3, 8),
    "volume": (4, 9),
    "oscillators": (5, 6, 7),
}
GROUP_ORDER = tuple(GROUPS)
# Three products of existing inputs (clamped like every live feature):
# momentum x volatility, last move x volume spike, Bollinger x RSI. Each is
# used only when both of its groups are read.
INTERACTIONS = ((1, 2), (0, 4), (7, 5))
WIDTH = 1 + 10 + len(INTERACTIONS)


def _input_sets() -> tuple[tuple[str, ...], ...]:
    out = []
    for mask in range(1, 1 << len(GROUP_ORDER)):
        out.append(tuple(g for i, g in enumerate(GROUP_ORDER) if mask >> i & 1))
    return tuple(out)


SPACE: dict[str, tuple] = {
    "inputs": _input_sets(),
    "shape": ("linear", "interact"),
    "lr": (0.01, 0.025, 0.05, 0.1),
    "l2": (0.0001, 0.001, 0.01),
    "regime": ("any", "trend", "quiet"),
    "shrink": (0.5, 0.75, 1.0),
    "warm": ("main", "zero"),
}
DIMENSIONS = tuple(SPACE)

# Plain starting ideas for an empty timeframe: an all-rounder, a quiet-market
# mean-reversion idea, and a trend follower.
SEEDS = (
    {"inputs": GROUP_ORDER, "shape": "interact", "lr": 0.025, "l2": 0.001,
     "regime": "any", "shrink": 0.75, "warm": "main"},
    {"inputs": ("volume", "oscillators"), "shape": "linear", "lr": 0.05, "l2": 0.001,
     "regime": "quiet", "shrink": 0.75, "warm": "zero"},
    {"inputs": ("momentum", "volatility"), "shape": "interact", "lr": 0.05, "l2": 0.0001,
     "regime": "trend", "shrink": 1.0, "warm": "main"},
)

EXPLORE = 0.25            # share of refills that are fresh designs, not changes
REVIEW_EVERY = timedelta(minutes=30)
JUDGE_MIN_RESULTS = 300   # graded calls across exchanges before a verdict
JUDGE_MIN_DAYS = 2
LOSING_Z = 2.0            # clearly losing: upper bound below zero
NO_EDGE_AFTER = timedelta(days=5)
MAX_AGE = timedelta(days=35)
CUT_SHARE = 0.5           # early stop on at least this share of its exchanges
SEARCH_ALPHA = 0.005      # Route B's 2.58 one-sided bar, split across designs

GENOME_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_lab_genomes (
    family TEXT PRIMARY KEY, horizon_minutes INTEGER NOT NULL,
    genome_json TEXT NOT NULL, parent TEXT, status TEXT NOT NULL,
    opened_at TEXT NOT NULL, closed_at TEXT, reason TEXT,
    results INTEGER NOT NULL DEFAULT 0, days INTEGER NOT NULL DEFAULT 0,
    gain DOUBLE PRECISION, fitness DOUBLE PRECISION, reviewed_at TEXT)"""
LEARNER_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_lab_learners (
    venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL, family TEXT NOT NULL,
    weights_json TEXT NOT NULL, cursor TEXT, updates INTEGER NOT NULL,
    trained_at TEXT NOT NULL,
    PRIMARY KEY (venue,contract,quote,horizon_minutes,family))"""


# --- designs ------------------------------------------------------------------

def canonical(genome: dict, horizon: int) -> dict:
    """The stored design: every dimension, its timeframe and the lab version."""
    out = {"version": VERSION, "horizon": int(horizon)}
    for dim in DIMENSIONS:
        value = genome[dim]
        if dim == "inputs":
            value = tuple(g for g in GROUP_ORDER if g in value)
        if value not in SPACE[dim]:
            raise ValueError(f"lab design {dim}={value!r} is outside the search space")
        out[dim] = list(value) if dim == "inputs" else value
    return out


def family_of(genome: dict, horizon: int) -> str:
    doc = canonical(genome, horizon)
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"))
                            .encode()).hexdigest()
    return f"{PREFIX}{int(horizon)}m_{digest[:10]}"


def is_lab(family: str) -> bool:
    return str(family).startswith(PREFIX)


def describe(genome: dict) -> str:
    """One plain sentence about a design."""
    inputs = list(genome["inputs"])
    reads = ", ".join(inputs[:-1]) + (" and " if len(inputs) > 1 else "") + inputs[-1]
    when = {"any": "every market", "trend": "trending markets only",
            "quiet": "choppy (non-trending) markets only"}[genome["regime"]]
    speed = {0.01: "slowly", 0.025: "steadily", 0.05: "quickly", 0.1: "very quickly"}
    trust = {0.5: "halves its confidence", 0.75: "trims its confidence",
             1.0: "uses its full confidence"}[genome["shrink"]]
    extra = " plus a few combined signals" if genome["shape"] == "interact" else ""
    start = "starting from the main model's weights" if genome["warm"] == "main" else \
        "starting from scratch"
    return (f"Reads {reads}{extra}; calls {when}; learns {speed[genome['lr']]} "
            f"{start}; {trust}.")


def _vector(genome: dict, features) -> tuple[float, ...]:
    on = {i for g in genome["inputs"] for i in GROUPS[g]}
    base = [float(v) if i in on else 0.0 for i, v in enumerate(features)]
    extra = []
    for a, b in INTERACTIONS:
        if genome["shape"] == "interact" and a in on and b in on:
            extra.append(max(-5.0, min(5.0, base[a] * base[b])))
        else:
            extra.append(0.0)
    return (1.0, *base, *extra)


def _sigmoid(value: float) -> float:
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _regime_of(er: float | None) -> str | None:
    if er is None:
        return None
    return "trend" if er > ER_THRESHOLD else "quiet"


def _matches(genome: dict, er: float | None) -> bool:
    if genome["regime"] == "any":
        return True
    return _regime_of(er) == genome["regime"]


def _er_index(candles):
    """issued_at -> efficiency ratio at that bar (same rule as trending_samples)."""
    ordered = sorted(candles, key=lambda c: utc(c.opened_at))
    closes = [c.close_price for c in ordered]
    observed = [utc(c.observed_at) for c in ordered]
    by_observed = {stamp: idx for idx, stamp in enumerate(observed)}

    def lookup(issued_at) -> float | None:
        idx = by_observed.get(utc(issued_at))
        if idx is None:
            earlier = [i for i, s in enumerate(observed) if s <= utc(issued_at)]
            if not earlier:
                return None
            idx = earlier[-1]
        if idx < ER_WINDOW:
            return None
        return efficiency_ratio(closes[idx - ER_WINDOW:idx + 1])

    current = (efficiency_ratio(closes[-(ER_WINDOW + 1):])
               if len(closes) > ER_WINDOW else None)
    return lookup, current


# --- the live learners -----------------------------------------------------------

class LabLearners:
    """One small online logistic per (stream, design), kept in memory and in
    ``rival_lab_learners`` (a few hundred bytes per row, written only when it
    learned something)."""

    def __init__(self, store) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._ready = False
        self._states: dict[tuple, dict] = {}
        self._loaded: set[tuple] = set()

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(LEARNER_SCHEMA)
        self._ready = True

    def _load(self, stream: tuple, families: list[str]) -> None:
        key = stream
        if key in self._loaded:
            return
        venue, contract, quote, horizon = stream
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT family,weights_json,cursor,updates FROM rival_lab_learners
                   WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?""",
                (venue, contract, quote, horizon)).fetchall()
        for row in rows:
            try:
                weights = [float(w) for w in json.loads(row["weights_json"])]
                if len(weights) != WIDTH or not all(math.isfinite(w) for w in weights):
                    continue
                cursor = json.loads(row["cursor"]) if row["cursor"] else None
                self._states[(*stream, str(row["family"]))] = {
                    "weights": weights, "cursor": cursor,
                    "updates": int(row["updates"]), "dirty": False}
            except (TypeError, ValueError):
                continue
        self._loaded.add(key)

    def forget(self, families) -> None:
        """Drop closed designs from memory (their rows stay as a record)."""
        drop = set(families)
        with self._lock:
            for key in [k for k in self._states if k[-1] in drop]:
                self._states.pop(key, None)

    def calls(self, *, genomes: dict[str, dict], candles, samples, features,
              incumbent_weights, venue, contract, quote, horizon, now,
              stopped=frozenset()) -> list[dict]:
        """Train each design on newly graded labels, then make its call.

        Returns challenger items in the shape ShadowModelCompetition.issue
        accepts. A design whose market condition does not match the current
        bar abstains (no row), like the trend models do.
        """
        live = {f: g for f, g in genomes.items() if f not in stopped}
        if not live:
            return []
        self.initialize()
        stream = (venue, contract, quote, int(horizon))
        lookup, current_er = _er_index(candles)
        ordered = sorted(samples, key=lambda s: (utc(s.label_known_at), utc(s.issued_at)))
        stamp = utc(now)
        out = []
        with self._lock:
            self._load(stream, list(live))
            er_cache: dict = {}
            for family, genome in live.items():
                key = (*stream, family)
                state = self._states.get(key)
                if state is None:
                    weights = [0.0] * WIDTH
                    if genome["warm"] == "main" and incumbent_weights is not None \
                            and len(incumbent_weights) == 11:
                        on = {i for g in genome["inputs"] for i in GROUPS[g]}
                        weights[0] = float(incumbent_weights[0])
                        for i in range(10):
                            if i in on:
                                weights[1 + i] = float(incumbent_weights[1 + i])
                    state = {"weights": weights, "cursor": None, "updates": 0, "dirty": True}
                    self._states[key] = state
                cursor = tuple(state["cursor"]) if state["cursor"] else None
                weights = state["weights"]
                lr, l2 = float(genome["lr"]), float(genome["l2"])
                for sample in ordered:
                    known, issued = utc(sample.label_known_at), utc(sample.issued_at)
                    mark = (known.isoformat(), issued.isoformat())
                    if known > stamp or (cursor is not None and mark <= cursor):
                        continue
                    cursor = mark
                    if genome["regime"] != "any":
                        if issued not in er_cache:
                            er_cache[issued] = lookup(issued)
                        if not _matches(genome, er_cache[issued]):
                            continue
                    x = _vector(genome, sample.features)
                    error = sample.label_up - _sigmoid(sum(w * v for w, v in zip(weights, x)))
                    for i, value in enumerate(x):
                        weights[i] += lr * (error * value - (l2 * weights[i] if i else 0.0))
                    state["updates"] += 1
                if cursor != (tuple(state["cursor"]) if state["cursor"] else None):
                    state["cursor"] = list(cursor) if cursor else None
                    state["dirty"] = True
                if not all(math.isfinite(w) for w in weights):
                    # A diverged learner restarts rather than issuing nonsense.
                    self._states.pop(key, None)
                    continue
                if state["updates"] < 20 or not _matches(genome, current_er):
                    continue
                p = _sigmoid(sum(w * v for w, v in zip(weights, _vector(genome, features))))
                p = 0.5 + float(genome["shrink"]) * (p - 0.5)
                if not 0 < p < 1:
                    continue
                out.append({
                    "family": family, "probability": min(0.999, max(0.001, p)),
                    "training_rows": int(state["updates"]),
                    "model_sha256": hashlib.sha256(json.dumps(
                        [round(w, 12) for w in weights]).encode()).hexdigest(),
                })
        self._last_stamp = stamp
        return out

    def flush(self) -> int:
        """Save every learner that learned since the last flush, on ONE
        connection (the forecast cycle calls this once at its end).

        Until then learning lives in memory; after a crash a learner resumes
        from its last saved cursor and relearns the same graded labels,
        which gives the same weights.
        """
        with self._lock:
            dirty = [(key, state) for key, state in self._states.items() if state["dirty"]]
        if not dirty:
            return 0
        stamp = getattr(self, "_last_stamp", None) or datetime.now(UTC)
        try:
            with self.store.connect() as conn:
                for (venue, contract, quote, horizon, family), state in dirty:
                    conn.execute(
                        """INSERT INTO rival_lab_learners (venue,contract,quote,horizon_minutes,
                             family,weights_json,cursor,updates,trained_at)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT (venue,contract,quote,horizon_minutes,family) DO UPDATE SET
                             weights_json=excluded.weights_json, cursor=excluded.cursor,
                             updates=excluded.updates, trained_at=excluded.trained_at""",
                        (venue, contract, quote, horizon, family,
                         json.dumps([round(w, 12) for w in state["weights"]]),
                         json.dumps(state["cursor"]) if state["cursor"] else None,
                         int(state["updates"]), stamp.isoformat()))
            with self._lock:
                for _key, state in dirty:
                    state["dirty"] = False
            return len(dirty)
        except Exception:  # noqa: BLE001 - learning stays in memory; saved next cycle
            _LOG.warning("RivaL lab learner save failed; kept in memory", exc_info=True)
            return 0


# --- the search ------------------------------------------------------------------

def _weights_for(dim: str, scored: list[dict], exclude=None) -> list[tuple[object, float]]:
    """Each value of one setting, weighted by how designs with it have done.

    Unseen values get the best seen average, so the lab keeps trying them.
    """
    by_value: dict[object, list[float]] = {}
    for record in scored:
        value = record["genome"][dim]
        value = tuple(value) if isinstance(value, list) else value
        by_value.setdefault(value, []).append(record["fitness"])
    means = {v: statistics.mean(f) for v, f in by_value.items()}
    top = max(means.values(), default=0.0)
    low = min(means.values(), default=0.0)
    spread = max((top - low) / 2, 1e-4)
    out = []
    for value in SPACE[dim]:
        if exclude is not None and value == exclude:
            continue
        mean = means.get(value, top)
        out.append((value, math.exp((mean - top) / spread)))
    return out


def _pick(rng: random.Random, weighted: list[tuple[object, float]]):
    total = sum(w for _, w in weighted)
    mark = rng.random() * total
    for value, weight in weighted:
        mark -= weight
        if mark <= 0:
            return value
    return weighted[-1][0]


def _plain(genome: dict) -> dict:
    out = {dim: genome[dim] for dim in DIMENSIONS}
    out["inputs"] = tuple(out["inputs"])
    return out


def propose(horizon: int, tried: list[dict], rng: random.Random) -> tuple[dict, str | None] | None:
    """A design never tried on this timeframe, and the design it came from.

    ``tried`` holds every design opened on this timeframe: {"family",
    "genome", "fitness" (None until judged)}. Seeds come first; then mostly
    a one- or two-setting change to one of the three best designs, and a
    fresh design weighted by what has worked about ``EXPLORE`` of the time.
    """
    seen = {r["family"] for r in tried}
    for seed in SEEDS:
        if family_of(seed, horizon) not in seen:
            return dict(seed), None
    scored = [r for r in tried if r.get("fitness") is not None]
    # Build on the best designs: those beating the main model if any are,
    # otherwise the least bad.
    winners = [r for r in scored if r["fitness"] > 0] or scored
    best = sorted(winners, key=lambda r: -r["fitness"])[:3]
    for _attempt in range(200):
        if not best or rng.random() < EXPLORE:
            genome = {dim: _pick(rng, _weights_for(dim, scored)) for dim in DIMENSIONS}
            parent = None
        else:
            source = rng.choice(best)
            genome = _plain(source["genome"])
            for dim in rng.sample(DIMENSIONS, rng.choice((1, 1, 2))):
                genome[dim] = _pick(rng, _weights_for(dim, scored, exclude=genome[dim]))
            parent = source["family"]
        if family_of(genome, horizon) not in seen:
            return genome, parent
    return None


def search_bar(pairs, *, tried: int) -> dict:
    """Route B's daily-block lower bound with a z raised for designs tried.

    ``pairs`` are (target ISO, model Brier, main-model Brier). One-sided
    alpha ``SEARCH_ALPHA`` (Route B's 2.58) is split across ``tried``
    designs (Sidak), so trying more designs needs stronger evidence.
    """
    tried = max(1, int(tried))
    alpha = 1 - (1 - SEARCH_ALPHA) ** (1 / tried)
    z = statistics.NormalDist().inv_cdf(1 - alpha)
    by_target: dict[str, list[float]] = {}
    for target, model, main in pairs:
        by_target.setdefault(str(target), []).append(float(main) - float(model))
    per_day: dict[str, list[float]] = {}
    for target, diffs in by_target.items():
        per_day.setdefault(target[:10], []).append(sum(diffs) / len(diffs))
    daily = [sum(v) / len(v) for v in per_day.values()]
    if len(daily) < 2:
        return {"z": round(z, 3), "tried": tried, "lower": None, "passed": False}
    lower = statistics.mean(daily) - z * statistics.stdev(daily) / math.sqrt(len(daily))
    return {"z": round(z, 3), "tried": tried, "lower": lower, "passed": lower > 0}


# --- the lab ------------------------------------------------------------------------

_CLOSED: set[str] = set()
_TRIED: dict[int, int] = {}
_GENOMES: dict[str, dict] = {}


def describe_family(family: str) -> str | None:
    """The plain sentence for a lab design this process has loaded."""
    genome = _GENOMES.get(str(family))
    return describe(genome) if genome else None


def closed_families() -> frozenset[str]:
    """Lab designs that were closed (never shown as on trial again)."""
    return frozenset(_CLOSED)


def tried_count(horizon: int, store=None) -> int:
    """Designs ever opened on this timeframe (at least 1).

    Read from the table when this process has not loaded the lab yet, so
    the stricter bar never silently falls back to a single design.
    """
    if int(horizon) in _TRIED or store is None:
        return max(1, _TRIED.get(int(horizon), 1))
    try:
        with store.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_lab_genomes WHERE horizon_minutes=?",
                (int(horizon),)).fetchone()
        return max(1, int(row["n"]))
    except Exception:  # noqa: BLE001 - unknown count: assume a large search
        return 1000


class ModelLab:
    """Owns the design table, the live learners and the review/refill step."""

    def __init__(self, store) -> None:
        self.store = store
        self.learners = LabLearners(store)
        self._ready = False
        self._active: dict[int, dict[str, dict]] = {}
        self._active_at: datetime | None = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(GENOME_SCHEMA)
        self.learners.initialize()
        self._ready = True

    def _rows(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM rival_lab_genomes").fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["genome"] = json.loads(record["genome_json"])
            except (TypeError, ValueError):
                continue
            out.append(record)
        return out

    def _remember(self, rows: list[dict]) -> None:
        active: dict[int, dict[str, dict]] = {h: {} for h in HORIZONS}
        tried: dict[int, int] = {}
        closed = set()
        for r in rows:
            horizon = int(r["horizon_minutes"])
            tried[horizon] = tried.get(horizon, 0) + 1
            if r["status"] in ("active", "passed"):
                active.setdefault(horizon, {})[r["family"]] = _plain(r["genome"])
            else:
                closed.add(r["family"])
        with self._lock:
            self._active = active
            self._active_at = datetime.now(UTC)
        _CLOSED.clear()
        _CLOSED.update(closed)
        _TRIED.clear()
        _TRIED.update(tried)
        _GENOMES.clear()
        _GENOMES.update({r["family"]: _plain(r["genome"]) for r in rows})

    def active(self, horizon: int) -> dict[str, dict]:
        """Designs issuing on this timeframe (refreshed at most every minute)."""
        with self._lock:
            fresh = (self._active_at is not None
                     and datetime.now(UTC) - self._active_at < timedelta(minutes=1))
            if fresh:
                return dict(self._active.get(int(horizon), {}))
        try:
            self._remember(self._rows())
        except Exception:  # noqa: BLE001 - no lab this cycle, never a crash
            _LOG.warning("RivaL lab designs unavailable this cycle", exc_info=True)
            with self._lock:
                self._active_at = datetime.now(UTC)
        with self._lock:
            return dict(self._active.get(int(horizon), {}))

    def calls(self, **kwargs) -> list[dict]:
        genomes = self.active(int(kwargs["horizon"]))
        if not genomes:
            return []
        return self.learners.calls(genomes=genomes, **kwargs)

    def flush(self) -> int:
        return self.learners.flush()

    # -- review ------------------------------------------------------------------

    def _scores(self, families: list[str], horizon: int, since: str) -> dict[str, dict]:
        """Per design: daily paired gains (main Brier - design Brier) and streams."""
        if not families:
            return {}
        marks = ",".join("?" for _ in families)
        days: dict[str, dict[str, list[float]]] = {f: {} for f in families}
        streams: dict[str, set] = {f: set() for f in families}
        from parlay_bot.storage.optional_reads import is_missing_table, optional_rows

        for asset in ASSETS:
            rows = optional_rows(
                self.store,
                f"""SELECT family, venue, contract, SUBSTR(target_opened_at,1,10) AS day,
                              COUNT(*) AS n, SUM(incumbent_brier-model_brier) AS gain
                       FROM rival_model_shadow_forecasts
                       WHERE asset=? AND horizon_minutes=? AND family IN ({marks})
                         AND issued_at>=? AND settled_at IS NOT NULL
                         AND model_brier IS NOT NULL AND incumbent_brier IS NOT NULL
                       GROUP BY family, venue, contract, SUBSTR(target_opened_at,1,10)""",
                (asset, int(horizon), *families, since), what="model lab score read")
            if rows is None:
                # No ledger yet is "no results"; any other failure means
                # this timeframe is not judged this round (never closed blind).
                try:
                    with self.store.connect() as conn:
                        conn.execute("SELECT 1 FROM rival_model_shadow_forecasts LIMIT 1")
                except Exception as error:  # noqa: BLE001
                    if is_missing_table(error):
                        continue
                return None
            for r in rows:
                family = str(r["family"])
                cell = days[family].setdefault(str(r["day"]), [0, 0.0])
                cell[0] += int(r["n"])
                cell[1] += float(r["gain"] or 0.0)
                streams[family].add((str(r["venue"]), str(r["contract"])))
        out = {}
        for family in families:
            daily = [g / n for n, g in days[family].values() if n]
            results = sum(n for n, _ in days[family].values())
            mean = statistics.mean(daily) if daily else None
            se = (statistics.stdev(daily) / math.sqrt(len(daily))
                  if len(daily) > 1 else None)
            fitness = (mean * len(daily) / (len(daily) + 3)) if mean is not None else None
            out[family] = {"results": results, "days": len(daily), "gain": mean, "se": se,
                           "fitness": fitness if results >= JUDGE_MIN_RESULTS // 3 else None,
                           "streams": streams[family]}
        return out

    def _cut(self, families: list[str]) -> dict[str, set]:
        if not families:
            return {}
        marks = ",".join("?" for _ in families)
        try:
            with self.store.connect() as conn:
                rows = conn.execute(
                    f"SELECT family, venue, contract FROM rival_family_early_stop "
                    f"WHERE family IN ({marks})", tuple(families)).fetchall()
        except Exception:  # noqa: BLE001 - no early-stop table yet
            return {}
        out: dict[str, set] = {}
        for r in rows:
            out.setdefault(str(r["family"]), set()).add((str(r["venue"]), str(r["contract"])))
        return out

    def _passed(self, families: list[str]) -> set[str]:
        if not families:
            return set()
        try:
            with self.store.connect() as conn:
                rows = conn.execute(
                    "SELECT evidence_json FROM rival_family_promotion").fetchall()
        except Exception:  # noqa: BLE001 - no promotion check yet
            return set()
        wanted, out = set(families), set()
        for r in rows:
            try:
                evidence = json.loads(r["evidence_json"] or "{}")
            except (TypeError, ValueError):
                continue
            for family in wanted & set(evidence if isinstance(evidence, dict) else ()):
                if (evidence[family] or {}).get("qualified"):
                    out.add(family)
        return out

    @staticmethod
    def verdict(record: dict, score: dict, cut: set, *, now: datetime) -> str | None:
        """Why to close an active design now, or None to keep it running."""
        age = now - utc(datetime.fromisoformat(record["opened_at"]))
        streams = score.get("streams") or set()
        if streams and len(cut) >= max(1, math.ceil(len(streams) * CUT_SHARE)):
            return "cut by early stop on most exchanges"
        gain, se = score.get("gain"), score.get("se")
        judged = (score.get("results", 0) >= JUDGE_MIN_RESULTS
                  and score.get("days", 0) >= JUDGE_MIN_DAYS and gain is not None
                  and se is not None)
        if judged and gain + LOSING_Z * se < 0:
            return "clearly losing to the main model"
        if age >= NO_EDGE_AFTER and (not judged or gain - se <= 0):
            return "no edge after 5 days"
        if age >= MAX_AGE:
            return "trial finished without passing"
        return None

    def review(self, now: datetime | None = None) -> dict:
        """Score, close and refill. Safe to call often; returns what changed."""
        stamp = utc(now or datetime.now(UTC))
        rows = self._rows()
        changed = {"closed": [], "opened": [], "passed": []}
        for horizon in HORIZONS:
            here = [r for r in rows if int(r["horizon_minutes"]) == horizon]
            running = [r for r in here if r["status"] in ("active", "passed")]
            families = [r["family"] for r in running]
            oldest = min((r["opened_at"] for r in running), default=stamp.isoformat())
            scores = self._scores(families, horizon, oldest)
            if scores is None:
                continue
            cuts = self._cut(families)
            passed = self._passed(families)
            with self.store.connect() as conn:
                for r in running:
                    score = scores.get(r["family"], {})
                    if r["status"] == "active" and r["family"] in passed:
                        r["status"] = "passed"
                        changed["passed"].append(r["family"])
                    reason = (None if r["status"] == "passed"
                              else self.verdict(r, score, cuts.get(r["family"], set()), now=stamp))
                    if reason:
                        r["status"] = "closed"
                        changed["closed"].append((r["family"], reason))
                    r["fitness"] = score.get("fitness", r.get("fitness"))
                    conn.execute(
                        """UPDATE rival_lab_genomes SET status=?, closed_at=?, reason=?,
                             results=?, days=?, gain=?, fitness=?, reviewed_at=?
                           WHERE family=?""",
                        (r["status"], stamp.isoformat() if reason else r.get("closed_at"),
                         reason or r.get("reason"), int(score.get("results", 0)),
                         int(score.get("days", 0)), score.get("gain"), r["fitness"],
                         stamp.isoformat(), r["family"]))
            open_slots = SLOTS_PER_HORIZON - sum(r["status"] == "active" for r in here)
            rng = random.Random(f"rival-lab:{horizon}:{len(here)}")
            for _ in range(max(0, open_slots)):
                tried = [{"family": r["family"], "genome": r["genome"],
                          "fitness": r.get("fitness")} for r in here]
                pick = propose(horizon, tried, rng)
                if pick is None:
                    break
                genome, parent = pick
                family = family_of(genome, horizon)
                doc = canonical(genome, horizon)
                with self.store.connect() as conn:
                    conn.execute(
                        """INSERT INTO rival_lab_genomes (family,horizon_minutes,genome_json,
                             parent,status,opened_at) VALUES (?,?,?,?,?,?)
                           ON CONFLICT (family) DO NOTHING""",
                        (family, horizon, json.dumps(doc, sort_keys=True), parent,
                         "active", stamp.isoformat()))
                here.append({"family": family, "horizon_minutes": horizon, "genome": doc,
                             "status": "active", "opened_at": stamp.isoformat(),
                             "fitness": None})
                changed["opened"].append(family)
        closed = [f for f, _ in changed["closed"]]
        if closed:
            self.learners.forget(closed)
        self._remember(self._rows())
        for family, reason in changed["closed"]:
            _LOG.info("RivaL lab closed %s: %s", family, reason)
        for family in changed["opened"]:
            _LOG.info("RivaL lab opened %s", family)
        return changed

    # -- what the lab has learned ---------------------------------------------------

    def summary(self) -> dict[int, dict]:
        """Per timeframe: designs, the best so far, and which settings work."""
        rows = self._rows()
        out = {}
        for horizon in HORIZONS:
            here = [r for r in rows if int(r["horizon_minutes"]) == horizon]
            scored = [{"family": r["family"], "genome": r["genome"],
                       "fitness": float(r["fitness"])}
                      for r in here if r.get("fitness") is not None]
            learned = {}
            for dim in DIMENSIONS:
                by_value: dict = {}
                for s in scored:
                    value = s["genome"][dim]
                    value = tuple(value) if isinstance(value, list) else value
                    by_value.setdefault(value, []).append(s["fitness"])
                if by_value:
                    value, fits = max(by_value.items(), key=lambda kv: statistics.mean(kv[1]))
                    learned[dim] = {"value": value, "mean": statistics.mean(fits),
                                    "designs": len(fits)}
            # "Best" is among designs still running: a closed design is history.
            running = {r["family"] for r in here if r["status"] in ("active", "passed")}
            best = max((s for s in scored if s["family"] in running),
                       key=lambda s: s["fitness"], default=None)
            out[horizon] = {
                "tried": len(here),
                "active": [r for r in here if r["status"] == "active"],
                "passed": [r for r in here if r["status"] == "passed"],
                "closed": sum(r["status"] == "closed" for r in here),
                "best": best, "learned": learned,
            }
        return out
