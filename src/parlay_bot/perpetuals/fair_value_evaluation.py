"""Read-only evaluator for the settlement-faithful fair-value pre-test.

The family under test (design 2026-11-19, competitor-edge research
family 1): price each native 15-minute Up/Down contract as the digital
option it mechanically is, on the venue's OWN settlement index --
Polymarket's Chainlink TWAP-60s (average vs average: the price-to-beat
is itself a TWAP-60s) and Kalshi's CF Benchmarks BRTI (60-second average
at the close against the 60-second average at the open, published as
``floor_strike``). Fair value is Phi(d), d the distance from the
PROJECTED settlement average to the strike, scaled by the trailing
realised volatility of the settlement series itself and the effective
time remaining. Nothing is fitted anywhere: no drift, no parameters --
the chronological halves are a regime-stability check, not a train/test
split.

Inputs are the records the collectors persist: executable book snapshots
(``rival_convergence_book_snapshots``, the convergence collector's
table), the settlement-index paths (``rival_settlement_index_ticks``,
recorded by the PolyBolt recorder and the Kalshi BRTI collector), strikes
(the same reference-level chain the convergence evaluator uses), and
labels from each venue's own published settlement as recorded in the
store (Polymarket outcome receipts; Kalshi's published result). A window
missing any input is excluded and counted -- never imputed, never
proxied, never graded on a spot substitute. This module writes nothing,
issues nothing, and is never wired into a bot loop or a schedule; it is
run manually (``tools/evaluate_fair_value.py``).

Frozen criteria (design section 7 -- changing any of these numbers is a
NEW pre-registration, not an edit):

- Universe: native 15-minute windows, BTC/ETH/SOL/XRP, per venue.
  Windows count only when their full life falls inside the venue's
  index-recording period, starting from the first full UTC day after
  that venue's recorder deployed.
- Checkpoints: t=300s and t=600s (primary, pooled per venue); t=840s is
  a secondary final-minute sub-test, reported separately and never part
  of the verdict.
- Volatility: per-second std of log increments of the settlement series
  itself, trailing 60 minutes before the decision time, prints at or
  before t only. A gap over 120s in that trailing series (or a level
  stale by more than 120s) excludes the observation. The 1e-6 sigma
  floor is a numerical guard only.
- Call rule: call Up iff F - (avg_ask + fee) >= buffer; call Down iff
  (1-F) - ((1-avg_bid) + fee) >= buffer; else NO CALL, a first-class
  outcome. Buffer = half the depth-walked spread + 1.5c, floored at
  2.5c. Fees: Polymarket at the snapshot's own stored fee_rate; Kalshi
  via ``kalshi_fee()`` (ceil to the cent). Neighbouring margins are
  reported for transparency; the verdict uses only the registered one.
- Minimum sample, per venue, evaluated ONCE when all are met: >= 7
  distinct UTC days of index recording AND >= 2,500 evaluated windows
  AND >= 250 triggered calls. Below the floors the report is ACCRUAL
  with ``verdict: None`` -- structurally unable to print PASS/KILL.
- PASS (per venue, all four): (1) pooled paired delta log-loss < 0 with
  the day-clustered 95% interval entirely below 0; (2) pooled paired
  delta Brier <= -0.005; (3) delta log-loss < 0 in both chronological
  halves; (4) call-subset after-cost mean net > 0 in both chronological
  halves. KILL otherwise -- a KILL retires the family for that venue.
- Borderline rule (one, pre-registered): if delta Brier lands in
  [-0.005, -0.002) with legs 1, 3, 4 passing, the sample extends by
  exactly 7 more days ONCE (status BORDERLINE_EXTENSION, verdict still
  None; rerun with the extension marked used) and the extended sample
  decides on the original bar.

An "evaluated window" is a window with at least one evaluated primary
checkpoint; an "observation" is one (window, checkpoint) pair. The
call floor counts primary-checkpoint calls only.
"""
from __future__ import annotations

import bisect
import itertools
import math
import random
from datetime import UTC, datetime, time, timedelta

