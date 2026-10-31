"""Matched-venue quote shadow for each learned model family.

No candle is a fill; no order is placed. Both candidate and incumbent are
priced against the SAME timestamped source book and same exit opportunity.
Absent quotes and missed exits are tracked as missing, not zero-cost wins.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.live_quotes import BookQuote, PerpReferenceBook
from parlay_bot.perpetuals.model_competition import FAMILIES
from parlay_bot.perpetuals.profit_proof import (
    ENTRY_GRACE,
    EXIT_GRACE,
    NOTIONAL_USDT,
)

SCORE_THRESHOLD = .56
MAX_ROWS = 60


def identity(quote):
    if isinstance(quote, PerpReferenceBook) and quote.venue == "hyperliquid":
        return "hyperliquid", quote.asset, "USDC"
    if isinstance(quote, BookQuote) and quote.venue == "binance_usdm":
        return "binance", quote.symbol, "USDT"
    raise ValueError("unmatched provider quote")


def side(probability: float) -> str:
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("invalid model probability")
    if probability >= SCORE_THRESHOLD:
        return "LONG"
    if probability <= 1 - SCORE_THRESHOLD:
        return "SHORT"
    return "CASH"


def gross_bps(row, quote, side_key: str) -> float:
    if side_key == "CASH":
        return 0.0
    if side_key == "LONG":
        return (float(quote.bid)/float(row["entry_ask"])-1)*10000
    if side_key == "SHORT":
        return (float(row["entry_bid"])-float(quote.ask))/float(row["entry_bid"])*10000
    raise ValueError("invalid quoted side")


class FamilyQuoteShadow:
    def __init__(self, store):
        self.store = store
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS rival_family_quote_shadow (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                asset TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
                family TEXT NOT NULL,reference_opened_at TEXT NOT NULL,
                issued_at TEXT NOT NULL,target_close_at TEXT NOT NULL,
                training_sha256 TEXT NOT NULL,
                challenger_up DOUBLE PRECISION NOT NULL,
                incumbent_up DOUBLE PRECISION NOT NULL,
                challenger_side TEXT NOT NULL,incumbent_side TEXT NOT NULL,
                entry_at TEXT NOT NULL,entry_bid DOUBLE PRECISION NOT NULL,
                entry_ask DOUBLE PRECISION NOT NULL,quantity DOUBLE PRECISION NOT NULL,
                state TEXT NOT NULL,exit_at TEXT,exit_bid DOUBLE PRECISION,
                exit_ask DOUBLE PRECISION,challenger_gross_bps DOUBLE PRECISION,
                incumbent_gross_bps DOUBLE PRECISION,
                PRIMARY KEY(venue,contract,quote,horizon_minutes,family,reference_opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_rival_family_quote_exit
              ON rival_family_quote_shadow(state,target_close_at);
            CREATE INDEX IF NOT EXISTS idx_rival_family_quote_source
              ON rival_family_quote_shadow(asset,venue,contract,quote,
                  horizon_minutes,family,target_close_at);
            """)
        self._initialized = True

    def observe(self, quote, *, now=None):
        clock = now or datetime.now(UTC)
        if clock.utcoffset() is None:
            raise ValueError("naive quote observation")
        clock = clock.astimezone(UTC)
        quote.validate(now=clock)
        venue, contract, currency = identity(quote)
        self.initialize()
        data = {"opened":0,"closed":0,"missed":0}
        with self.store.connect() as conn:
            data["missed"] = conn.execute(
                """UPDATE rival_family_quote_shadow SET state='MISSED_EXIT'
                   WHERE venue=? AND contract=? AND quote=? AND asset=?
                    AND state='OPEN' AND target_close_at<?""",
                (venue,contract,currency,quote.asset,
                 (clock-EXIT_GRACE).isoformat()),
            ).rowcount
            exits = conn.execute(
                """SELECT * FROM rival_family_quote_shadow
                   WHERE venue=? AND contract=? AND quote=? AND asset=?
                    AND state='OPEN' AND target_close_at<=?
                    AND target_close_at>=? ORDER BY target_close_at LIMIT ?""",
                (venue,contract,currency,quote.asset,quote.exchange_at.isoformat(),
                 (quote.exchange_at-EXIT_GRACE).isoformat(),MAX_ROWS),
            ).fetchall()
            for row in exits:
                entry=datetime.fromisoformat(row["entry_at"])
                if (entry >= quote.exchange_at
                        or float(row["quantity"]) >
                        min(float(quote.bid_quantity),float(quote.ask_quantity))):
                    continue
                challenger = gross_bps(row,quote,str(row["challenger_side"]))
                incumbent = gross_bps(row,quote,str(row["incumbent_side"]))
                if not all(map(math.isfinite,(challenger,incumbent))):
                    continue
                data["closed"] += conn.execute(
                    """UPDATE rival_family_quote_shadow SET state='EXIT_QUOTED',
                       exit_at=?,exit_bid=?,exit_ask=?,challenger_gross_bps=?,
                       incumbent_gross_bps=?
                       WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?
                        AND family=? AND reference_opened_at=? AND state='OPEN'""",
                    (quote.exchange_at.isoformat(),quote.bid,quote.ask,
                     challenger,incumbent,venue,contract,currency,
                     row["horizon_minutes"],row["family"],row["reference_opened_at"]),
                ).rowcount
            issued = conn.execute(
                """SELECT venue,contract,quote,asset,horizon_minutes,family,
                          reference_opened_at,issued_at,target_opened_at,
                          training_sha256,probability_up,incumbent_up
                   FROM rival_model_shadow_forecasts
                   WHERE venue=? AND contract=? AND quote=? AND asset=?
                    AND issued_at>=? AND issued_at<=?
                   ORDER BY issued_at LIMIT ?""",
                (venue,contract,currency,quote.asset,
                 (quote.exchange_at-ENTRY_GRACE).isoformat(),
                 quote.exchange_at.isoformat(),MAX_ROWS),
            ).fetchall()
            for row in issued:
                if (row["family"] not in FAMILIES
                        and not (str(row["family"]).startswith("boosted_")
                                 and len(str(row["family"])) <= 64)):
                    continue
                at=datetime.fromisoformat(row["issued_at"])
                target=datetime.fromisoformat(row["target_opened_at"])+timedelta(minutes=5)
                if (at > quote.exchange_at or
                        quote.exchange_at > at+ENTRY_GRACE or quote.exchange_at>=target):
                    continue
                challenger = side(float(row["probability_up"]))
                incumbent = side(float(row["incumbent_up"]))
                if challenger=="CASH" and incumbent=="CASH":
                    continue
                qty=NOTIONAL_USDT / float(quote.ask)
                if qty>min(float(quote.bid_quantity),float(quote.ask_quantity)):
                    continue
                data["opened"] += conn.execute(
                    """INSERT INTO rival_family_quote_shadow
                       (venue,contract,quote,asset,horizon_minutes,family,
                        reference_opened_at,issued_at,target_close_at,
                        training_sha256,challenger_up,incumbent_up,
                        challenger_side,incumbent_side,
                        entry_at,entry_bid,entry_ask,quantity,state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (venue,contract,currency,quote.asset,row["horizon_minutes"],
                     row["family"],row["reference_opened_at"],at.isoformat(),
                     target.isoformat(),row["training_sha256"],
                     row["probability_up"],row["incumbent_up"],
                     challenger,incumbent,quote.exchange_at.isoformat(),
                     quote.bid,quote.ask,qty,"OPEN"),
                ).rowcount
        return data

    def expire(self, *, now=None):
        clock=now or datetime.now(UTC)
        if clock.utcoffset() is None:
            raise ValueError("naive quote clock")
        self.initialize()
        with self.store.connect() as conn:
            return conn.execute(
                """UPDATE rival_family_quote_shadow SET state='MISSED_EXIT'
                   WHERE state='OPEN' AND target_close_at<?""",
                ((clock.astimezone(UTC)-EXIT_GRACE).isoformat(),),
            ).rowcount
