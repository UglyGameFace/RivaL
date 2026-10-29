"""Immutable, pre-outcome executable-quote *shadow* profitability accounting.

Only live USD-M bid/ask snapshots recorded while the original prospectively
issued forecast is active may open a shadow position; only a fresh quote
near its expected exit may close it. No backfilled candles, fake fills,
leverage, orders, broker receipts or guarantee of an executable trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL
from parlay_bot.perpetuals.live_quotes import BookQuote
from parlay_bot.storage.hot import RelationalHotStore

ENTRY_GRACE = timedelta(seconds=90)
EXIT_GRACE = timedelta(seconds=90)
NOTIONAL_USDT = 25.0
MIN_FORWARD_EVENTS = 150
MIN_FORWARD_DAYS = 21
MIN_OBSERVED_COVERAGE = 0.80
COST_SCENARIOS = (
    ("conservative", 8.0 + 4.0 + 2.0),
    ("severe", 25.0 + 30.0 + 25.0),
)


def _stamp(raw: object) -> datetime:
    stamp = datetime.fromisoformat(str(raw))
    if stamp.utcoffset() is None:
        raise ValueError("Quote ledger needs timezone-aware timestamps")
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class ProfitProof:
    asset: str
    horizon_minutes: int
    status: str
    eligible_forecast_windows: int
    recorded_entries: int
    verified_quote_exits: int
    missing_exits: int
    coverage: float
    utc_days: int
    net_conservative_bps: float | None
    net_severe_bps: float | None
    conservative_daily_lower_bps: float | None
    severe_daily_lower_bps: float | None
    quote_shadow_qualified: bool
    real_fills_verified: bool
    explanation: str


def _daily_stress(values: list[tuple[str, float]]) -> tuple[float | None, float | None, int]:
    if not values:
        return None, None, 0
    daily: dict[str, list[float]] = {}
    for date, bps in values:
        if not math.isfinite(bps):
            raise ValueError("Nonfinite quote return")
        daily.setdefault(date, []).append(bps)
    daily_means = [
        sum(items) / len(items) for _, items in sorted(daily.items())
    ]
    days = len(daily_means)
    mean = sum(bps for _, bps in values) / len(values)
    day_mean = sum(daily_means) / days
    variance = (
        sum((x - day_mean) ** 2 for x in daily_means) / (days - 1)
        if days >= 2 else 0.0
    )
    # Day-block stress heuristic, not a valid universal confidence interval.
    lower = day_mean - 2.58 * math.sqrt(variance / days) if days >= 2 else None
    return mean, lower, sum(x > 0 for x in daily_means)


class ProspectiveQuoteProfitMonitor:
    """Read-only quote forward research. No exchange account API is touched."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_forward_quote_profit (
                     asset TEXT NOT NULL,
                     horizon_minutes INTEGER NOT NULL,
                     target_close_at TEXT NOT NULL,
                     model_version TEXT NOT NULL,
                     forecast_venue TEXT NOT NULL,
                     forecast_contract TEXT NOT NULL,
                     forecast_issued_at TEXT NOT NULL,
                     probability_up DOUBLE PRECISION NOT NULL,
                     quote_venue TEXT NOT NULL,
                     quote_symbol TEXT NOT NULL,
                     side TEXT NOT NULL,
                     quantity DOUBLE PRECISION NOT NULL,
                     entry_observed_at TEXT NOT NULL,
                     entry_bid DOUBLE PRECISION NOT NULL,
                     entry_ask DOUBLE PRECISION NOT NULL,
                     entry_bid_quantity DOUBLE PRECISION NOT NULL,
                     entry_ask_quantity DOUBLE PRECISION NOT NULL,
                     state TEXT NOT NULL,
                     exit_observed_at TEXT,
                     exit_bid DOUBLE PRECISION,
                     exit_ask DOUBLE PRECISION,
                     gross_bps DOUBLE PRECISION,
                     PRIMARY KEY (asset,horizon_minutes,target_close_at)
                   );
                   CREATE INDEX IF NOT EXISTS idx_rival_forward_quote_exit
                   ON rival_forward_quote_profit (asset,state,target_close_at);
                """
            )

    def observe(self, quote: BookQuote, *, now: datetime | None = None) -> dict:
        """Append a quote to future-only positions; no retrodating or picking winners."""
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("Naive clock")
        clock = stamp.astimezone(UTC)
        quote.validate(now=clock)
        if quote.venue != "binance_usdm":
            raise ValueError("Unsupported shadow execution venue")
        self.initialize()
        result = {"opened": 0, "closed": 0, "expired": 0}
        with self.store.connect() as conn:
            # Any stale open shadow must be marked missing, never
            # retroactively 'closed' at a convenient later price.
            stale = conn.execute(
                """UPDATE rival_forward_quote_profit SET state='MISSED_EXIT'
                   WHERE asset=? AND state='OPEN' AND target_close_at<?""",
                (quote.asset, (clock - EXIT_GRACE).isoformat()),
            )
            result["expired"] = stale.rowcount
            closing = conn.execute(
                """SELECT * FROM rival_forward_quote_profit
                   WHERE asset=? AND state='OPEN'
                     AND target_close_at<=? AND target_close_at>=?
                   ORDER BY target_close_at LIMIT 6""",
                (
                    quote.asset, quote.exchange_at.isoformat(),
                    (quote.exchange_at - EXIT_GRACE).isoformat(),
                ),
            ).fetchall()
            for row in closing:
                qty = float(row["quantity"])
                if (
                    str(row["quote_venue"]) != quote.venue
                    or str(row["quote_symbol"]) != quote.symbol
                    or _stamp(row["entry_observed_at"]) >= quote.exchange_at
                    or qty > min(quote.bid_quantity, quote.ask_quantity)
                ):
                    continue
                side = str(row["side"])
                if side == "LONG":
                    gross = (quote.bid / float(row["entry_ask"]) - 1) * 10000
                elif side == "SHORT":
                    gross = (
                        (float(row["entry_bid"]) - quote.ask)
                        / float(row["entry_bid"]) * 10000
                    )
                else:
                    continue
                if not math.isfinite(gross):
                    continue
                updated = conn.execute(
                    """UPDATE rival_forward_quote_profit
                       SET state='EXIT_QUOTED',exit_observed_at=?,
                           exit_bid=?,exit_ask=?,gross_bps=?
                       WHERE asset=? AND horizon_minutes=?
                         AND target_close_at=? AND state='OPEN'""",
                    (
                        quote.exchange_at.isoformat(), quote.bid, quote.ask, gross,
                        quote.asset, row["horizon_minutes"], row["target_close_at"],
                    ),
                )
                result["closed"] += updated.rowcount
            # Pick the chronologically FIRST eligible pre-outcome forecast
            # from each horizon and UTC target, not the winning venue.
            forecasts = conn.execute(
                """SELECT asset,venue,contract,quote,horizon_minutes,
                          model_version,issued_at,target_opened_at,
                          probability_up
                   FROM perp_forward_audit
                   WHERE asset=? AND model_version=? AND issued_at<=?
                     AND issued_at>=? AND probability_up IS NOT NULL
                   ORDER BY issued_at ASC,venue ASC LIMIT 90""",
                (
                    quote.asset, MODEL, quote.exchange_at.isoformat(),
                    (quote.exchange_at - ENTRY_GRACE).isoformat(),
                ),
            ).fetchall()
            inspected: set[tuple[int, str]] = set()
            for row in forecasts:
                horizon = int(row["horizon_minutes"])
                if horizon not in (5, 15):
                    continue
                probability = float(row["probability_up"])
                if not math.isfinite(probability) or not (
                    0 <= probability <= 0.45 or 0.55 <= probability <= 1
                ):
                    continue
                issued = _stamp(row["issued_at"])
                close_at = _stamp(row["target_opened_at"]) + timedelta(minutes=5)
                key = (horizon, close_at.isoformat())
                if key in inspected:
                    continue
                inspected.add(key)
                if not issued <= quote.exchange_at <= issued + ENTRY_GRACE:
                    continue
                if not quote.exchange_at < close_at:
                    continue
                side = "LONG" if probability >= 0.55 else "SHORT"
                entry = quote.ask if side == "LONG" else quote.bid
                quantity = NOTIONAL_USDT / entry
                if quantity > min(quote.bid_quantity, quote.ask_quantity):
                    continue
                inserted = conn.execute(
                    """INSERT INTO rival_forward_quote_profit
                       (asset,horizon_minutes,target_close_at,model_version,
                        forecast_venue,forecast_contract,forecast_issued_at,
                        probability_up,quote_venue,quote_symbol,side,quantity,
                        entry_observed_at,entry_bid,entry_ask,
                        entry_bid_quantity,entry_ask_quantity,state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (
                        quote.asset, horizon, close_at.isoformat(), MODEL,
                        row["venue"], row["contract"], issued.isoformat(),
                        probability, quote.venue, quote.symbol, side, quantity,
                        quote.exchange_at.isoformat(), quote.bid, quote.ask,
                        quote.bid_quantity, quote.ask_quantity, "OPEN",
                    ),
                )
                result["opened"] += inserted.rowcount
        return result

    def expire_unobserved(self, *, now: datetime | None = None) -> int:
        """Call even during provider outages; missing quote is never a gain."""
        clock = now or datetime.now(UTC)
        if clock.utcoffset() is None:
            raise ValueError("Naive clock")
        self.initialize()
        with self.store.connect() as conn:
            result = conn.execute(
                """UPDATE rival_forward_quote_profit SET state='MISSED_EXIT'
                   WHERE state='OPEN' AND target_close_at<?""",
                ((clock.astimezone(UTC) - EXIT_GRACE).isoformat(),),
            )
        return result.rowcount

    def proof(
        self, *, asset: str, horizon_minutes: int,
        now: datetime | None = None,
    ) -> ProfitProof:
        if asset not in ("BTC", "ETH", "SOL") or horizon_minutes not in (5, 15):
            raise ValueError("Invalid proof market")
        instant = now or datetime.now(UTC)
        if instant.utcoffset() is None:
            raise ValueError("Naive research clock")
        clock = instant.astimezone(UTC)
        start = clock - timedelta(days=30)
        cutoff = clock - EXIT_GRACE
        self.initialize()
        with self.store.connect() as conn:
            eligible = conn.execute(
                """SELECT COUNT(DISTINCT target_opened_at) AS n
                   FROM perp_forward_audit
                   WHERE asset=? AND horizon_minutes=? AND model_version=?
                     AND issued_at>=? AND target_opened_at<?
                     AND (probability_up<=0.45 OR probability_up>=0.55)""",
                (asset, horizon_minutes, MODEL, start.isoformat(),
                 (cutoff - timedelta(minutes=5)).isoformat()),
            ).fetchone()
            rows = conn.execute(
                """SELECT * FROM rival_forward_quote_profit
                   WHERE asset=? AND horizon_minutes=? AND target_close_at>=?
                     AND target_close_at<=? ORDER BY target_close_at""",
                (asset, horizon_minutes, start.isoformat(), cutoff.isoformat()),
            ).fetchall()
        denominator = int(eligible["n"] or 0)
        completed = [x for x in rows if x["state"] == "EXIT_QUOTED"]
        missing = sum(x["state"] != "EXIT_QUOTED" for x in rows)
        coverage = len(completed) / denominator if denominator else 0.0
        days = len({_stamp(x["target_close_at"]).date() for x in completed})
        reports = []
        for label, assumed in COST_SCENARIOS:
            values = [
                (_stamp(x["target_close_at"]).date().isoformat(),
                 float(x["gross_bps"]) - assumed)
                for x in completed
            ]
            mean, lower, positive_days = _daily_stress(values)
            reports.append((label, mean, lower, positive_days))
        passed = (
            denominator >= MIN_FORWARD_EVENTS and len(completed) >= MIN_FORWARD_EVENTS
            and days >= MIN_FORWARD_DAYS and coverage >= MIN_OBSERVED_COVERAGE
            and all(
                mean is not None and mean > 0
                and lower is not None and lower > 0
                and plus >= math.ceil(days * 0.8)
                for _, mean, lower, plus in reports
            )
        )
        return ProfitProof(
            asset, horizon_minutes,
            "QUOTE_SHADOW_STRESS_PASS_NOT_REAL_PROFIT" if passed
            else "NOT_PROVEN",
            denominator, len(rows), len(completed), missing,
            min(1.0, coverage), days,
            reports[0][1], reports[1][1],
            reports[0][2], reports[1][2],
            passed, False,
            (
                "Prospective live bid/ask paper evaluation passed strict multi-cost "
                "stress; not real trading profit or an executable fill."
                if passed else
                "Insufficient or unprofitable genuinely forward quote evidence. "
                "Missed/stale quotes cannot be silently filled with candles."
            ),
        )