from parlay_bot.perpetuals.convergence_evaluation import (
    _reference_from_evidence,
    _snapshot_reference,
)
from parlay_bot.perpetuals.convergence_quotes import select_checkpoint
from parlay_bot.perpetuals.kalshi_updown import kalshi_fee
from parlay_bot.perpetuals.polymarket_learning import polymarket_fee

ASSETS = ("BTC", "ETH", "SOL", "XRP")
VENUES = ("polymarket", "kalshi")

# --- Frozen criteria (design 2026-11-19, sections 6-7) ---
WINDOW_SECONDS = 900
PRIMARY_OFFSETS = (300, 600)
SECONDARY_OFFSET = 840
CHECKPOINT_TOLERANCE_SECONDS = 45
VOL_LOOKBACK_SECONDS = 3600
VOL_GAP_SECONDS = 120
SIGMA_FLOOR = 1e-6  # numerical guard only (design 6.3)
SAFETY_MARGIN = 0.015  # the call rule's 1.5c safety margin
BUFFER_FLOOR = 0.025  # buffer floored at 2.5c total
MIN_RECORDING_DAYS = 7
MIN_EVALUATED_WINDOWS = 2500
MIN_TRIGGERED_CALLS = 250
PASS_BRIER = -0.005
BORDERLINE_BRIER_LOW = -0.005
BORDERLINE_BRIER_HIGH = -0.002
EXTENSION_DAYS = 7
MARGIN_SENSITIVITY_STEP = 0.005  # transparency cuts only, never gated
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261119
PROBABILITY_EPSILON = 1e-9

WINDOW_EXCLUSION_REASONS = (
    "outside_recording",
    "strike_unavailable",
    "settlement_unavailable",
    "index_missing",
)
OBSERVATION_EXCLUSION_REASONS = (
    "index_gap",
    "missing_snapshot",
    "unfillable_leg",
    "fee_unverified",
)


