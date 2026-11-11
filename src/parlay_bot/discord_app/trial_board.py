"""Every model on trial for one coin and timeframe, as one compact table.

The Performance page used to show five hand-picked trial rows, each its own
sentence, read with five separate 7-day queries. The authoritative trial
state already exists: the hourly promotion check stores, per exchange and
timeframe, every test model's days, independent results and paired gain
over the main model (``rival_family_promotion``), and early stop records
which models were cut (``rival_family_early_stop``). This reads both (two
small point reads) and folds the exchanges into one row per model.

One row per model, never per exchange: the exchanges move together, so the
row shows the furthest clock (days, results) and the result-weighted gain,
plus on how many exchanges it is ahead.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MIN_DAYS = 21
MIN_RESULTS = 150
# Below this many results a gain is noise, not a verdict.
TOO_FEW = 20
# A gain smaller than this (Brier) reads as level.
LEVEL_WITHIN = 0.0005
FRESH = timedelta(hours=48)
MAX_ROWS = 14

_NAMES = {
    "regime_v1": "Trend-only",
    "regime_v2": "Trend v2",
    "regime_live_v1": "Trend learn",
    "regime_live_fast_v1": "Fast trend",
    "regime_wild_v1": "Wild-only",
    "cross_v1": "Cross-exch",
    "cross_all_v1": "Cross-all",
    "liq_v1": "Liquidity",
    "flow_v1": "Order-flow 1",
    "flow_v2": "Order-flow 2",
    "flow_v3": "Order-flow 3",
    "calib_incumbent_v1": "Calibrated",
    "archive_v1": "Archive",
    "gbm_v1": "Boosted",
    "gaussian_nb_v1": "Naive Bayes",
    "knn21_v1": "Nearest-21",
}


def short_name(family: str) -> str:
    """A table-width everyday name; every 6-year version folds into one."""
    base = family.removesuffix("_venue")
    suffix = "/ex" if family.endswith("_venue") else ""
    if re.fullmatch(r"pooled_\d+m_[0-9a-f]+", base):
        return "6-year" + suffix
    if base.startswith("boosted_"):
        return "Boosted" + suffix
    if base.startswith("historical_"):
        return "History" + suffix
    return (_NAMES.get(base) or base[:12]) + suffix


@dataclass(frozen=True)
class TrialRow:
    name: str
    days: int
    results: int
    gain: float | None          # result-weighted Brier gain over the main model
    ahead: int                  # exchanges where it is ahead
    exchanges: int              # exchanges it is scored on
    stopped: int                # exchanges where early stop cut it
    passed: bool
    after_fees_bps: float | None  # severe-cost paired gain, bps per trade

    @property
    def status(self) -> str:
        if self.passed:
            return "PASSED"
        if self.exchanges and self.stopped >= self.exchanges:
            return "cut"
        if self.results < TOO_FEW or self.gain is None:
            return "early"
        if self.gain > LEVEL_WITHIN:
            return "ahead"
        if self.gain < -LEVEL_WITHIN:
            return "behind"
        return "level"


_ORDER = {"PASSED": 0, "ahead": 1, "level": 2, "early": 3, "behind": 4, "cut": 5}


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def fold(evidence_rows, stops, *, retired=frozenset()) -> list[TrialRow]:
    """One row per model from per-exchange promotion evidence.

    ``evidence_rows``: (venue, contract, evidence dict) per exchange.
    ``stops``: (venue, contract, family) cut by early stop.
    Versions of a model that share a name (6-year versions) keep only the
    most advanced one, like the status card.
    """
    stopped: dict[str, set[tuple[str, str]]] = {}
    for venue, contract, family in stops:
        stopped.setdefault(str(family), set()).add((str(venue), str(contract)))
    families: dict[str, list] = {}
    for venue, contract, evidence in evidence_rows:
        if not isinstance(evidence, dict):
            continue
        for family, info in evidence.items():
            if family in retired or not isinstance(info, dict):
                continue
            targets, days = info.get("targets"), info.get("days")
            if not isinstance(targets, int) or not isinstance(days, int):
                continue
            families.setdefault(str(family), []).append((str(venue), str(contract), info))
    rows: dict[str, TrialRow] = {}
    for family, scopes in families.items():
        weighted = weight = 0.0
        ahead = 0
        fees_sum = fees_weight = 0.0
        for _venue, _contract, info in scopes:
            gain = _finite(info.get("forward_gain"))
            n = int(info.get("targets") or 0)
            if gain is not None and n:
                weighted += gain * n
                weight += n
                ahead += gain > LEVEL_WITHIN
            severe = ((info.get("cost_proof") or {}).get("severe") or {})
            bps, windows = _finite(severe.get("paired_gain_bps")), int(severe.get("windows") or 0)
            if bps is not None and windows:
                fees_sum += bps * windows
                fees_weight += windows
        cut = len(stopped.get(family, set()) & {(v, c) for v, c, _ in scopes})
        row = TrialRow(
            name=short_name(family),
            days=max(int(info["days"]) for _, _, info in scopes),
            results=max(int(info["targets"]) for _, _, info in scopes),
            gain=weighted / weight if weight else None,
            ahead=ahead, exchanges=len(scopes), stopped=cut,
            passed=any(bool(info.get("qualified")) for _, _, info in scopes),
            after_fees_bps=fees_sum / fees_weight if fees_weight else None,
        )
        best = rows.get(row.name)
        if best is None or (row.days, row.results) > (best.days, best.results):
            rows[row.name] = row
    return sorted(rows.values(), key=lambda r: (_ORDER[r.status], -r.days, -r.results, r.name))


def _pts(gain: float | None) -> str:
    """Brier gain in points (x1000), never wider than 5 characters."""
    if gain is None:
        return "—"
    points = gain * 1000
    return f"{points:+.1f}" if abs(points) < 100 else f"{points:+.0f}"[:5]


def _count(n: int) -> str:
    """A count in at most 5 characters (1,400 · 12k · 12M), never misread."""
    if n < 10_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{min(n // 1_000_000, 9999)}M"


def table(rows: list[TrialRow]) -> str:
    """A monospace table that fits a phone: name, day, results, edge, status."""
    lines = [f"{'Model':<12} {'Day':>5} {'Res':>5} {'Edge':>5} Now"]
    for row in rows[:MAX_ROWS]:
        day = f"{min(row.days, 99)}/{MIN_DAYS}"
        res = _count(row.results)
        lines.append(f"{row.name[:12]:<12} {day:>5} {res:>5} {_pts(row.gain):>5} {row.status}")
    if len(rows) > MAX_ROWS:
        lines.append(f"… and {len(rows) - MAX_ROWS} more")
    return "```\n" + "\n".join(lines) + "\n```\n"


def leader_line(rows: list[TrialRow]) -> str:
    """The one sentence a trader wants: who is closest, and what is left."""
    passed = [r for r in rows if r.status == "PASSED"]
    if passed:
        return f"✅ **{passed[0].name}** passed every test and can replace the main model."
    ahead = [r for r in rows if r.status == "ahead"]
    if not ahead:
        return "No test model is beating the main model here yet."
    lead = max(ahead, key=lambda r: (r.days, r.results))
    left = []
    if lead.days < MIN_DAYS:
        left.append(f"{MIN_DAYS - lead.days} more days")
    if lead.results < MIN_RESULTS:
        left.append(f"{MIN_RESULTS - lead.results} more results")
    fees = ("" if lead.after_fees_bps is None else
            f", {'ahead' if lead.after_fees_bps > 0 else 'behind'} after fees")
    where = f"ahead on {lead.ahead} of {lead.exchanges} exchanges{fees}"
    need = (" Needs " + " and ".join(left) + " before it can be trusted."
            if left else " Waiting on the after-fees test.")
    return f"Closest: **{lead.name}** ({where}).{need}"


def read(store, *, asset: str, horizon: int, now: datetime | None = None,
         retired=frozenset()) -> dict:
    """Rows for this coin and timeframe, plus a count across every market.

    Two point reads on small tables (one row per exchange and timeframe).
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    since = (stamp - FRESH).isoformat()
    with store.connect() as conn:
        try:
            promotion = conn.execute(
                """SELECT venue,contract,asset,horizon_minutes,evidence_json
                   FROM rival_family_promotion WHERE evaluated_at>=?""",
                (since,),
            ).fetchall()
        except Exception:  # noqa: BLE001 - table appears after the first hourly check
            promotion = []
        try:
            stop_rows = conn.execute(
                """SELECT venue,contract,horizon_minutes,family
                   FROM rival_family_early_stop""",
            ).fetchall()
        except Exception:  # noqa: BLE001 - table appears after the first stop
            stop_rows = []
    here, everywhere = [], []
    contracts = set()
    for row in promotion:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        entry = (str(row["venue"]), str(row["contract"]), evidence)
        everywhere.append((entry, str(row["asset"]), int(row["horizon_minutes"])))
        if str(row["asset"]) == asset and int(row["horizon_minutes"]) == horizon:
            here.append(entry)
            contracts.add((str(row["venue"]), str(row["contract"])))
    stops_here = [(r["venue"], r["contract"], r["family"]) for r in stop_rows
                  if int(r["horizon_minutes"]) == horizon
                  and (str(r["venue"]), str(r["contract"])) in contracts]
    rows = fold(here, stops_here, retired=retired)
    total = passed = 0
    by_market: dict[tuple[str, int], list] = {}
    for entry, a, h in everywhere:
        by_market.setdefault((a, h), []).append(entry)
    for entries in by_market.values():
        folded = fold(entries, [], retired=retired)
        total += len(folded)
        passed += sum(r.passed for r in folded)
    return {"rows": rows, "markets": len(by_market), "trials": total, "passed": passed,
            "stops": len(stop_rows)}


