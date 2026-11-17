"""Source-exact, always-on paper TP1/TP2-before-stop forecasts.

This is an ADDITIVE research ledger, never an order/filled trade. Training
uses closed source candles known at issuance. Future labels are matched to
the same market and issued time, with first-observation proof. Five-minute
OHLC ambiguity excludes the path rather than inventing a profitable fill.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import statistics
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.first_passage import trace_barriers
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.governance import score_paired_forward

VERSION = "prospective-empirical-path-v1"
TARGET_VERSION = "prospective-empirical-close-targets-v1"
HORIZONS = (5, 15, 60)
SIDES = ("long", "short")
LOOKBACK = 24
MIN_TRAIN = 25
MAX_TRAIN = 280
# Source paths, each with at most the two declared sides. A normal cycle
# selects at most 40 streams x 3 horizons: 120 paths / 240 side records.
# Batching 100 side records cannot keep up even without missing evidence.
MAX_SETTLE = 100
# Readiness counting fetches candle windows in chunks of this many path
# keys (5 SQL parameters per key). The correlated COUNT(*) this replaced
# evaluated one subquery per candidate row; the chunked batched fetch
# keeps every statement small while selection still stops as soon as
# MAX_SETTLE ready keys are found.
_READY_COUNT_CHUNK = 200
# Give current outcomes 80 of the 100-path bound while retaining 20 slots
# for older evidence. Unused capacity is shared. This is scheduling only:
# all paths retain the same 14-day and causal source gates. A full
# 3-horizon cycle can issue 120 paths, so current outcomes may need a
# second settle to drain; the backlog stays bounded (see the full-cycle
# test in tests/test_prospective_path_forward.py).
CURRENT_SETTLEMENT_WINDOW = timedelta(hours=2)


def _take_split(current_list, backlog_list, total):
    """Take up to ``total`` paths keeping the current/backlog split.

    Current outcomes take ``total`` minus a 20% reserve for older
    evidence (at least 1 when ``total`` > 1); the backlog fills the
    rest. Returns (taken, used_current, used_backlog) so a caller can
    continue taking from the same canonical order afterwards.
    """
    reserved = max(1, total // 5) if total > 1 else 0
    current_limit = total - min(reserved, len(backlog_list))
    take_current = min(current_limit, len(current_list))
    take_backlog = min(total - take_current, len(backlog_list))
    return (current_list[:take_current] + backlog_list[:take_backlog],
            take_current, take_backlog)


def _batched_candles(conn, keys):
    """Fetch settlement candles for many path keys in one round-trip.

    ``keys`` maps each distinct path key to its (reference, target).
    Returns {key: [rows]} where each key's rows are exactly what the
    old per-key query returned for it: the same six columns, the same
    ``opened_at > reference+5m AND opened_at <= target`` predicate,
    the same opened_at order. The single query ORs one branch per key
    with the identical predicate; rows are distributed back in Python
    by testing that predicate, so a row matching several overlapping
    keys (same stream, different horizons) is handed to each of them,
    exactly as the per-key queries would each have returned it.
    At most MAX_SETTLE keys are ever selected, so the parameter count
    stays bounded (5 per key).
    """
    if not keys:
        return {}
    branches = []
    params = []
    for (venue, contract, quote, _horizon, _ref, _issued, _target), (reference, target) in keys.items():
        branches.append(
            "(venue=? AND contract=? AND quote=? AND interval_minutes=5"
            " AND opened_at>? AND opened_at<=?)")
        params.extend([venue, contract, quote,
                       (reference + timedelta(minutes=5)).isoformat(),
                       target.isoformat()])
    rows = conn.execute(
        """SELECT venue,contract,quote,opened_at,closed_at,first_observed_at,
                  high_price,low_price,close_price FROM perp_market_candles
           WHERE """ + " OR ".join(branches) +
        " ORDER BY venue,contract,quote,opened_at",
        params,
    ).fetchall()
    by_group = {}
    for key, (reference, target) in keys.items():
        by_group.setdefault(key[:3], []).append((
            key, (reference + timedelta(minutes=5)).isoformat(),
            target.isoformat()))
    out = {key: [] for key in keys}
    for row in rows:
        for key, low, high in by_group.get(
                (row["venue"], row["contract"], row["quote"]), ()):
            if low < row["opened_at"] <= high:
                out[key].append(row)
    for bars in out.values():
        bars.sort(key=lambda bar: bar["opened_at"])
    return out


def _batched_window_counts(conn, keys, stamp_s):
    """Count valid settlement candles per path key, in one round-trip.

    ``keys`` is a list of path keys in canonical order; ``stamp_s`` is
    the settle stamp as an ISO string. Returns {key: count} where count
    is the number of 5-minute candles in the key's readiness window
    (opened_at in (issued_at, target_opened_at]) that also pass the
    first-observation checks: first_observed_at in (issued_at, stamp],
    closed_at < stamp, closed_at <= first_observed_at. A key is ready
    when its count equals horizon_minutes // 5.

    The SQL carries only primary-key terms (venue/contract/quote/
    interval_minutes/opened_at range). Adding first_observed_at terms
    makes SQLite's planner abandon the candle primary key for the
    (interval_minutes, first_observed_at) index and scan every bar
    first-observed in the window instead of the ~1-12 bars in it, so
    the validity checks run here in Python. The comparisons are plain
    ISO-string comparisons, exactly what SQLite does for these TEXT
    columns, so the counts match the old correlated COUNT(*) row for
    row. A row matching several overlapping keys is counted for each
    of them, exactly as the per-key subqueries would each have
    counted it.
    """
    counts = {}
    if not keys:
        return counts
    branches = []
    params = []
    for key in keys:
        branches.append(
            "(venue=? AND contract=? AND quote=? AND interval_minutes=5"
            " AND opened_at>? AND opened_at<=?)")
        params.extend([key[0], key[1], key[2], key[5], key[6]])
    rows = conn.execute(
        """SELECT venue,contract,quote,opened_at,closed_at,first_observed_at
           FROM perp_market_candles
           WHERE """ + " OR ".join(branches),
        params,
    ).fetchall()
    by_group = {}
    for key in keys:
        by_group.setdefault(key[:3], []).append(key)
    for row in rows:
        for key in by_group.get(
                (row["venue"], row["contract"], row["quote"]), ()):
            issued_at, target = key[5], key[6]
            if (issued_at < row["opened_at"] <= target
                    and issued_at < row["first_observed_at"] <= stamp_s
                    and row["closed_at"] < stamp_s
                    and row["closed_at"] <= row["first_observed_at"]):
                counts[key] = counts.get(key, 0) + 1
    return counts


def _sigma(candles, index: int, steps: int) -> float:
    if index < LOOKBACK:
        raise ValueError("not enough causal volatility observations")
    closes = [float(c.close_price) for c in candles[index-LOOKBACK:index+1]]
    if any(not math.isfinite(v) or v <= 0 for v in closes):
        raise ValueError("invalid prior prices")
    returns = [math.log(b/a) for a,b in itertools.pairwise(closes)]
    return max(.0001, min(.25, statistics.pstdev(returns)*math.sqrt(steps)))


def prune_path_memo(memo: dict, candles) -> None:
    """Drop samples whose volatility window starts before this read."""
    if not candles:
        memo.clear()
        return
    first = candles[0].opened_at
    for key in [key for key in memo if key[0] < first]:
        del memo[key]


def _reference_samples(candles, index: int, steps: int, ahead) -> tuple:
    """(side, sample) pairs for one matured reference bar; () if unusable."""
    try:
        vol = _sigma(candles, index, steps)
    except ValueError:
        return ()
    reference = candles[index]
    entry = float(reference.close_price)
    movement = math.log(entry / float(candles[index-3].close_price))
    bars = [{"high": c.high_price, "low": c.low_price} for c in ahead]
    out = []
    absolute_return = abs(math.log(float(ahead[-1].close_price)/entry))
    # First confirmed CLOSE crossing. This is NOT an intrabar high/low
    # estimate or an executable first-passage time.
    first_close_hit = next((
        j for j, future in enumerate(ahead, 1)
        if abs(math.log(float(future.close_price)/entry)) >= vol
    ), None)
    early_move = int(first_close_hit is not None
                     and first_close_hit <= (steps + 1)//2)
    for side in SIDES:
        flags = trace_barriers(
            entry=entry, sigma_horizon=vol, side=side, future=bars,
        )
        sign = 1 if side == "long" else -1
        realized = sign*math.log(float(ahead[-1].close_price)/entry)
        out.append((side, (
            utc(reference.opened_at).isoformat(),
            bool(sign*movement >= 0),
            flags[0] if flags is not None else None,
            flags[1] if flags is not None else None,
            realized, int(realized > 0), early_move, absolute_return,
        )))
    return tuple(out)


def _smoothed_close_probability(history, cohort, index):
    """Shrink a conditional event rate to its same-source historical prior."""
    base = (sum(row[index] for row in history) + 2) / (len(history) + 4)
    candidate = (sum(row[index] for row in cohort) + 16 * base) / (
        len(cohort) + 16)
    return candidate, base


def _quantile(values, p):
    sequence = sorted(values)
    at = (len(sequence)-1)*p
    lower, upper = math.floor(at), math.ceil(at)
    return sequence[lower]*(upper-at) + sequence[upper]*(at-lower) if upper != lower else sequence[lower]


def empirical_paths(candles, *, horizon: int, now: datetime, memo: dict | None = None):
    """Both sides' source-specific priors from past matured NONOVERLAP paths.

    The aligned/trend bucket is fixed using only information known at each
    reference bar, not hindsight labels or market regimes selected after test.
    Historical periods with late first observations are not *forward proof*,
    but may train a prior after their data is actually known.

    ``memo``: pass the same dict each cycle for ONE stream and horizon. A
    reference bar's samples depend only on the bars from its volatility window
    to the end of its path, which never change once stored; they are keyed by
    the first and last of those bars, so the 1,000-bar read can slide forward
    without recomputing them. The reference grid is a ``steps``-wide stride
    from the start of the read, so a bar recurs every ``steps`` cycles: keep
    the dict whole and trim it with prune_path_memo(). The causality checks
    below still run every call.
    """
    if horizon not in HORIZONS:
        return {}
    instant = utc(now)
    steps = horizon//5
    if len(candles) < LOOKBACK+steps+MIN_TRAIN*steps+2:
        return {}
    previous = None
    for c in candles:
        if (utc(c.observed_at) > instant
                or int(c.interval_minutes) != 5
                or (previous is not None and
                    (c.venue,c.contract,c.quote) !=
                    (previous.venue,previous.contract,previous.quote))):
            raise ValueError("future or cross-market path input")
        previous = c
    latest = len(candles)-1
    side_samples = {side: [] for side in SIDES}
    close_samples = {side: [] for side in SIDES}
    # A step-wide grid avoids training on 12 copies of one 60m outcome.
    # Skip the first candle opening alongside the reference close: the
    # real worker issues seconds AFTER that bar opens and cannot prove
    # what happened inside the bar before issuance.
    for index in range(LOOKBACK, latest-steps, steps):
        reference = candles[index]
        ahead = candles[index+2:index+steps+2]
        if any(utc(item.opened_at) != utc(reference.opened_at)+timedelta(minutes=5*(j+2))
               or utc(item.closed_at) >= instant or utc(item.observed_at) > instant
               for j,item in enumerate(ahead)):
            continue
        key = (candles[index-LOOKBACK].opened_at, reference.opened_at,
               ahead[-1].opened_at if ahead else None, steps)
        if memo is not None and key in memo:
            samples = memo[key]
        else:
            samples = _reference_samples(candles, index, steps, ahead)
            if memo is not None:
                memo[key] = samples
        for side, sample in samples:
            close_samples[side].append(sample)
            # Intrabar TP/stop ambiguity must not train the barrier model,
            # but it cannot erase an otherwise known closing-price label.
            if sample[2] is not None:
                side_samples[side].append(sample)
    if not all(len(rows) >= MIN_TRAIN for rows in side_samples.values()):
        return {}
    current_movement = math.log(
        float(candles[-1].close_price)/float(candles[-4].close_price),
    )
    current_sigma = _sigma(candles,latest,steps)
    result = {}
    for side, history in side_samples.items():
        history = history[-MAX_TRAIN:]
        sign = 1 if side=="long" else -1
        aligned = bool(sign*current_movement >= 0)
        matching = [row for row in history if row[1]==aligned]
        if len(matching) < 12:
            matching = history
        # Honest baseline: unconditional source history known at issuance.
        baseline = [(sum(row[index] for row in history)+2)/(len(history)+4)
                    for index in (2,3)]
        probabilities = [
            (sum(row[index] for row in matching)+16*base)/(len(matching)+16)
            for index,base in zip((2,3),baseline,strict=True)
        ]
        # Nested events must remain nested after conservative shrinkage.
        probabilities[1] = min(probabilities[0],probabilities[1])
        signed_returns = [row[4] for row in matching]
        quantiles = [_quantile(signed_returns,p) for p in (.1,.5,.9)]
        digest = hashlib.sha256(json.dumps(
            [row[:5] for row in history],
            sort_keys=True, separators=(",",":"), allow_nan=False,
        ).encode()).hexdigest()
        close_history = close_samples[side][-MAX_TRAIN:]
        close_matching = [row for row in close_history if row[1] == aligned]
        if len(close_matching) < 12:
            close_matching = close_history
        # Predeclared smoothing against an unconditional stream baseline.
        # Both outcomes are independently scored (Brier) against the same
        # source/time labels; no selection on test results.
        direction_p, direction_base = _smoothed_close_probability(
            close_history, close_matching, 5)
        early_p, early_base = _smoothed_close_probability(
            close_history, close_matching, 6)
        size_p50 = _quantile([row[7] for row in close_matching], .5)
        size_base = _quantile([row[7] for row in close_history], .5)
        result[side] = {
            "sigma": current_sigma,
            "tp1_probability": probabilities[0],
            "tp2_probability": probabilities[1],
            "tp1_baseline": baseline[0],
            "tp2_baseline": baseline[1],
            "p10": quantiles[0], "p50": quantiles[1], "p90": quantiles[2],
            "training_count": len(history), "cohort_count": len(matching),
            "training_sha256": digest,
            # Standalone immutable close-only targets: no same-candle fill
            # assumption, no duplicated TP/stop evidence.
            "direction_probability": direction_p,
            "direction_baseline": direction_base,
            "early_move_probability": early_p,
            "early_move_baseline": early_base,
            "absolute_move_p50": size_p50,
            "absolute_move_baseline": size_base,
            "close_target_training_count": len(close_history),
            "close_target_training_sha256": hashlib.sha256(json.dumps(
                close_history, sort_keys=True, separators=(",",":"),
                allow_nan=False,
            ).encode()).hexdigest(),
        }
    return result


class ProspectivePathLedger:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS rival_forward_paths (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                asset TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
                model_version TEXT NOT NULL,side TEXT NOT NULL,
                reference_opened_at TEXT NOT NULL,issued_at TEXT NOT NULL,
                target_opened_at TEXT NOT NULL,reference_close DOUBLE PRECISION NOT NULL,
                sigma_horizon DOUBLE PRECISION NOT NULL,
                tp1_probability DOUBLE PRECISION NOT NULL,
                tp2_probability DOUBLE PRECISION NOT NULL,
                tp1_baseline DOUBLE PRECISION NOT NULL,
                tp2_baseline DOUBLE PRECISION NOT NULL,
                signed_return_p10 DOUBLE PRECISION NOT NULL,
                signed_return_p50 DOUBLE PRECISION NOT NULL,
                signed_return_p90 DOUBLE PRECISION NOT NULL,
                training_count INTEGER NOT NULL,cohort_count INTEGER NOT NULL,
                training_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,settled_at TEXT,
                tp1_before_stop INTEGER,tp2_before_stop INTEGER,
                realized_signed_return DOUBLE PRECISION,
                PRIMARY KEY(venue,contract,quote,horizon_minutes,side,reference_opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_rival_forward_paths_pending
                ON rival_forward_paths(state,target_opened_at);
            CREATE INDEX IF NOT EXISTS idx_rival_forward_paths_score
                ON rival_forward_paths(asset,horizon_minutes,side,issued_at);
            CREATE TABLE IF NOT EXISTS rival_forward_path_targets (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                asset TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
                model_version TEXT NOT NULL,side TEXT NOT NULL,
                reference_opened_at TEXT NOT NULL,issued_at TEXT NOT NULL,
                target_opened_at TEXT NOT NULL,reference_close DOUBLE PRECISION NOT NULL,
                sigma_horizon DOUBLE PRECISION NOT NULL,
                direction_probability DOUBLE PRECISION NOT NULL,
                direction_baseline DOUBLE PRECISION NOT NULL,
                early_move_probability DOUBLE PRECISION NOT NULL,
                early_move_baseline DOUBLE PRECISION NOT NULL,
                absolute_move_p50 DOUBLE PRECISION NOT NULL,
                absolute_move_baseline DOUBLE PRECISION NOT NULL,
                close_target_training_count INTEGER NOT NULL,
                close_target_training_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,settled_at TEXT,
                direction_positive INTEGER,early_move_by_half INTEGER,
                absolute_move_realized DOUBLE PRECISION,
                first_close_hit_bars INTEGER,
                PRIMARY KEY(venue,contract,quote,horizon_minutes,side,reference_opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_rival_path_targets_pending
                ON rival_forward_path_targets(state,target_opened_at);
            CREATE INDEX IF NOT EXISTS idx_rival_path_targets_score
                ON rival_forward_path_targets(asset,horizon_minutes,side,issued_at);
            """)

    def issue(self, conn, *, candles, asset, horizon, now, issued_at, options=None):
        """Insert both sides' path forecasts. ``options``: empirical_paths()
        for the same candles, horizon and ``now``, computed before the caller's
        transaction opened; computed here when not given (same result)."""
        if horizon not in HORIZONS:
            return 0
        now,issued_at=utc(now),utc(issued_at)
        last=candles[-1]
        if (issued_at < utc(last.observed_at)
                or issued_at >= utc(last.opened_at)+timedelta(minutes=10)):
            return 0
        if options is None:
            options=empirical_paths(candles,horizon=horizon,now=now)
        if not options:
            return 0
        # The path starts with the next complete candle AFTER issuance
        # (reference open +10m). Never score the partially elapsed bar.
        target=utc(last.opened_at)+timedelta(minutes=horizon+5)
        count=0
        for side,sample in options.items():
            result=conn.execute(
                """INSERT INTO rival_forward_paths
                 (venue,contract,quote,asset,horizon_minutes,model_version,side,
                  reference_opened_at,issued_at,target_opened_at,reference_close,
                  sigma_horizon,tp1_probability,tp2_probability,tp1_baseline,
                  tp2_baseline,signed_return_p10,signed_return_p50,
                  signed_return_p90,training_count,cohort_count,training_sha256,state)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT DO NOTHING""",
                (last.venue,last.contract,last.quote,asset,horizon,VERSION,side,
                 utc(last.opened_at).isoformat(),issued_at.isoformat(),target.isoformat(),
                 last.close_price,sample["sigma"],sample["tp1_probability"],
                 sample["tp2_probability"],sample["tp1_baseline"],
                 sample["tp2_baseline"],sample["p10"],sample["p50"],sample["p90"],
                 sample["training_count"],sample["cohort_count"],
                 sample["training_sha256"],"pending"),
            )
            if result.rowcount == 1:
                # Only issue a companion task for a genuinely NEW prospect.
                # No hindsight backfill for previously issued path forecasts.
                conn.execute(
                    """INSERT INTO rival_forward_path_targets (
                         venue,contract,quote,asset,horizon_minutes,
                         model_version,side,reference_opened_at,issued_at,
                         target_opened_at,reference_close,sigma_horizon,
                         direction_probability,direction_baseline,
                         early_move_probability,early_move_baseline,
                         absolute_move_p50,absolute_move_baseline,
                         close_target_training_count,close_target_training_sha256,state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (last.venue,last.contract,last.quote,asset,horizon,TARGET_VERSION,side,
                     utc(last.opened_at).isoformat(),issued_at.isoformat(),
                     target.isoformat(),last.close_price,sample["sigma"],
                     sample["direction_probability"],sample["direction_baseline"],
                     sample["early_move_probability"],sample["early_move_baseline"],
                     sample["absolute_move_p50"],sample["absolute_move_baseline"],
                     sample["close_target_training_count"],
                     sample["close_target_training_sha256"],"pending"),
                )
            count+=int(result.rowcount==1)
        return count

    def _ready_paths(self, conn, *, stamp: datetime, current: bool,
                     horizons=(15, 60), limit=MAX_SETTLE) -> list:
        """Read at most ``limit`` source paths from one disjoint age queue.

        ``horizons`` restricts which horizons may be selected. Settlement
        itself is horizon-generic (one complete 5-minute candle per 5
        minutes of horizon); callers give 5-minute paths a guaranteed
        reserve of MAX_SETTLE//5 paths per settle call, with either
        horizon group able to use the other's unused slots, so a
        faster-due horizon can never displace a longer-horizon path
        while the longer queue still has ready work.

        Readiness is two-phase, not a correlated COUNT(*) per candidate
        row: one indexed scan reads the candidate keys in canonical
        order, then _batched_window_counts counts valid candles per
        key in chunks, stopping as soon as ``limit`` ready keys are
        found, and one row-value IN query fetches their side rows.
        The selection (keys, order, outcomes) is identical to the old
        per-row subquery — the subquery only ever proved that a key's
        window holds exactly horizon/5 valid bars, which is what the
        batched counts compute. The correlated form was dropped
        because the GROUP BY forces a full pass over every pending row
        in the 14-day window, and once first_observed_at terms are
        present SQLite's planner reads the candles through the
        (interval_minutes, first_observed_at) index instead of the
        primary key: on a 2,000-unready-row fixture the backlog scan
        alone took ~880ms; the two-phase form takes ~11ms for the
        same selection.
        """
        if limit <= 0:
            return []
        comparison = ">=" if current else "<"
        direction = "DESC" if current else "ASC"
        placeholders = ",".join("?" for _ in horizons)
        stamp_s = stamp.isoformat()
        rows = conn.execute(
                f"""SELECT DISTINCT p.venue,p.contract,p.quote,p.horizon_minutes,
                       p.reference_opened_at,p.issued_at,p.target_opened_at
                   FROM rival_forward_paths p
                   WHERE p.state='pending' AND p.target_opened_at<=?
                    AND p.target_opened_at>=? AND p.horizon_minutes IN ({placeholders})
                    AND p.target_opened_at {comparison} ?
                    AND p.side IN ('long','short')
                   ORDER BY p.target_opened_at {direction},p.venue,p.contract,p.quote,
                            p.horizon_minutes,p.reference_opened_at,p.issued_at""",
                ((stamp-timedelta(minutes=5)).isoformat(),
                 (stamp-timedelta(days=14)).isoformat(),
                 *horizons,
                 (stamp-CURRENT_SETTLEMENT_WINDOW).isoformat()),
            ).fetchall()
        key_fields = ("venue", "contract", "quote", "horizon_minutes",
                      "reference_opened_at", "issued_at", "target_opened_at")
        keys = [tuple(row[field] for field in key_fields) for row in rows]
        ready = []
        for start in range(0, len(keys), _READY_COUNT_CHUNK):
            chunk = keys[start:start + _READY_COUNT_CHUNK]
            counts = _batched_window_counts(conn, chunk, stamp_s)
            for key in chunk:
                if counts.get(key, 0) == key[3] // 5:
                    ready.append(key)
                    if len(ready) == limit:
                        break
            if len(ready) == limit:
                break
        if not ready:
            return []
        in_placeholders = ",".join("(?,?,?,?,?,?,?)" for _ in ready)
        params = [value for key in ready for value in key]
        rows = conn.execute(
                f"""SELECT p.* FROM rival_forward_paths p
                   WHERE p.state='pending' AND p.side IN ('long','short')
                     AND (p.venue,p.contract,p.quote,p.horizon_minutes,
                          p.reference_opened_at,p.issued_at,p.target_opened_at)
                         IN ({in_placeholders})
                   ORDER BY p.target_opened_at {direction},p.venue,p.contract,p.quote,
                            p.horizon_minutes,p.reference_opened_at,p.issued_at,p.side""",
                params,
            ).fetchall()
        paths = {}
        for row in rows:
            key = tuple(row[field] for field in key_fields)
            paths.setdefault(key, []).append(row)
        return list(paths.values())

    def settle(self, *, now: datetime) -> dict:
        """Settle current and older paper paths with bounded, shared capacity."""
        stamp=utc(now)
        self.initialize()
        with self.store.connect() as conn:
            # Both SELECTs filter complete, already-observed exact-source
            # paths before their bound. Older missing evidence stays pending.
            # Oldest-first alone can spend every cycle on pre-companion
            # forecasts for days while current close targets never score.
            current = self._ready_paths(conn, stamp=stamp, current=True)
            backlog = self._ready_paths(conn, stamp=stamp, current=False)
            current5 = self._ready_paths(
                conn, stamp=stamp, current=True, horizons=(5,), limit=MAX_SETTLE)
            backlog5 = self._ready_paths(
                conn, stamp=stamp, current=False, horizons=(5,), limit=MAX_SETTLE)
            # Five-minute paths hold a guaranteed reserve of MAX_SETTLE//5
            # paths per settle call (20 at the 100-path bound), taken
            # before the 15/60-minute queues fill the rest. A full cycle
            # issues 40 five-minute paths (80 sides), so a 20-path reserve
            # settles half the 5m flow (~40 sides/cycle, ~11.5k sides/day)
            # — far above the ~7 settled/scope/day a scope needs for the
            # 150-paired bar at 80% coverage — while 15/60 keep 80% of
            # capacity against their ~107k due queue. Either horizon group
            # may use the other's unused slots (see the slack step below),
            # so a drained queue never strands capacity. The 5m readiness
            # scans are new work per cycle, but the 5m pending queue is
            # small next to the 15/60 queue, so they stay cheap.
            five_min_reserve = MAX_SETTLE // 5
            selected5, used_current5, used_backlog5 = _take_split(
                current5, backlog5, five_min_reserve)
            selected, _, _ = _take_split(
                current, backlog, MAX_SETTLE - len(selected5))
            slack = MAX_SETTLE - len(selected) - len(selected5)
            if slack > 0:
                # 15/60 had nothing more ready; let 5m continue in its
                # canonical current/backlog order instead of stranding
                # slots. This keeps the old leftover behaviour for small
                # caps (a MAX_SETTLE of 4 has a zero reserve yet still
                # drains 5m once 15/60 are done).
                rest5 = current5[used_current5:] + backlog5[used_backlog5:]
                selected5 += rest5[:slack]
            selected += selected5
            waiting = [row for path in selected for row in path]
            scored=ambiguous=0
            # Collect the distinct candle keys first: timing validity
            # depends only on the row itself, never on the candles, so
            # one batched fetch serves every selected key and the scoring
            # loop below runs exactly as it did with per-key fetches.
            keys={}
            for p in waiting:
                target=utc(datetime.fromisoformat(p["target_opened_at"]))
                reference=utc(datetime.fromisoformat(p["reference_opened_at"]))
                issued=utc(datetime.fromisoformat(p["issued_at"]))
                steps=int(p["horizon_minutes"])//5
                if not (issued < reference+timedelta(minutes=10) and issued<=stamp
                        and target==reference+timedelta(minutes=5*(steps+1))):
                    continue
                key=(p["venue"],p["contract"],p["quote"],p["horizon_minutes"],
                     p["reference_opened_at"],p["issued_at"],p["target_opened_at"])
                keys.setdefault(key,(reference,target))
            paths=_batched_candles(conn,keys)
            for p in waiting:
                target=utc(datetime.fromisoformat(p["target_opened_at"]))
                reference=utc(datetime.fromisoformat(p["reference_opened_at"]))
                issued=utc(datetime.fromisoformat(p["issued_at"]))
                steps=int(p["horizon_minutes"])//5
                if not (issued < reference+timedelta(minutes=10) and issued<=stamp
                        and target==reference+timedelta(minutes=5*(steps+1))):
                    continue
                key=(p["venue"],p["contract"],p["quote"],p["horizon_minutes"],
                     p["reference_opened_at"],p["issued_at"],p["target_opened_at"])
                bars=paths[key]
                if len(bars)!=steps:
                    continue
                try:
                    valid=all(
                        utc(datetime.fromisoformat(b["opened_at"])) ==
                            reference+timedelta(minutes=5*(j+2))
                        and issued < utc(datetime.fromisoformat(
                            b["first_observed_at"])) <= stamp
                        and utc(datetime.fromisoformat(b["closed_at"])) < stamp
                        and utc(datetime.fromisoformat(b["closed_at"])) <=
                            utc(datetime.fromisoformat(b["first_observed_at"]))
                        for j,b in enumerate(bars)
                    )
                    if not valid:
                        continue
                    result=trace_barriers(
                        entry=float(p["reference_close"]),
                        sigma_horizon=float(p["sigma_horizon"]),
                        side=str(p["side"]),
                        future=[{"high":b["high_price"],"low":b["low_price"]}
                                for b in bars],
                    )
                    terminal=float(bars[-1]["close_price"])
                    signed=(1 if p["side"]=="long" else -1)*math.log(
                        terminal/float(p["reference_close"]),
                    )
                    if not math.isfinite(signed):
                        continue
                except (ValueError,TypeError,OverflowError,ZeroDivisionError):
                    continue
                state="ambiguous" if result is None else "settled"
                # Closing-price labels are independent of TP/stop intrabar
                # ordering. Every future close still must have passed the
                # immutable exact-source/first-observation checks above.
                entry=float(p["reference_close"])
                sigma=float(p["sigma_horizon"])
                closes=[float(b["close_price"]) for b in bars]
                if not all(math.isfinite(v) and v > 0 for v in closes):
                    continue
                first_close_hit=next((
                    j for j, close in enumerate(closes, 1)
                    if abs(math.log(close/entry)) >= sigma
                ), None)
                early=int(first_close_hit is not None
                          and first_close_hit <= (steps+1)//2)
                updated=conn.execute(
                    """UPDATE rival_forward_paths SET
                       state=?,settled_at=?,tp1_before_stop=?,
                       tp2_before_stop=?,realized_signed_return=?
                       WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?
                         AND side=? AND reference_opened_at=? AND state='pending'""",
                    (state,stamp.isoformat(),
                     result[0] if result is not None else None,
                     result[1] if result is not None else None,
                     signed,p["venue"],p["contract"],p["quote"],
                     p["horizon_minutes"],p["side"],p["reference_opened_at"]),
                )
                if updated.rowcount==1:
                    # No late/backfilled forecast insertion: UPDATE is limited
                    # to rows frozen at issuance. Ambiguous OHLC is allowed
                    # for independent closing-price labels only.
                    conn.execute(
                        """UPDATE rival_forward_path_targets SET
                             state='settled',settled_at=?,
                             direction_positive=?,early_move_by_half=?,
                             absolute_move_realized=?,first_close_hit_bars=?
                           WHERE venue=? AND contract=? AND quote=?
                             AND horizon_minutes=? AND side=?
                             AND reference_opened_at=? AND state='pending'""",
                        (stamp.isoformat(),int(signed>0),early,abs(signed),
                         first_close_hit,p["venue"],p["contract"],p["quote"],
                         p["horizon_minutes"],p["side"],p["reference_opened_at"]),
                    )
                    scored+=int(state=="settled")
                    ambiguous+=int(state=="ambiguous")
        return {"settled":scored,"ambiguous":ambiguous}

    def report(self, *, asset, horizon, now=None) -> dict:
        """Independent source/side evaluation; research never live approval."""
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows=conn.execute(
                """SELECT * FROM rival_forward_paths WHERE asset=?
                   AND horizon_minutes=? AND issued_at>=? AND settled_at<=?
                   AND state='settled' ORDER BY target_opened_at""",
                (asset,horizon,(stamp-timedelta(days=14)).isoformat(),
                 stamp.isoformat()),
            ).fetchall()
        scopes={}
        for row in rows:
            key="/".join((row["venue"],row["contract"],row["quote"],row["side"]))
            target=str(row["target_opened_at"])
            if (int(datetime.fromisoformat(target).timestamp())-300)%(horizon*60):
                continue
            scopes.setdefault(key,{}).setdefault(target,[]).append(row)
        reports={}
        for key,targets in scopes.items():
            comparisons={}
            for level in (1,2):
                paired=[]
                for target,group in sorted(targets.items()):
                    if len(group)!=1 or group[0][f"tp{level}_before_stop"] not in (0,1):
                        continue
                    r=group[0]
                    y=int(r[f"tp{level}_before_stop"])
                    m=float(r[f"tp{level}_probability"])
                    b=float(r[f"tp{level}_baseline"])
                    if not all(math.isfinite(v) and 0<=v<=1 for v in (m,b)):
                        continue
                    paired.append((target,(m-y)**2,(b-y)**2))
                proof=score_paired_forward(paired)
                comparisons[f"tp{level}"]={
                    "targets":proof.targets,"days":proof.days,
                    "model_brier":proof.model_brier,
                    "baseline_brier":proof.baseline_brier,
                    "qualified_research":proof.status=="qualified_shadow",
                    "reason":proof.reason,
                }
            reports[key]={
                "tp1":comparisons["tp1"],"tp2":comparisons["tp2"],
                "qualified_research":all(x["qualified_research"]
                                         for x in comparisons.values()),
                "live_approved":False,
                "after_cost_qualified":False,
            }
        return reports

    def target_report(self, *, asset, horizon, now=None) -> dict:
        """Independent close-only scores, not proof of an executable edge.

        Direction and early-move are paired Brier comparisons against
        source-specific historical baselines. Move size uses paired absolute
        error. Correlated long/short and overlapping targets are reported
        separately; these rows cannot approve a live model or simulated fill.
        """
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows=conn.execute(
                """SELECT * FROM rival_forward_path_targets
                   WHERE asset=? AND horizon_minutes=?
                     AND issued_at>=? AND settled_at<=?
                     AND state='settled'
                   ORDER BY target_opened_at DESC LIMIT 8000""",
                (asset,horizon,(stamp-timedelta(days=28)).isoformat(),
                 stamp.isoformat()),
            ).fetchall()
        scopes={}
        for row in rows:
            key="/".join((row["venue"],row["contract"],row["quote"],row["side"],row["model_version"]))
            scopes.setdefault(key,{}).setdefault(str(row["target_opened_at"]),[]).append(row)
        reports={}
        for key, grouped in scopes.items():
            direction=[]
            timing=[]
            size=[]
            for target, group in sorted(grouped.items()):
                # Only one eligible prospect per exact model/source/target.
                # Do not count duplicates as independent supporting evidence.
                if len(group)!=1:
                    continue
                row=group[0]
                if row["direction_positive"] not in (0,1) or row["early_move_by_half"] not in (0,1):
                    continue
                y=int(row["direction_positive"])
                early=int(row["early_move_by_half"])
                d=float(row["direction_probability"])
                db=float(row["direction_baseline"])
                t=float(row["early_move_probability"])
                tb=float(row["early_move_baseline"])
                estimate=float(row["absolute_move_p50"])
                baseline=float(row["absolute_move_baseline"])
                actual=float(row["absolute_move_realized"])
                if (not all(math.isfinite(x) for x in (d,db,t,tb,estimate,baseline,actual))
                        or not all(0<x<1 for x in (d,db,t,tb))
                        or min(estimate,baseline,actual)<0):
                    continue
                direction.append((target,(d-y)**2,(db-y)**2))
                timing.append((target,(t-early)**2,(tb-early)**2))
                size.append((abs(estimate-actual),abs(baseline-actual)))
            d_proof=score_paired_forward(direction)
            t_proof=score_paired_forward(timing)
            reports[key]={
                "direction": {
                    "targets":d_proof.targets,"days":d_proof.days,
                    "model_brier":d_proof.model_brier,
                    "baseline_brier":d_proof.baseline_brier,
                    "paired_gain":d_proof.paired_gain,
                    "research_screen":d_proof.status,"reason":d_proof.reason,
                },
                "early_close_move": {
                    "definition": "first close >= prior one-sigma move by half horizon",
                    "targets":t_proof.targets,"days":t_proof.days,
                    "model_brier":t_proof.model_brier,
                    "baseline_brier":t_proof.baseline_brier,
                    "paired_gain":t_proof.paired_gain,
                    "research_screen":t_proof.status,"reason":t_proof.reason,
                },
                "absolute_terminal_move": {
                    "targets":len(size),
                    "model_mean_abs_error":(
                        sum(x[0] for x in size)/len(size) if size else None),
                    "baseline_mean_abs_error":(
                        sum(x[1] for x in size)/len(size) if size else None),
                },
                "independent_of_intrabar_tp_stop_order": True,
                "correlated_with_other_sides_and_horizons": True,
                "qualified_for_live_use": False,
                "after_cost_qualified": False,
                "execution_quote_proven": False,
            }
        return reports