def _parse_time(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if at.utcoffset() is None:
        return None
    return at.astimezone(UTC)


def _phi(z: float) -> float:
    """Standard normal CDF via erf, clamped for scoring."""
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return min(max(p, PROBABILITY_EPSILON), 1.0 - PROBABILITY_EPSILON)


def projected_settlement_average(ticks, *, window_open: datetime,
                                 at: datetime) -> tuple[float, float] | None:
    """(projected settlement average, current level) at time ``at``.

    ``ticks`` is the venue's settlement series for the asset as sorted
    (source_at, value) pairs. The settlement average covers the final
    60 seconds of the window; the portion already printed by ``at`` is
    integrated from the recorded prints (piecewise-constant between
    prints), the unprinted remainder is projected at the current level
    -- driftless, no look-ahead: only prints at or before ``at`` count.
    """
    if not ticks:
        return None
    close = window_open + timedelta(seconds=WINDOW_SECONDS)
    avg_start = close - timedelta(seconds=60)
    level = None
    for stamp, value in ticks:
        if stamp > at:
            break
        level = value
    if level is None:
        return None
    printed = min(max((at - avg_start).total_seconds(), 0.0), 60.0)
    integral = 0.0
    if printed > 0:
        # Piecewise-constant integration over [avg_start, at]: the value
        # in force at avg_start is the last print at or before it.
        cursor = avg_start
        current = None
        for stamp, value in ticks:
            if stamp <= avg_start:
                current = value
                continue
            if stamp > at:
                break
            if current is not None:
                integral += current * (stamp - cursor).total_seconds()
            cursor = stamp
            current = value
        if current is not None:
            integral += current * (at - cursor).total_seconds()
    mu = (integral + level * (60.0 - printed)) / 60.0
    return mu, level


def trailing_sigma(ticks, *, at: datetime) -> float | None:
    """Per-second std of log increments of the settlement series.

    Trailing 60 minutes before ``at``, prints at or before ``at`` only.
    Returns None (fail closed) when the series has fewer than two
    prints, a gap over VOL_GAP_SECONDS between consecutive prints, or
    no print within VOL_GAP_SECONDS of ``at`` (a stale level).
    """
    start = at - timedelta(seconds=VOL_LOOKBACK_SECONDS)
    series = [(stamp, value) for stamp, value in ticks
              if start <= stamp <= at]
    if len(series) < 2:
        return None
    if (at - series[-1][0]).total_seconds() > VOL_GAP_SECONDS:
        return None
    squared = 0.0
    span = 0.0
    for (t0, v0), (t1, v1) in itertools.pairwise(series):
        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            continue
        if dt > VOL_GAP_SECONDS:
            return None
        if v0 <= 0 or v1 <= 0:
            return None
        squared += math.log(v1 / v0) ** 2
        span += dt
    if span <= 0:
        return None
    return max(math.sqrt(squared / span), SIGMA_FLOOR)


def fair_value(ticks, *, window_open: datetime, strike: float,
               at: datetime) -> dict | None:
    """The closed-form fair value at one checkpoint, or None.

    Remaining standard deviation of the settlement average, for a
    driftless random walk viewed from ``at``: outside the final minute
    the variance of the 60-second average is sigma^2 * ((T-t) - 40) --
    the (T-60-t) wait for the average window to start plus 60/3 from
    averaging inside it, which is where the design's "~40s" comes from;
    inside the final minute only the unprinted remainder still varies,
    sigma^2 * (T-t)^3 / (3 * 60^2) on the average.
    """
    projected = projected_settlement_average(
        ticks, window_open=window_open, at=at)
    if projected is None:
        return None
    mu, level = projected
    sigma = trailing_sigma(ticks, at=at)
    if sigma is None:
        return None
    elapsed = (at - window_open).total_seconds()
    remaining = WINDOW_SECONDS - elapsed
    if remaining <= 0:
        return None
    if elapsed <= WINDOW_SECONDS - 60:
        spread = level * sigma * math.sqrt(remaining - 40.0)
    else:
        spread = level * sigma * remaining ** 1.5 / (math.sqrt(3.0) * 60.0)
    if spread <= 0:
        raw = 1.0 if mu > strike else 0.0 if mu < strike else 0.5
        probability = min(max(raw, PROBABILITY_EPSILON),
                          1.0 - PROBABILITY_EPSILON)
        return {"fair_value": probability,
                "mu": mu, "level": level, "sigma_s": sigma,
                "remaining_std": spread}
    return {
        "fair_value": _phi((mu - strike) / spread),
        "mu": mu, "level": level, "sigma_s": sigma,
        "remaining_std": spread,
    }


def _fee(venue: str, price: float, fee_rate) -> float | None:
    """Taker fee per contract for one leg; None when unverifiable."""
    if venue == "kalshi":
        return kalshi_fee(price)
    if fee_rate is None:
        return None
    return polymarket_fee(price, float(fee_rate))


def call_decision(*, fair: float, avg_ask: float, avg_bid: float,
                  fee_up: float, fee_down: float,
                  buffer: float) -> tuple[str | None, float, float]:
    """The pre-registered call rule; returns (side, cost, edge)."""
    up_cost = avg_ask + fee_up
    down_cost = (1.0 - avg_bid) + fee_down
    up_edge = fair - up_cost
    down_edge = (1.0 - fair) - down_cost
    if up_edge >= buffer and up_edge >= down_edge:
        return "up", up_cost, up_edge
    if down_edge >= buffer:
        return "down", down_cost, down_edge
    return None, 0.0, 0.0


def _log_loss(p: float, y: int) -> float:
    p = min(max(p, PROBABILITY_EPSILON), 1.0 - PROBABILITY_EPSILON)
    return -(y * math.log(p) + (1 - y) * math.log(1.0 - p))


def _day_cluster_interval(observations, key: str) -> dict | None:
    """Day-clustered bootstrap 95% interval for a paired mean delta.

    Windows within a UTC day are resampled as blocks (the four assets in
    one quarter hour are largely one macro bet, design section 3.10).
    Deterministic: a fixed seed makes every readout reproducible.
    """
    by_day: dict[str, list[float]] = {}
    for obs in observations:
        by_day.setdefault(obs["day"], []).append(obs[key])
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_REPLICATES):
        total = 0.0
        count = 0
        for _ in range(len(days)):
            values = by_day[rng.choice(days)]
            total += sum(values)
            count += len(values)
        means.append(total / count)
    means.sort()
    low = means[int(0.025 * len(means))]
    high = means[min(len(means) - 1, int(0.975 * len(means)))]
    return {"lower": low, "upper": high, "days": len(days),
            "replicates": len(means)}


