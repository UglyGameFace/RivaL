"""Rolling two-year-first acquisition policy for independent venue sources.

Keep actual calendar completed-month original archives separate from the
current open month; exact two-year instant is tracked independently. No
historical data is discarded and no provider listing dates are inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

COMPLETE_MONTHS = 24
# Earliest month any supported official archive can hold: Binance USD-M
# BTCUSDT perpetual trading began September 2019. Later-listed contracts
# return 404 for earlier months and are recorded as unavailable, never filled.
EARLIEST_ARCHIVE_MONTH = "2019-09"
FAST_RECENT_MONTHS = 6


@dataclass(frozen=True)
class Window:
    exact_rolling_start_utc: str
    snapshot_at_utc: str
    first_completed_month: str
    last_completed_month: str
    completed_months: tuple[str, ...]
    live_partial_month: str

    def metadata(self) -> dict:
        return {
            "policy": "rolling_24_completed_months_first_older_history_secondary",
            "exact_rolling_start_utc": self.exact_rolling_start_utc,
            "snapshot_at_utc": self.snapshot_at_utc,
            "first_completed_utc_month": self.first_completed_month,
            "last_completed_utc_month": self.last_completed_month,
            "completed_utc_months": list(self.completed_months),
            "live_partial_utc_month": self.live_partial_month,
            "completed_monthly_archive_can_precede_exact_start": True,
            "original_source_byte_readbacks_verified_by_policy": False,
            "contract_listing_dates_proven_by_policy": False,
            "live_model_admission_allowed": False,
        }


def window(now: datetime | None = None) -> Window:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("source acquisition priority requires an aware clock")
    instant = instant.astimezone(UTC)
    try:
        start = instant.replace(year=instant.year - 2)
    except ValueError:  # Feb 29: two prior nonleap years
        start = instant.replace(year=instant.year - 2, day=28)
    current = instant.year * 12 + instant.month - 1
    months = []
    for index in range(current - COMPLETE_MONTHS, current):
        year, zero_based = divmod(index, 12)
        months.append(f"{year:04d}-{zero_based + 1:02d}")
    return Window(
        exact_rolling_start_utc=start.isoformat(),
        snapshot_at_utc=instant.isoformat(),
        first_completed_month=months[0],
        last_completed_month=months[-1],
        completed_months=tuple(months),
        live_partial_month=instant.strftime("%Y-%m"),
    )


def ordered_months(
    available: list[str] | tuple[str, ...], *, now: datetime | None = None,
    recent: int = FAST_RECENT_MONTHS,
) -> list[str]:
    """Recent six, then other rolling-24, then older. Never invent a month.

    Explicit source publication dates/listings remain the caller's job.
    """
    if not 1 <= recent <= COMPLETE_MONTHS:
        raise ValueError("invalid recent research priority")
    w = window(now)
    allowed = set(w.completed_months)
    if len(set(available)) != len(available):
        raise ValueError("duplicate source month in priority input")
    if any(not isinstance(m, str) or len(m) != 7 or m[4] != "-"
           or m < EARLIEST_ARCHIVE_MONTH or m >= w.live_partial_month
           or m[5:] not in {f"{i:02d}" for i in range(1, 13)}
           for m in available):
        raise ValueError("source months must be completed YYYY-MM archive months")
    within = sorted((m for m in available if m in allowed), reverse=True)
    older = sorted((m for m in available if m not in allowed), reverse=True)
    # A distinct six-month band is retained to explain research scheduling;
    # all 24 months get ahead of any older year.
    return within[:recent] + within[recent:] + older