def retired_families() -> frozenset[str]:
    """Test models whose issue switch is off: they can never advance."""
    try:
        from parlay_bot.perpetuals import forward_audit as fa
    except Exception:  # noqa: BLE001 - a flag read must never break the page
        return frozenset()
    return frozenset(family for family, flag in (
        ("flow_v1", "ISSUE_FLOW_CHALLENGERS"), ("flow_v2", "ISSUE_FLOW_CHALLENGERS"),
        ("flow_v3", "ISSUE_FLOW_V3_CHALLENGERS"), ("regime_v2", "ISSUE_REGIME_V2_CHALLENGERS"),
    ) if not getattr(fa, flag, True))


# --- Polymarket contest -----------------------------------------------------

CONTEST_CONTRACTS = 300
CONTEST_NAMES = {"Book logistic": "Order book", "GBM + EWMA": "Formula",
                 "Jump diffusion": "Jump model", "Queue pressure": "Pressure",
                 "Odds-lag learner": "Odds-lag"}


def contest_rows(models) -> list[dict]:
    """One row per contest model, keeping its best-covered settlement source."""
    best: dict[str, dict] = {}
    for item in models or ():
        if not isinstance(item, dict) or not item.get("model"):
            continue
        current = best.get(item["model"])
        if current is None or int(item.get("contracts") or 0) > int(current.get("contracts") or 0):
            best[item["model"]] = item
    def status(m):
        if m.get("significant"):
            return "PASSED"
        if int(m.get("contracts") or 0) < TOO_FEW:
            return "early"
        gain = float(m.get("improvement") or 0)
        return "ahead" if gain > LEVEL_WITHIN else "behind" if gain < -LEVEL_WITHIN else "level"
    rows = [{**m, "status": status(m)} for m in best.values()]
    return sorted(rows, key=lambda m: (_ORDER[m["status"]], -int(m.get("contracts") or 0)))


def contest_table(rows: list[dict]) -> str:
    """Name, contracts, edge vs the odds, after-fees result per $1."""
    lines = [f"{'Model':<12} {'Done':>5} {'Edge':>5} {'Fees':>6} Now"]
    for m in rows[:MAX_ROWS]:
        name = str(m.get("label") or m["model"])
        name = CONTEST_NAMES.get(name, name)
        done = int(m.get("contracts") or 0)
        net = _finite(m.get("net_per_contract"))
        fees = "—" if net is None or not int(m.get("trades") or 0) else f"{net * 100:+.1f}¢"
        lines.append(f"{name[:12]:<12} {_count(done):>5} "
                     f"{_pts(_finite(m.get('improvement'))):>5} {fees[:6]:>6} {m['status']}")
    return "```\n" + "\n".join(lines) + "\n```\n"