def _halves(observations) -> tuple[list, list]:
    ordered = sorted(observations,
                     key=lambda o: (o["window_open"], o["offset"]))
    split = len(ordered) // 2
    return ordered[:split], ordered[split:]


def _mean(values) -> float | None:
    return sum(values) / len(values) if values else None


def _score(observations) -> dict:
    """One metrics block over (window, checkpoint) observations."""
    n = len(observations)
    if not n:
        return {
            "observations": 0, "windows": 0,
            "delta_log_loss": None, "delta_log_loss_ci95": None,
            "delta_brier": None, "delta_brier_ci95": None,
            "chronological_halves": None,
            "calls": {"n": 0, "mean_net": None, "win_rate": None,
                      "breakeven_win_rate": None},
            "trigger_rate": None, "near_strike_share": None,
        }
    first, second = _halves(observations)

    def _call_block(obs_list):
        calls = [o for o in obs_list if o["call_side"] is not None]
        if not calls:
            return {"n": 0, "mean_net": None, "win_rate": None,
                    "breakeven_win_rate": None}
        return {
            "n": len(calls),
            "mean_net": _mean([o["call_net"] for o in calls]),
            "win_rate": sum(1 for o in calls if o["call_net"] > 0) / len(calls),
            "breakeven_win_rate": _mean([o["call_cost"] for o in calls]),
        }

    calls = [o for o in observations if o["call_side"] is not None]
    return {
        "observations": n,
        "windows": len({o["window_key"] for o in observations}),
        "delta_log_loss": _mean([o["delta_log_loss"] for o in observations]),
        "delta_log_loss_ci95": _day_cluster_interval(
            observations, "delta_log_loss"),
        "delta_brier": _mean([o["delta_brier"] for o in observations]),
        "delta_brier_ci95": _day_cluster_interval(
            observations, "delta_brier"),
        "chronological_halves": {
            "first_delta_log_loss": _mean(
                [o["delta_log_loss"] for o in first]),
            "second_delta_log_loss": _mean(
                [o["delta_log_loss"] for o in second]),
            "first_call_mean_net": _call_block(first)["mean_net"],
            "second_call_mean_net": _call_block(second)["mean_net"],
            "first_n": len(first), "second_n": len(second),
        },
        "calls": _call_block(observations),
        "trigger_rate": len(calls) / n,
        "near_strike_share": (
            sum(1 for o in observations if o["near_strike"]) / n),
    }


def _buffer(avg_ask: float, avg_bid: float, *, margin: float = SAFETY_MARGIN) -> float:
    """Half the depth-walked spread + safety margin, floored (design 6.4)."""
    return max((avg_ask - avg_bid) / 2.0 + margin, BUFFER_FLOOR)


