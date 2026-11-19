"""Read-only evaluator for the PM-lag convergence re-measurement (PR2).

Track 1 (PR #690) collects executable book snapshots for Polymarket and
Kalshi 15-minute Up/Down windows into ``rival_convergence_book_snapshots``.
Nothing read that table. This module is the single pre-registered consumer:
it pairs the two venues' snapshots per window, applies the frozen criteria
from the 2026-11-18 re-measurement report exactly, and emits one report --
an ACCRUAL state while the sample is immature, and the single PASS/KILL
verdict only at the frozen evaluation point. It writes nothing, issues
nothing, and is never wired into a bot loop or a schedule; it is run
manually (``tools/evaluate_pm_lag_convergence.py``) against the bot's
store.

Frozen criteria (changing any of these numbers is a NEW pre-registration,
not an edit -- pivot spec K3 discipline):

- Universe: native 15-minute windows, BTC/ETH/SOL/XRP, both venues on the
  same UTC interval.
- Direction: PM-cheap only. The Kalshi-cheap direction lost in both
  pre-test halves and is pre-registered as out.
- Checkpoints: entry snapshot closest to open+300s within +/-45s, exit
  likewise for +600s (``select_checkpoint`` from the collection module).
- Trigger: Kalshi book mid - PM 10-share depth-walked average ask
  > 0.035 at the entry checkpoint.
- Hypothetical trade (measurement only -- RivaL places no orders): buy
  10 PM Up contracts at the entry ``avg_ask``, exit at the exit
  checkpoint's ``avg_bid``. A leg whose book cannot fill the clip
  (average is NULL) excludes the window and is counted, never priced off
  the top of book.
- Fees: Polymarket taker fee per share = fee_rate * P * (1 - P) charged
  on both legs at each contract's own recorded feeSchedule rate
  (``polymarket_fee``). Windows whose entry snapshot has ``fee_rate``
  NULL are excluded from the headline and counted separately. Kalshi is
  reference-only in the frozen design and carries no fee leg in the
  headline PnL; ``kalshi_fee`` prices the Kalshi reference leg at the
  reference mid for the reported toll diagnostic only (see
  ``kalshi_reference_fee``), so the size of a hypothetical Kalshi fee is
  visible without altering the frozen arithmetic.
- Evaluation point: ONCE, at n >= 300 trigger windows spanning >= 7
  distinct UTC days. PASS = full-sample mean net > +0.005/contract AND
  both chronological halves > 0. KILL otherwise. If the minimums are not
  met within 21 days of collection going live (2026-11-19 ~02:12Z), the
  measurement is ACCRUAL FAILED -- no silent extensions.

Diligence conditions layered on top (2026-11-19 adversarial review --
reporting and parameter-freeze only; none of them moves a frozen number):

1. Every readout is printed three ways: pooled, per-asset, and restricted
   to windows opening after the Polymarket fee change
   (2026-11-20T23:59:00Z). A PASS manufactured by mixing fee regimes is
   visible as such.
2. Strike alignment, frozen HERE, before any evaluation run: a window
   pairs only when the two venues' reference levels -- Polymarket's
   price-to-beat vs Kalshi's strike (``open_reference``) -- agree within
   ``STRIKE_ALIGNMENT_TOLERANCE_REL``. Beyond it the window is a basis
   event, excluded and counted, never a trigger. Rationale for 0.20%:
   the two references are two venues' 60-second averages of the same
   underlying over the same open minute (Chainlink TWAP-60s vs CF
   Benchmarks BRTI 60s average), so a legitimate same-window pair should
   nearly coincide -- the pre-test measured the median |Kalshi strike -
   spot open| at 0.09% of price. 0.20% is roughly twice that median,
   admitting ordinary index-construction noise while excluding the
   windows where the indices genuinely diverged. Those excluded windows
   matter: near the strike, a few tenths of a percent of spot is worth
   many probability cents, and the pre-test's residual cross-venue gap
   (mean |gap| fell only 10.34c -> 7.39c over five minutes) is what a
   fair basis looks like -- a "gap" measured on a strike-diverged window
   is basis compensation, not lag, and counting it would rig the
   measurement toward PASS. The report prints the strike-divergence
   distribution so the excluded share is auditable.
   Kalshi reference-book age is likewise reported (and a Kalshi book
   that has not changed for the whole entry offset is excluded as
   stale): Kalshi snapshots carry receipt time, not a source clock, so
   a trigger fired against a minutes-old unchanged book would measure
   Polymarket converging to a ghost.
3. (Process condition: the portfolio stop rule lives with Felix, not in
   code. This tool only refuses to print a verdict early.)
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.convergence_quotes import (
    CHECKPOINT_TOLERANCE_SECONDS,
    CLIP_SHARES,
    ENTRY_OFFSET_SECONDS,
    EXIT_OFFSET_SECONDS,
    select_checkpoint,
)
from parlay_bot.perpetuals.kalshi_updown import kalshi_fee
from parlay_bot.perpetuals.polymarket_learning import polymarket_fee

ASSETS = ("BTC", "ETH", "SOL", "XRP")

# --- Frozen criteria (2026-11-18 re-measurement report, section 3) ---
TRIGGER_GAP = 0.035  # Kalshi mid - PM depth-walked avg ask, PM-cheap only
SECONDARY_FEE_ONLY_GAP = 0.0175  # reported as a count only, never a trigger
PASS_MEAN_NET = 0.005  # full-sample mean net per contract must exceed this
MIN_TRIGGER_WINDOWS = 300
MIN_DISTINCT_DAYS = 7
COLLECTION_START = datetime(2026, 11, 19, 2, 12, tzinfo=UTC)
ACCRUAL_WINDOW_DAYS = 21
ACCRUAL_DEADLINE = COLLECTION_START + timedelta(days=ACCRUAL_WINDOW_DAYS)

# --- Diligence condition 1: fee-regime split ---
FEE_CHANGE_AT = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)

# --- Diligence condition 2: strike alignment + Kalshi reference age ---
# Relative tolerance on the two reference levels, as a fraction of their
# mean. See the module docstring for the rationale (roughly twice the
# pre-test's measured 0.09% median legitimate strike divergence).
STRIKE_ALIGNMENT_TOLERANCE_REL = 0.0020
# A Kalshi book whose payload has not changed for longer than the entry
# offset itself carries no t=5 information; the window is excluded and
# counted as a stale reference rather than measured against a ghost mid.
KALSHI_REFERENCE_STALE_SECONDS = ENTRY_OFFSET_SECONDS

EXCLUSION_REASONS = (
    "strike_unavailable",
    "strike_divergence",
    "missing_snapshot",
    "stale_kalshi_reference",
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


def kalshi_reference_fee(mid: float) -> float:
    """What one Kalshi contract at the reference mid would cost in fees.

    Diagnostic only: the frozen headline PnL charges no Kalshi fee leg
    (Kalshi is the reference, the hypothetical trade is Polymarket-only),
    but the Kalshi taker toll at the trigger moment is part of reading
    the gap honestly, and Kalshi snapshots store fee_rate=None, so the
    toll must come from ``kalshi_fee`` rather than a recorded rate.
    """
    return kalshi_fee(mid)


def _kalshi_book_age_seconds(kalshi_rows, entry_row, window_open: datetime) -> float:
    """Seconds the Kalshi book at the entry checkpoint had been unchanged.

    Kalshi snapshots are receipt-timed and every fetch stores a row, so
    the book's age is the distance back to the most recent earlier
    snapshot with a different ``book_sha256``; if the book never changed
    since the first snapshot at/after the open, the age is measured
    from the window open (a lower bound, stated as such in the report).
    """
    entry_at = _parse_time(entry_row["source_at"])
    if entry_at is None:
        return 0.0
    age = (entry_at - window_open).total_seconds()
    for row in kalshi_rows:
        at = _parse_time(row.get("source_at"))
        if at is None or at >= entry_at:
            continue
        if row.get("book_sha256") != entry_row.get("book_sha256"):
            age = min(age, (entry_at - at).total_seconds())
    return max(0.0, age)


def _summarise(trades: list[dict]) -> dict:
    """One reporting cut: n, days, effect sizes, day-block interval."""
    n = len(trades)
    if not n:
        return {
            "n": 0, "distinct_days": 0, "mean_net_per_contract": None,
            "median_net_per_contract": None, "win_rate": None,
            "mean_net_per_dollar": None, "day_block_interval": None,
            "chronological_halves": None,
        }
    pnls = sorted(t["pnl_per_contract"] for t in trades)
    mean = sum(pnls) / n
    median = pnls[n // 2] if n % 2 else (pnls[n // 2 - 1] + pnls[n // 2]) / 2
    days = sorted({t["window_open"].date().isoformat() for t in trades})
    per_dollar = [
        t["pnl_per_contract"] / t["entry_cost_per_contract"] for t in trades
        if t["entry_cost_per_contract"] > 0
    ]
    # Day-block interval: day means as the sampling units, normal approx.
    interval = None
    if len(days) >= 2:
        by_day: dict[str, list[float]] = {}
        for t in trades:
            by_day.setdefault(t["window_open"].date().isoformat(), []).append(
                t["pnl_per_contract"])
        day_means = [sum(v) / len(v) for v in by_day.values()]
        day_mean = sum(day_means) / len(day_means)
        var = sum((m - day_mean) ** 2 for m in day_means) / (len(day_means) - 1)
        half = 1.96 * math.sqrt(var / len(day_means))
        interval = {"lower": day_mean - half, "upper": day_mean + half,
                    "days": len(day_means)}
    halves = None
    if n >= 2:
        ordered = sorted(trades, key=lambda t: t["window_open"])
        split = n // 2
        first = [t["pnl_per_contract"] for t in ordered[:split]]
        second = [t["pnl_per_contract"] for t in ordered[split:]]
        halves = {"first_mean": sum(first) / len(first),
                  "second_mean": sum(second) / len(second),
                  "first_n": len(first), "second_n": len(second)}
    return {
        "n": n, "distinct_days": len(days),
        "mean_net_per_contract": mean,
        "median_net_per_contract": median,
        "win_rate": sum(1 for p in pnls if p > 0) / n,
        "mean_net_per_dollar": (sum(per_dollar) / len(per_dollar)
                                if per_dollar else None),
        "day_block_interval": interval,
        "chronological_halves": halves,
    }


def evaluate_windows(windows: list[dict], *, now: datetime) -> dict:
    """Apply the frozen criteria to paired-window inputs (pure, no I/O).

    Each window: {asset, window_open (datetime), pm_snapshots,
    kalshi_snapshots, pm_reference, kalshi_reference}. Snapshot rows are
    stored-row dicts (ISO ``source_at``, best/avg bid/ask, fee_rate,
    book_sha256). Below the frozen evaluation point the returned report
    carries ``verdict: None`` -- an accrual state, never a PASS/KILL.
    """
    clock = now.astimezone(UTC)
    exclusions = {reason: 0 for reason in EXCLUSION_REASONS}
    checkpoint_missing = {"pm_entry": 0, "kalshi_entry": 0, "pm_exit": 0}
    trades: list[dict] = []
    non_trigger = 0
    secondary_line_triggers = 0
    paired = 0
    with_entry_and_exit = 0
    kalshi_ages: list[float] = []
    strike_divergences: list[float] = []
    fee_verified = 0
    fee_seen = 0

    for window in windows:
        asset = str(window.get("asset") or "")
        opened = window["window_open"].astimezone(UTC)
        pm_rows = list(window.get("pm_snapshots") or [])
        kalshi_rows = list(window.get("kalshi_snapshots") or [])
        if pm_rows and kalshi_rows:
            paired += 1
        # --- Strike alignment (diligence condition 2, frozen above) ---
        pm_ref = window.get("pm_reference")
        kalshi_ref = window.get("kalshi_reference")
        if (pm_ref is None or kalshi_ref is None
                or not math.isfinite(pm_ref) or not math.isfinite(kalshi_ref)
                or pm_ref <= 0 or kalshi_ref <= 0):
            exclusions["strike_unavailable"] += 1
            continue
        divergence = abs(pm_ref - kalshi_ref) / ((pm_ref + kalshi_ref) / 2)
        strike_divergences.append(divergence)
        if divergence > STRIKE_ALIGNMENT_TOLERANCE_REL:
            exclusions["strike_divergence"] += 1
            continue
        # --- Checkpoints (frozen selection rule) ---
        pm_entry = select_checkpoint(
            pm_rows, window_open=opened,
            offset_seconds=ENTRY_OFFSET_SECONDS)
        kalshi_entry = select_checkpoint(
            kalshi_rows, window_open=opened,
            offset_seconds=ENTRY_OFFSET_SECONDS)
        pm_exit = select_checkpoint(
            pm_rows, window_open=opened,
            offset_seconds=EXIT_OFFSET_SECONDS)
        missing = False
        if pm_entry is None:
            checkpoint_missing["pm_entry"] += 1
            missing = True
        if kalshi_entry is None:
            checkpoint_missing["kalshi_entry"] += 1
            missing = True
        if pm_exit is None:
            checkpoint_missing["pm_exit"] += 1
            missing = True
        if missing:
            exclusions["missing_snapshot"] += 1
            continue
        with_entry_and_exit += 1
        # --- Kalshi reference age (condition 2 reporting + stale rule) ---
        age = _kalshi_book_age_seconds(kalshi_rows, kalshi_entry, opened)
        kalshi_ages.append(age)
        if age > KALSHI_REFERENCE_STALE_SECONDS:
            exclusions["stale_kalshi_reference"] += 1
            continue
        # --- Trigger (frozen, PM-cheap only) ---
        if (kalshi_entry.get("best_bid") is None
                or kalshi_entry.get("best_ask") is None):
            exclusions["missing_snapshot"] += 1
            checkpoint_missing["kalshi_entry"] += 1
            continue
        if pm_entry.get("avg_ask") is None:
            exclusions["unfillable_leg"] += 1
            continue
        kalshi_mid = (float(kalshi_entry["best_bid"])
                      + float(kalshi_entry["best_ask"])) / 2
        entry_ask = float(pm_entry["avg_ask"])
        gap = kalshi_mid - entry_ask
        if gap > SECONDARY_FEE_ONLY_GAP:
            secondary_line_triggers += 1
        if gap <= TRIGGER_GAP:
            non_trigger += 1
            continue
        # --- Fee verification (frozen: unverified is out of the headline) ---
        fee_seen += 1
        entry_rate = pm_entry.get("fee_rate")
        if entry_rate is None:
            exclusions["fee_unverified"] += 1
            continue
        fee_verified += 1
        # --- Exit leg must fill the clip, never top-of-book ---
        if pm_exit.get("avg_bid") is None:
            exclusions["unfillable_leg"] += 1
            continue
        exit_rate = pm_exit.get("fee_rate")
        if exit_rate is None:
            exit_rate = entry_rate
        exit_bid = float(pm_exit["avg_bid"])
        fee_entry = polymarket_fee(entry_ask, float(entry_rate))
        fee_exit = polymarket_fee(exit_bid, float(exit_rate))
        pnl = exit_bid - entry_ask - fee_entry - fee_exit
        trades.append({
            "asset": asset, "window_open": opened,
            "pnl_per_contract": pnl,
            "entry_cost_per_contract": entry_ask + fee_entry,
            "gap": gap, "kalshi_mid": kalshi_mid,
            "kalshi_reference_fee": kalshi_reference_fee(kalshi_mid),
            "kalshi_book_age_seconds": age,
            "strike_divergence_rel": divergence,
        })

    pooled = _summarise(trades)
    per_asset = {
        asset: _summarise([t for t in trades if t["asset"] == asset])
        for asset in ASSETS
    }
    post_fee_change = _summarise(
        [t for t in trades if t["window_open"] > FEE_CHANGE_AT])
    n = pooled["n"]
    days = pooled["distinct_days"]
    mature = n >= MIN_TRIGGER_WINDOWS and days >= MIN_DISTINCT_DAYS
    if mature:
        halves = pooled["chronological_halves"] or {}
        passed = (pooled["mean_net_per_contract"] > PASS_MEAN_NET
                  and halves.get("first_mean", 0.0) > 0
                  and halves.get("second_mean", 0.0) > 0)
        verdict = "PASS" if passed else "KILL"
        status = "EVALUATED"
    elif clock > ACCRUAL_DEADLINE:
        verdict = None
        status = "ACCRUAL_FAILED"
    else:
        verdict = None
        status = "ACCRUAL"

    def _dist(values):
        if not values:
            return None
        ordered = sorted(values)
        return {"mean": sum(ordered) / len(ordered),
                "median": ordered[len(ordered) // 2],
                "max": ordered[-1], "n": len(ordered)}

    return {
        "schema": "rival-pm-lag-convergence-evaluation-v1",
        "evaluated_at": clock.isoformat(),
        "status": status,
        "verdict": verdict,
        "read_only": True,
        "real_money_orders": 0,
        "frozen_criteria": {
            "trigger_gap": TRIGGER_GAP,
            "pass_mean_net": PASS_MEAN_NET,
            "min_trigger_windows": MIN_TRIGGER_WINDOWS,
            "min_distinct_days": MIN_DISTINCT_DAYS,
            "clip_shares": CLIP_SHARES,
            "entry_offset_seconds": ENTRY_OFFSET_SECONDS,
            "exit_offset_seconds": EXIT_OFFSET_SECONDS,
            "checkpoint_tolerance_seconds": CHECKPOINT_TOLERANCE_SECONDS,
            "accrual_deadline": ACCRUAL_DEADLINE.isoformat(),
            "strike_alignment_tolerance_rel": STRIKE_ALIGNMENT_TOLERANCE_REL,
            "kalshi_reference_stale_seconds": KALSHI_REFERENCE_STALE_SECONDS,
            "fee_change_at": FEE_CHANGE_AT.isoformat(),
        },
        "coverage": {
            "windows_seen": len(windows),
            "windows_paired_both_venues": paired,
            "windows_with_entry_and_exit_checkpoints": with_entry_and_exit,
            "checkpoint_missing": checkpoint_missing,
            "exclusions": exclusions,
            "non_trigger_windows": non_trigger,
            "secondary_fee_only_line_triggers": secondary_line_triggers,
            "trigger_windows": n,
            "trigger_distinct_days": days,
            "fee_verified_rate": (fee_verified / fee_seen) if fee_seen else None,
            "kalshi_reference_book_age_seconds": _dist(kalshi_ages),
            "strike_divergence_rel": _dist(strike_divergences),
        },
        "cuts": {
            "pooled": pooled,
            "per_asset": per_asset,
            "post_fee_change_only": post_fee_change,
        },
    }


def _reference_from_evidence(raw) -> float | None:
    """price_to_beat out of a stored canonical evidence JSON, or None."""
    if not raw:
        return None
    try:
        evidence = json.loads(str(raw))
        value = float(evidence["price_to_beat"])
    except (TypeError, ValueError, KeyError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def load_windows(store) -> list[dict]:
    """Read snapshots + venue reference levels from the hot store.

    SELECT-only. Reference levels: Kalshi strike from
    ``rival_kalshi_updown.open_reference`` by ticker; Polymarket
    price-to-beat from the evidence JSON of
    ``rival_poly_oracle_observations`` (fallback:
    ``rival_polymarket_ai_calls``) by slug. A window whose reference
    cannot be found keeps ``None`` and is excluded downstream as
    ``strike_unavailable`` -- never approximated from a spot proxy.
    """
    def _exists(conn, table: str) -> bool:
        # An absent reference table means the level is unavailable for
        # every window (counted downstream), never an evaluator error.
        if store.database.backend == "postgres":
            found = conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_name=?", (table,)).fetchone()
        else:
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name=?", (table,)).fetchone()
        return found is not None

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT venue, asset, contract_id, window_open, source_at,"
            " received_at, best_bid, best_ask, avg_bid, avg_ask, fee_rate,"
            " book_sha256 FROM rival_convergence_book_snapshots"
            " ORDER BY asset, window_open, venue, source_at",
        ).fetchall()
        kalshi_refs: dict[str, float] = {}
        if _exists(conn, "rival_kalshi_updown"):
            for row in conn.execute(
                    "SELECT ticker, open_reference FROM rival_kalshi_updown"
                    " WHERE open_reference IS NOT NULL").fetchall():
                kalshi_refs[str(row["ticker"])] = float(row["open_reference"])
        pm_refs: dict[str, float] = {}
        for table in ("rival_poly_oracle_observations",
                      "rival_polymarket_ai_calls"):
            if not _exists(conn, table):
                continue
            for row in conn.execute(
                    f"SELECT slug, evidence_json FROM {table}").fetchall():
                slug = str(row["slug"])
                if slug not in pm_refs:
                    value = _reference_from_evidence(row["evidence_json"])
                    if value is not None:
                        pm_refs[slug] = value

    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        record = dict(row)
        key = (str(record["asset"]), str(record["window_open"]))
        window = grouped.setdefault(key, {
            "asset": key[0],
            "window_open": _parse_time(record["window_open"]),
            "pm_snapshots": [], "kalshi_snapshots": [],
            "pm_reference": None, "kalshi_reference": None,
        })
        if window["window_open"] is None:
            continue
        if record["venue"] == "polymarket":
            window["pm_snapshots"].append(record)
            ref = pm_refs.get(str(record["contract_id"]))
            if ref is not None:
                window["pm_reference"] = ref
        elif record["venue"] == "kalshi":
            window["kalshi_snapshots"].append(record)
            ref = kalshi_refs.get(str(record["contract_id"]))
            if ref is not None:
                window["kalshi_reference"] = ref
    return [w for w in grouped.values() if w["window_open"] is not None]
