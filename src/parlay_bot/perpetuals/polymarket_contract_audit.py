"""Offline Polymarket contract-outcome audit; NEVER an auto-promotion path.

Unlike exchange-candle backtests, samples here refer to ONE exact Gamma
event and the corresponding Chainlink TWAP starting and ending reference.
Input provenance must be independently checked against official event/rules;
this program cannot prove that user-supplied rows are authentic. Historical
training and on-contract forward evidence remain distinct.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.polymarket_watch import market_slug

MIN_PROSPECTIVE_WINDOWS = 150
MIN_PROSPECTIVE_DAYS = 21
MIN_QUOTE_COVERAGE = 0.80


@dataclass(frozen=True)
class ContractComparison:
    """Externally-supplied prospective, source-matched contract observation."""

    slug: str
    asset: str
    horizon_minutes: int
    opens: datetime
    closes: datetime
    candidate_issued_at: datetime
    baseline_issued_at: datetime
    candidate_probability_up: float
    baseline_probability_up: float
    oracle_start_twap: float
    oracle_end_twap: float
    oracle_recorded_at: datetime
    oracle_provenance: str
    quote_observed_at: datetime | None = None
    up_ask: float | None = None
    down_ask: float | None = None


@dataclass(frozen=True)
class ContractAudit:
    status: str
    reason: str
    unique_windows: int
    days: int
    quoted_windows: int
    quote_coverage: float
    candidate_brier: float | None
    baseline_brier: float | None
    candidate_accuracy: float | None
    mean_after_cost_per_share: float | None
    eligible_trades: int
    last_window: str | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("all event, prediction and observation times need UTC offsets")
    return value.astimezone(UTC)


def _valid(row: ContractComparison) -> bool:
    try:
        start, end = _utc(row.opens), _utc(row.closes)
        candidate_at = _utc(row.candidate_issued_at)
        baseline_at = _utc(row.baseline_issued_at)
        oracle_at = _utc(row.oracle_recorded_at)
        # Pre-window forecasts are required; in-flight trades are not
        # compared against a price-to-beat that was already known.
        if (
            row.horizon_minutes not in (5, 15)
            or row.asset not in ("BTC", "ETH", "SOL", "XRP")
            or start.timestamp() % (row.horizon_minutes * 60) != 0
            or end - start != timedelta(minutes=row.horizon_minutes)
            or row.slug != market_slug(row.asset, row.horizon_minutes, start)
            or candidate_at > start or baseline_at > start
            or oracle_at <= end
            or not row.oracle_provenance.startswith("chainlink-twap:")
            or len(row.oracle_provenance) < 22
        ):
            return False
        values = (
            row.oracle_start_twap, row.oracle_end_twap,
            row.candidate_probability_up, row.baseline_probability_up,
        )
        if any(not math.isfinite(v) for v in values):
            return False
        if (
            min(row.oracle_start_twap, row.oracle_end_twap) <= 0
            or not 0 < row.candidate_probability_up < 1
            or not 0 < row.baseline_probability_up < 1
        ):
            return False
        has_quote = (row.up_ask is not None and row.down_ask is not None)
        if has_quote:
            if row.quote_observed_at is None:
                return False
            quote_time = _utc(row.quote_observed_at)
            if not (
                candidate_at <= quote_time <= start
                and timedelta(0) <= start - quote_time <= timedelta(minutes=10)
                and math.isfinite(row.up_ask)
                and math.isfinite(row.down_ask)
                and 0 < row.up_ask < 1 and 0 < row.down_ask < 1
            ):
                return False
        elif (
            row.up_ask is not None or row.down_ask is not None
            or row.quote_observed_at is not None
        ):
            return False
    except (ValueError, TypeError, OverflowError, AttributeError):
        return False
    return True


def audit_contract_predictions(
    observations: list[ContractComparison], *,
    min_windows: int = MIN_PROSPECTIVE_WINDOWS,
    min_days: int = MIN_PROSPECTIVE_DAYS,
    min_quote_coverage: float = MIN_QUOTE_COVERAGE,
    cost_per_share: float = 0.01,
) -> ContractAudit:
    """Read-only evaluation of already-issued market-matched predictions.

    Even a PASS means *candidate eligible for further independently verified
    forward research*, never automatic promotion or a profitable market claim.
    """
    if not (
        min_windows >= 20 and min_days >= 7
        and 0.5 <= min_quote_coverage <= 1
        and 0 <= cost_per_share < 1
    ):
        raise ValueError("invalid contract proof thresholds")
    distinct: dict[str, ContractComparison] = {}
    for row in observations:
        if not _valid(row) or row.slug in distinct:
            return ContractAudit(
                "blocked", "Invalid, duplicate, unmatched or noncausal contract row",
                0, 0, 0, 0, None, None, None, None, 0, None,
            )
        distinct[row.slug] = row
    rows = sorted(distinct.values(), key=lambda x: _utc(x.opens))
    n = len(rows)
    days = len({_utc(x.opens).date() for x in rows})
    quoted = [x for x in rows if x.up_ask is not None]
    coverage = len(quoted) / n if n else 0.0
    if not rows:
        return ContractAudit(
            "insufficient", "No source-aligned contract labels",
            0, 0, 0, 0, None, None, None, None, 0, None,
        )
    labels = [int(row.oracle_end_twap >= row.oracle_start_twap) for row in rows]
    c_brier = sum(
        (row.candidate_probability_up - actual)**2
        for row, actual in zip(rows, labels, strict=True)
    ) / n
    b_brier = sum(
        (row.baseline_probability_up - actual)**2
        for row, actual in zip(rows, labels, strict=True)
    ) / n
    accuracy = sum(
        int((row.candidate_probability_up >= 0.5) == actual)
        for row, actual in zip(rows, labels, strict=True)
    ) / n

    # A fixed, previously specified research threshold; changing it to fit
    # held-out outcomes would leak future information into the test.
    profits: list[float] = []
    for row in quoted:
        probability = row.candidate_probability_up
        if not (probability >= 0.56 or probability <= 0.44):
            continue
        outcome_up = row.oracle_end_twap >= row.oracle_start_twap
        choosing_up = probability >= 0.56
        ask = row.up_ask if choosing_up else row.down_ask
        profits.append(float(choosing_up == outcome_up) - ask - cost_per_share)
    mean_pnl = sum(profits) / len(profits) if profits else None
    if n < min_windows or days < min_days:
        status, reason = (
            "insufficient",
            f"Need {min_windows} unique windows over {min_days} UTC days",
        )
    elif coverage < min_quote_coverage:
        status, reason = "insufficient", "Insufficient contemporaneous CLOB quote coverage"
    elif c_brier >= b_brier:
        status, reason = "rejected", "Candidate does not beat paired baseline Brier"
    elif len(profits) < 20 or mean_pnl is None or mean_pnl <= 0:
        status, reason = "rejected", "No positive after-cost directional evidence"
    else:
        status, reason = (
            "research_only",
            "Candidate passes descriptive screens; provenance and "
            "independent forward validation still required",
        )
    return ContractAudit(
        status=status, reason=reason, unique_windows=n, days=days,
        quoted_windows=len(quoted), quote_coverage=coverage,
        candidate_brier=c_brier, baseline_brier=b_brier,
        candidate_accuracy=accuracy, mean_after_cost_per_share=mean_pnl,
        eligible_trades=len(profits), last_window=rows[-1].slug,
    )