def _observe(window: dict, offset: int, *,
             observation_exclusions: dict) -> dict | None:
    """One (window, checkpoint) observation, or None (counted exclusion)."""
    venue = window["venue"]
    opened = window["window_open"]
    at = opened + timedelta(seconds=offset)
    ticks = window["index_ticks"]
    fv = fair_value(ticks, window_open=opened, strike=window["strike"], at=at)
    if fv is None:
        # Distinguish "no usable series at all" (handled at window level)
        # from a trailing-series gap or stale level at this checkpoint.
        observation_exclusions["index_gap"] += 1
        return None
    snap = select_checkpoint(
        window["snapshots"], window_open=opened, offset_seconds=offset,
        tolerance_seconds=CHECKPOINT_TOLERANCE_SECONDS)
    if snap is None:
        observation_exclusions["missing_snapshot"] += 1
        return None
    avg_bid, avg_ask = snap.get("avg_bid"), snap.get("avg_ask")
    if avg_bid is None or avg_ask is None:
        observation_exclusions["unfillable_leg"] += 1
        return None
    avg_bid, avg_ask = float(avg_bid), float(avg_ask)
    fee_up = _fee(venue, avg_ask, snap.get("fee_rate"))
    fee_down = _fee(venue, 1.0 - avg_bid, snap.get("fee_rate"))
    if fee_up is None or fee_down is None:
        observation_exclusions["fee_unverified"] += 1
        return None
    fair = fv["fair_value"]
    mid = min(max((avg_bid + avg_ask) / 2.0, PROBABILITY_EPSILON),
              1.0 - PROBABILITY_EPSILON)
    label = window["settlement"]
    buffer = _buffer(avg_ask, avg_bid)
    side, cost, _ = call_decision(
        fair=fair, avg_ask=avg_ask, avg_bid=avg_bid,
        fee_up=fee_up, fee_down=fee_down, buffer=buffer)
    call_net = None
    if side == "up":
        call_net = label - cost
    elif side == "down":
        call_net = (1 - label) - cost
    return {
        "window_key": window["contract_id"],
        "window_open": opened,
        "day": opened.date().isoformat(),
        "asset": window["asset"],
        "offset": offset,
        "fair": fair,
        "market_mid": mid,
        "label": label,
        "delta_log_loss": _log_loss(fair, label) - _log_loss(mid, label),
        "delta_brier": (fair - label) ** 2 - (mid - label) ** 2,
        "near_strike": abs(fv["mu"] - window["strike"]) < fv["remaining_std"],
        "call_side": side,
        "call_cost": cost if side is not None else None,
        "call_net": call_net,
        # Kept so the transparency margin cuts can re-run the call rule
        # at neighbouring buffers without touching the store again.
        "_avg_ask": avg_ask, "_avg_bid": avg_bid,
        "_fee_up": fee_up, "_fee_down": fee_down,
    }


def _margin_sensitivity(observations) -> dict:
    """Call-subset outcomes at buffer +/- the sensitivity step.

    Transparency only (design 2.4): the verdict uses the registered
    buffer; these cuts exist so a reader can see the result is not a
    threshold artifact.
    """
    out = {}
    for label, delta in (("buffer_minus_0.005", -MARGIN_SENSITIVITY_STEP),
                         ("buffer_plus_0.005", MARGIN_SENSITIVITY_STEP)):
        nets = []
        for obs in observations:
            margin = SAFETY_MARGIN + delta
            buffer = _buffer(obs["_avg_ask"], obs["_avg_bid"], margin=margin)
            side, cost, _ = call_decision(
                fair=obs["fair"], avg_ask=obs["_avg_ask"],
                avg_bid=obs["_avg_bid"], fee_up=obs["_fee_up"],
                fee_down=obs["_fee_down"], buffer=buffer)
            if side == "up":
                nets.append(obs["label"] - cost)
            elif side == "down":
                nets.append((1 - obs["label"]) - cost)
        out[label] = {"calls": len(nets), "mean_net": _mean(nets)}
    return out


