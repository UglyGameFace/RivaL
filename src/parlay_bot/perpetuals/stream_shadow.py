"""Strict matched-venue forward quote shadow ledger and after-cost evidence.

No historical candles, inferred fills or alternative-venue prices are used.
Observe current books after prospective issuance, score later books only
inside a short exit window, and count every missed exit as missing evidence.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL
from parlay_bot.perpetuals.live_quotes import BookQuote, PerpReferenceBook
from parlay_bot.perpetuals.profit_proof import (
    COST_SCENARIOS,
    ENTRY_GRACE,
    EXIT_GRACE,
    MIN_FORWARD_DAYS,
    MIN_FORWARD_EVENTS,
    MIN_OBSERVED_COVERAGE,
    NOTIONAL_USDT,
    _daily_stress,
    _stamp,
)
from parlay_bot.storage.hot import RelationalHotStore

SUPPORTED_VENUES = ("hyperliquid", "binance")
MIN_SIGNAL_SCORE = 0.56


def _identity(quote: BookQuote | PerpReferenceBook) -> tuple[str, str, str]:
    if isinstance(quote, PerpReferenceBook):
        if quote.venue != "hyperliquid":
            raise ValueError("Invalid perp reference venue")
        return ("hyperliquid", quote.asset, "USDC")
    if isinstance(quote, BookQuote) and quote.venue == "binance_usdm":
        return ("binance", quote.symbol, "USDT")
    raise ValueError("Unsupported strict shadow market")


class StreamShadowLedger:
    """Only exchange-matched prospective top of book; read-only research."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_stream_shadow (
                    asset TEXT NOT NULL,venue TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,target_close_at TEXT NOT NULL,
                    model_version TEXT NOT NULL,issued_at TEXT NOT NULL,
                    probability_up DOUBLE PRECISION NOT NULL,
                    side TEXT NOT NULL,quantity DOUBLE PRECISION NOT NULL,
                    entry_at TEXT NOT NULL,entry_bid DOUBLE PRECISION NOT NULL,
                    entry_ask DOUBLE PRECISION NOT NULL,state TEXT NOT NULL,
                    exit_at TEXT,exit_bid DOUBLE PRECISION,exit_ask DOUBLE PRECISION,
                    gross_bps DOUBLE PRECISION,
                    PRIMARY KEY(asset,venue,horizon_minutes,target_close_at)
                );
                CREATE INDEX IF NOT EXISTS idx_rival_stream_exit
                ON rival_stream_shadow (asset,venue,state,target_close_at);
                """
            )
        self._initialized = True

    def expire(self, *, now: datetime | None = None) -> int:
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("Naive shadow clock")
        self.initialize()
        with self.store.connect() as conn:
            updated = conn.execute(
                """UPDATE rival_stream_shadow SET state='MISSED_EXIT'
                   WHERE state='OPEN' AND target_close_at<?""",
                ((stamp.astimezone(UTC) - EXIT_GRACE).isoformat(),),
            )
            return updated.rowcount

    def observe(
        self, quote: BookQuote | PerpReferenceBook, *, now: datetime | None = None,
    ) -> dict[str, int]:
        instant = now or datetime.now(UTC)
        if instant.utcoffset() is None:
            raise ValueError("Naive shadow clock")
        stamp = instant.astimezone(UTC)
        quote.validate(now=stamp)
        venue, contract, currency = _identity(quote)
        self.initialize()
        result = {"opened": 0, "closed": 0, "expired": 0}
        with self.store.connect() as conn:
            result["expired"] = conn.execute(
                """UPDATE rival_stream_shadow SET state='MISSED_EXIT'
                   WHERE asset=? AND venue=? AND state='OPEN' AND target_close_at<?""",
                (quote.asset, venue, (stamp - EXIT_GRACE).isoformat()),
            ).rowcount
            rows = conn.execute(
                """SELECT * FROM rival_stream_shadow
                   WHERE asset=? AND venue=? AND state='OPEN'
                     AND target_close_at<=? AND target_close_at>=?
                   ORDER BY target_close_at LIMIT 12""",
                (quote.asset, venue, quote.exchange_at.isoformat(),
                 (quote.exchange_at - EXIT_GRACE).isoformat()),
            ).fetchall()
            for row in rows:
                qty = float(row["quantity"])
                if (_stamp(row["entry_at"]) >= quote.exchange_at
                    or qty > min(quote.bid_quantity, quote.ask_quantity)):
                    continue
                gross = (
                    (quote.bid / float(row["entry_ask"]) - 1) * 10000
                    if row["side"] == "LONG" else
                    ((float(row["entry_bid"]) - quote.ask)
                     / float(row["entry_bid"])) * 10000
                )
                if not math.isfinite(gross):
                    continue
                result["closed"] += conn.execute(
                    """UPDATE rival_stream_shadow
                       SET state='EXIT_QUOTED',exit_at=?,exit_bid=?,
                           exit_ask=?,gross_bps=?
                       WHERE asset=? AND venue=? AND horizon_minutes=?
                         AND target_close_at=? AND state='OPEN'""",
                    (quote.exchange_at.isoformat(), quote.bid, quote.ask, gross,
                     quote.asset, venue, row["horizon_minutes"], row["target_close_at"]),
                ).rowcount
            forecasts = conn.execute(
                """SELECT asset,venue,contract,quote,horizon_minutes,
                          model_version,issued_at,target_opened_at,probability_up
                   FROM perp_forward_audit
                   WHERE asset=? AND venue=? AND contract=? AND quote=?
                     AND model_version=? AND issued_at<=? AND issued_at>=?
                   ORDER BY issued_at ASC LIMIT 24""",
                (quote.asset, venue, contract, currency, MODEL,
                 quote.exchange_at.isoformat(),
                 (quote.exchange_at - ENTRY_GRACE).isoformat()),
            ).fetchall()
            seen = set()
            for row in forecasts:
                horizon = int(row["horizon_minutes"])
                prob = float(row["probability_up"])
                if (horizon not in (5, 15) or not math.isfinite(prob)
                    or max(prob, 1 - prob) < MIN_SIGNAL_SCORE):
                    continue
                issued = _stamp(row["issued_at"])
                close_at = _stamp(row["target_opened_at"]) + timedelta(minutes=5)
                key = (horizon, close_at.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                if not issued <= quote.exchange_at <= issued + ENTRY_GRACE:
                    continue
                if quote.exchange_at >= close_at:
                    continue
                side = "LONG" if prob > 0.5 else "SHORT"
                price = quote.ask if side == "LONG" else quote.bid
                qty = NOTIONAL_USDT / price
                if qty > min(quote.bid_quantity, quote.ask_quantity):
                    continue
                result["opened"] += conn.execute(
                    """INSERT INTO rival_stream_shadow (
                       asset,venue,horizon_minutes,target_close_at,model_version,
                       issued_at,probability_up,side,quantity,entry_at,entry_bid,
                       entry_ask,state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (quote.asset, venue, horizon, close_at.isoformat(), MODEL,
                     issued.isoformat(), prob, side, qty,
                     quote.exchange_at.isoformat(), quote.bid, quote.ask, "OPEN"),
                ).rowcount
        return result

    def report(
        self, *, asset: str, horizon_minutes: int, now: datetime | None = None,
    ) -> dict:
        if asset not in ("BTC", "ETH", "SOL") or horizon_minutes not in (5, 15):
            raise ValueError("Invalid shadow proof market")
        instant = now or datetime.now(UTC)
        if instant.utcoffset() is None:
            raise ValueError("Naive shadow proof clock")
        stamp = instant.astimezone(UTC)
        start = stamp - timedelta(days=30)
        cutoff = stamp - EXIT_GRACE
        self.initialize()
        with self.store.connect() as conn:
            denominator = conn.execute(
                """SELECT COUNT(*) AS n FROM (
                     SELECT DISTINCT venue,target_opened_at FROM perp_forward_audit
                     WHERE asset=? AND venue IN ('binance','hyperliquid')
                       AND horizon_minutes=? AND model_version=?
                       AND issued_at>=? AND target_opened_at<?
                       AND (probability_up<=? OR probability_up>=?)
                   ) AS eligible""",
                (asset, horizon_minutes, MODEL, start.isoformat(),
                 (cutoff - timedelta(minutes=5)).isoformat(),
                 1 - MIN_SIGNAL_SCORE, MIN_SIGNAL_SCORE),
            ).fetchone()
            rows = conn.execute(
                """SELECT * FROM rival_stream_shadow
                   WHERE asset=? AND horizon_minutes=? AND target_close_at>=?
                     AND target_close_at<=? ORDER BY target_close_at""",
                (asset, horizon_minutes, start.isoformat(), cutoff.isoformat()),
            ).fetchall()
        total = int(denominator["n"] or 0)
        closed = [r for r in rows if r["state"] == "EXIT_QUOTED"]
        days = len({_stamp(r["target_close_at"]).date() for r in closed})
        coverage = len(closed) / total if total else 0.0
        metrics = {}
        for label, bps in COST_SCENARIOS:
            mean, lower, winning = _daily_stress([
                (_stamp(r["target_close_at"]).date().isoformat(),
                 float(r["gross_bps"]) - bps)
                for r in closed
            ])
            metrics[label] = {
                "assumed_extra_cost_bps": bps, "mean_net_bps": mean,
                "daily_lower_bps": lower, "positive_days": winning,
            }
        qualified = bool(
            total >= MIN_FORWARD_EVENTS and len(closed) >= MIN_FORWARD_EVENTS
            and days >= MIN_FORWARD_DAYS and coverage >= MIN_OBSERVED_COVERAGE
            and all(
                x["mean_net_bps"] is not None and x["mean_net_bps"] > 0
                and x["daily_lower_bps"] is not None and x["daily_lower_bps"] > 0
                and x["positive_days"] >= math.ceil(days * 0.8)
                for x in metrics.values()
            )
        )
        return {
            "status": "SHADOW_STRESS_PASS_NOT_REAL_PROFIT" if qualified
                      else "NOT_PROVEN",
            "asset": asset, "horizon_minutes": horizon_minutes,
            "eligible_windows": total, "entries": len(rows), "closed": len(closed),
            "missed_exits": sum(r["state"] == "MISSED_EXIT" for r in rows),
            "coverage": min(1.0, coverage), "days": days,
            "costs": metrics, "real_fills_verified": False,
            "qualified_shadow": qualified,
        }