def _evaluate_venue(venue: str, windows: list[dict], *,
                    extension_used: bool) -> dict:
    """One venue's sample against the frozen criteria (pure, no I/O)."""
    window_exclusions = {r: 0 for r in WINDOW_EXCLUSION_REASONS}
    observation_exclusions = {r: 0 for r in OBSERVATION_EXCLUSION_REASONS}
    primary: list[dict] = []
    secondary: list[dict] = []
    evaluated_windows = 0
    recording_days = 0
    for window in windows:
        recording_days = max(recording_days, int(window.get("recording_days") or 0))
        opened, close = window["window_open"], window["window_close"]
        first_full = window.get("recording_first_full_start")
        last_tick = window.get("recording_last_tick")
        if (first_full is None or last_tick is None
                or opened < first_full
                or close > last_tick + timedelta(seconds=VOL_GAP_SECONDS)):
            window_exclusions["outside_recording"] += 1
            continue
        if window.get("strike") is None:
            window_exclusions["strike_unavailable"] += 1
            continue
        if window.get("settlement") is None:
            window_exclusions["settlement_unavailable"] += 1
            continue
        if not window.get("index_ticks"):
            window_exclusions["index_missing"] += 1
            continue
        before = len(primary)
        for offset in PRIMARY_OFFSETS:
            obs = _observe(window, offset,
                           observation_exclusions=observation_exclusions)
            if obs is not None:
                primary.append(obs)
        if len(primary) > before:
            evaluated_windows += 1
        obs = _observe(window, SECONDARY_OFFSET,
                       observation_exclusions=observation_exclusions)
        if obs is not None:
            secondary.append(obs)

    primary_score = _score(primary)
    calls = primary_score["calls"]["n"]
    need_days = MIN_RECORDING_DAYS + (EXTENSION_DAYS if extension_used else 0)
    floors = {
        "recording_days": {"have": recording_days, "need": need_days},
        "evaluated_windows": {"have": evaluated_windows,
                              "need": MIN_EVALUATED_WINDOWS},
        "triggered_calls": {"have": calls, "need": MIN_TRIGGERED_CALLS},
        "extension_used": extension_used,
    }
    mature = (recording_days >= need_days
              and evaluated_windows >= MIN_EVALUATED_WINDOWS
              and calls >= MIN_TRIGGERED_CALLS)
    legs = None
    status, verdict = "ACCRUAL", None
    if mature:
        halves = primary_score["chronological_halves"] or {}
        ci = primary_score["delta_log_loss_ci95"] or {}
        legs = {
            "paired_log_loss": bool(
                primary_score["delta_log_loss"] is not None
                and primary_score["delta_log_loss"] < 0
                and ci.get("upper") is not None and ci["upper"] < 0),
            "paired_brier": bool(
                primary_score["delta_brier"] is not None
                and primary_score["delta_brier"] <= PASS_BRIER),
            "both_halves_log_loss": bool(
                (halves.get("first_delta_log_loss") or 0.0) < 0
                and (halves.get("second_delta_log_loss") or 0.0) < 0),
            "both_halves_call_net": bool(
                halves.get("first_call_mean_net") is not None
                and halves["first_call_mean_net"] > 0
                and halves.get("second_call_mean_net") is not None
                and halves["second_call_mean_net"] > 0),
        }
        if all(legs.values()):
            status, verdict = "EVALUATED", "PASS"
        elif (not extension_used
              and not legs["paired_brier"]
              and primary_score["delta_brier"] is not None
              and BORDERLINE_BRIER_LOW <= primary_score["delta_brier"]
              < BORDERLINE_BRIER_HIGH
              and legs["paired_log_loss"] and legs["both_halves_log_loss"]
              and legs["both_halves_call_net"]):
            # The one pre-registered borderline rule: extend exactly 7
            # more days, once; the extended sample decides.
            status, verdict = "BORDERLINE_EXTENSION", None
        else:
            status, verdict = "EVALUATED", "KILL"
    return {
        "status": status,
        "verdict": verdict,
        "floors": floors,
        "legs": legs,
        "coverage": {
            "windows_seen": len(windows),
            "evaluated_windows": evaluated_windows,
            "primary_observations": len(primary),
            "secondary_observations": len(secondary),
            "window_exclusions": window_exclusions,
            "observation_exclusions": observation_exclusions,
        },
        "primary": primary_score,
        "secondary_t840": _score(secondary),
        "per_asset": {
            asset: _score([o for o in primary if o["asset"] == asset])
            for asset in ASSETS
        },
        "margin_sensitivity": _margin_sensitivity(primary),
    }


def evaluate_fair_value(windows: list[dict], *, now: datetime,
                        extension_used: bool = False) -> dict:
    """Apply the frozen fair-value criteria per venue (pure, no I/O).

    Each window: {venue, asset, contract_id, window_open, window_close,
    strike, settlement (0/1/None), snapshots, index_ticks,
    recording_days, recording_first_full_start, recording_last_tick}.
    Below the section-7 floors a venue's block carries ``verdict:
    None`` -- an accrual state, never a PASS/KILL.
    """
    clock = now.astimezone(UTC)
    venues = {}
    for venue in VENUES:
        venues[venue] = _evaluate_venue(
            venue, [w for w in windows if w.get("venue") == venue],
            extension_used=extension_used)
    return {
        "schema": "rival-fair-value-evaluation-v1",
        "evaluated_at": clock.isoformat(),
        "read_only": True,
        "real_money_orders": 0,
        "frozen_criteria": {
            "window_seconds": WINDOW_SECONDS,
            "primary_offsets": list(PRIMARY_OFFSETS),
            "secondary_offset": SECONDARY_OFFSET,
            "checkpoint_tolerance_seconds": CHECKPOINT_TOLERANCE_SECONDS,
            "vol_lookback_seconds": VOL_LOOKBACK_SECONDS,
            "vol_gap_seconds": VOL_GAP_SECONDS,
            "safety_margin": SAFETY_MARGIN,
            "buffer_floor": BUFFER_FLOOR,
            "min_recording_days": MIN_RECORDING_DAYS,
            "min_evaluated_windows": MIN_EVALUATED_WINDOWS,
            "min_triggered_calls": MIN_TRIGGERED_CALLS,
            "pass_brier": PASS_BRIER,
            "borderline_brier": [BORDERLINE_BRIER_LOW, BORDERLINE_BRIER_HIGH],
            "extension_days": EXTENSION_DAYS,
        },
        "venues": venues,
    }


# ---------------------------------------------------------------------------
# Loader (SELECT-only).
# ---------------------------------------------------------------------------

def _table_exists(store, conn, table: str) -> bool:
    if store.database.backend == "postgres":
        found = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_name=?", (table,)).fetchone()
    else:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name=?", (table,)).fetchone()
    return found is not None


# Rows per fetchmany chunk when streaming the evaluator's growing series
# loads (the #727 pattern): identical order and contents to fetchall(),
# without materializing ~8 days of ticks/outcomes before the first row
# is processed.
FETCH_CHUNK = 2000


def _stream_rows(conn, sql: str):
    """Yield a query's rows in FETCH_CHUNK slices, in the query's order."""
    cursor = conn.execute(sql)
    while True:
        chunk = cursor.fetchmany(FETCH_CHUNK)
        if not chunk:
            return
        yield from chunk


def load_windows(store) -> list[dict]:
    """Read snapshots, strikes, settlements and index paths (SELECT-only).

    Strikes follow the convergence evaluator's preference chain: the
    level a snapshot carried from its own venue fetch, then the
    collector-resolved ``rival_convergence_reference_levels``, then the
    stored joins (Kalshi ``open_reference`` by ticker; Polymarket
    price-to-beat from oracle/AI-call evidence by slug). Settlements are
    each venue's own published result as recorded in the store:
    Polymarket outcome receipts (``direction`` UP/DOWN by slug) and
    Kalshi's ``result_yes`` by ticker. Anything missing stays None and
    is excluded downstream -- never approximated.
    """
    with store.connect() as conn:
        ref_expr = (
            "reference_level"
            if "reference_level" in store.database.column_names(
                conn, "rival_convergence_book_snapshots")
            else "NULL AS reference_level")
        snapshots = conn.execute(
            "SELECT venue, asset, contract_id, window_open, window_close,"
            " source_at, avg_bid, avg_ask, fee_rate,"
            f" {ref_expr}, book_sha256"
            " FROM rival_convergence_book_snapshots"
            " ORDER BY venue, asset, window_open, source_at",
        ).fetchall()
        kalshi_refs: dict[str, float] = {}
        kalshi_results: dict[str, int] = {}
        if _table_exists(store, conn, "rival_kalshi_updown"):
            for row in conn.execute(
                    "SELECT ticker, open_reference, result_yes"
                    " FROM rival_kalshi_updown").fetchall():
                if row["open_reference"] is not None:
                    kalshi_refs[str(row["ticker"])] = float(row["open_reference"])
                if row["result_yes"] is not None:
                    kalshi_results[str(row["ticker"])] = int(row["result_yes"])
        pm_refs: dict[str, float] = {}
        for table in ("rival_poly_oracle_observations",
                      "rival_polymarket_ai_calls"):
            if not _table_exists(store, conn, table):
                continue
            for row in conn.execute(
                    f"SELECT slug, evidence_json FROM {table}").fetchall():
                slug = str(row["slug"])
                if slug not in pm_refs:
                    value = _reference_from_evidence(row["evidence_json"])
                    if value is not None:
                        pm_refs[slug] = value
        if _table_exists(store, conn, "rival_convergence_reference_levels"):
            for row in conn.execute(
                    "SELECT venue, contract_id, reference_level"
                    " FROM rival_convergence_reference_levels").fetchall():
                value = _snapshot_reference(row["reference_level"])
                if value is None:
                    continue
                if row["venue"] == "polymarket":
                    pm_refs[str(row["contract_id"])] = value
                elif row["venue"] == "kalshi":
                    kalshi_refs[str(row["contract_id"])] = value
        pm_results: dict[str, int] = {}
        if _table_exists(store, conn, "rival_polymarket_ai_outcomes"):
            for row in _stream_rows(
                    conn,
                    "SELECT slug, direction"
                    " FROM rival_polymarket_ai_outcomes"):
                direction = str(row["direction"]).upper()
                if direction == "UP":
                    pm_results[str(row["slug"])] = 1
                elif direction == "DOWN":
                    pm_results[str(row["slug"])] = 0
        series: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
        recording: dict[str, dict] = {}
        if _table_exists(store, conn, "rival_settlement_index_ticks"):
            for row in _stream_rows(
                    conn,
                    "SELECT venue, asset, source_at, value"
                    " FROM rival_settlement_index_ticks"
                    " ORDER BY venue, asset, source_at"):
                stamp = _parse_time(row["source_at"])
                if stamp is None:
                    continue
                key = (str(row["venue"]), str(row["asset"]))
                series.setdefault(key, []).append((stamp, float(row["value"])))
                venue_rec = recording.setdefault(
                    str(row["venue"]), {"first": stamp, "last": stamp, "days": set()})
                venue_rec["first"] = min(venue_rec["first"], stamp)
                venue_rec["last"] = max(venue_rec["last"], stamp)
                venue_rec["days"].add(stamp.date())

    recording_info: dict[str, dict] = {}
    for venue, rec in recording.items():
        first_full = datetime.combine(
            rec["first"].date() + timedelta(days=1), time.min, tzinfo=UTC)
        recording_info[venue] = {
            "days": len(rec["days"]),
            "first_full_start": first_full,
            "last_tick": rec["last"],
        }

    grouped: dict[tuple[str, str], dict] = {}
    carried: dict[tuple[str, str], float] = {}
    for row in snapshots:
        record = dict(row)
        key = (str(record["venue"]), str(record["contract_id"]))
        window = grouped.get(key)
        if window is None:
            opened = _parse_time(record["window_open"])
            close = _parse_time(record["window_close"])
            if opened is None or close is None:
                continue
            venue = str(record["venue"])
            asset = str(record["asset"])
            info = recording_info.get(venue) or {
                "days": 0, "first_full_start": None, "last_tick": None}
            window = grouped[key] = {
                "venue": venue, "asset": asset,
                "contract_id": str(record["contract_id"]),
                "window_open": opened, "window_close": close,
                "strike": None, "settlement": None,
                "snapshots": [],
                "index_ticks": [],
                "recording_days": info["days"],
                "recording_first_full_start": info["first_full_start"],
                "recording_last_tick": info["last_tick"],
            }
            if venue == "polymarket":
                window["strike"] = pm_refs.get(str(record["contract_id"]))
                window["settlement"] = pm_results.get(str(record["contract_id"]))
            elif venue == "kalshi":
                window["strike"] = kalshi_refs.get(str(record["contract_id"]))
                window["settlement"] = kalshi_results.get(str(record["contract_id"]))
            ticks = series.get((venue, asset)) or []
            if ticks:
                stamps = [stamp for stamp, _ in ticks]
                lo = bisect.bisect_left(
                    stamps, opened - timedelta(seconds=VOL_LOOKBACK_SECONDS))
                hi = bisect.bisect_right(stamps, close)
                window["index_ticks"] = ticks[lo:hi]
        window["snapshots"].append(record)
        snapshot_ref = _snapshot_reference(record.get("reference_level"))
        if snapshot_ref is not None and key not in carried:
            carried[key] = snapshot_ref
    # Snapshot-carried levels outrank the stored joins, as in the
    # convergence evaluator: the level the venue itself had resolved
    # when the book was fetched.
    for key, window in grouped.items():
        if key in carried:
            window["strike"] = carried[key]
    return list(grouped.values())
