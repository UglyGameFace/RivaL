"""Per-user, QUOTE-BASED paper ledger. Absolutely no order placement.

RivaL does not have authenticated fills. These are hypothetical bid/ask
snapshots with explicit assumed fees and slippage, never actual P&L.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from parlay_bot.perpetuals.live_quotes import BookQuote
from parlay_bot.storage.hot import RelationalHotStore

MAX_PAPER_USDT = 250.0
MAX_OPEN_PER_USER = 3
TAKER_BPS = 4.0
SLIPPAGE_BPS_PER_SIDE = 2.0


@dataclass(frozen=True)
class PaperClose:
    asset: str
    side: str
    quantity: float
    entry: float
    exit: float
    gross_usdt: float
    assumed_cost_usdt: float
    assumed_net_usdt: float
    opened_at: datetime
    closed_at: datetime


class PaperLedger:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_paper_positions (
                     position_id TEXT PRIMARY KEY,
                     user_id TEXT NOT NULL,
                     asset TEXT NOT NULL,
                     venue TEXT NOT NULL,
                     symbol TEXT NOT NULL,
                     side TEXT NOT NULL,
                     notional_usdt DOUBLE PRECISION NOT NULL,
                     quantity DOUBLE PRECISION NOT NULL,
                     entry DOUBLE PRECISION NOT NULL,
                     opened_at TEXT NOT NULL,
                     open_slot INTEGER,
                     exit DOUBLE PRECISION,
                     closed_at TEXT,
                     gross_usdt DOUBLE PRECISION,
                     assumed_cost_usdt DOUBLE PRECISION,
                     assumed_net_usdt DOUBLE PRECISION
                   );
                   CREATE UNIQUE INDEX IF NOT EXISTS idx_rival_paper_user_asset_open
                   ON rival_paper_positions (user_id,asset,open_slot);
                   CREATE INDEX IF NOT EXISTS idx_rival_paper_user_history
                   ON rival_paper_positions (user_id,opened_at);
                   """
            )

    @staticmethod
    def _user(user_id: str) -> str:
        if not user_id.isdecimal() or int(user_id) <= 0:
            raise ValueError("Invalid private paper account.")
        return user_id

    def open(
        self, *, user_id: str, position_id: str, quote: BookQuote,
        side: str, notional_usdt: float,
    ) -> str:
        self._user(user_id)
        if side not in ("long", "short"):
            raise ValueError("Choose long or short (paper only).")
        if not isinstance(position_id, str) or not position_id.isdecimal():
            raise ValueError("Invalid idempotency ID.")
        if (not math.isfinite(notional_usdt)
            or not 0 < notional_usdt <= MAX_PAPER_USDT):
            raise ValueError("Paper position must be between 0 and 250 USDT.")
        quote.validate(now=datetime.now(UTC))
        entry = quote.ask if side == "long" else quote.bid
        qty = notional_usdt / entry
        if qty > min(quote.ask_quantity, quote.bid_quantity):
            raise ValueError("Top-of-book quantity insufficient. No paper position created.")
        self.initialize()
        with self.store.connect() as conn:
            existing = conn.execute(
                """SELECT user_id FROM rival_paper_positions
                   WHERE position_id=?""",
                (position_id,),
            ).fetchone()
            if existing:
                if str(existing["user_id"]) != user_id:
                    raise ValueError("Paper position ID belongs to another user.")
                return "Already recorded (idempotent)."
            count = conn.execute(
                """SELECT COUNT(*) AS n FROM rival_paper_positions
                   WHERE user_id=? AND open_slot=1""",
                (user_id,),
            ).fetchone()
            if int(count["n"]) >= MAX_OPEN_PER_USER:
                raise ValueError("Maximum three simultaneous paper positions.")
            duplicate = conn.execute(
                """SELECT 1 FROM rival_paper_positions
                   WHERE user_id=? AND asset=? AND open_slot=1""",
                (user_id, quote.asset),
            ).fetchone()
            if duplicate:
                raise ValueError("Close the existing paper watch on this asset first.")
            conn.execute(
                """INSERT INTO rival_paper_positions
                   (position_id,user_id,asset,venue,symbol,side,
                    notional_usdt,quantity,entry,opened_at,open_slot)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    position_id, user_id, quote.asset, quote.venue,
                    quote.symbol, side, notional_usdt, qty,
                    entry, quote.exchange_at.astimezone(UTC).isoformat(),
                ),
            )
        return (
            f"Paper {side.upper()} opened • {quote.asset} "
            f"at indicative {entry:,.4f} USDT. No exchange order."
        )

    def close(self, *, user_id: str, asset: str, quote: BookQuote) -> PaperClose:
        self._user(user_id)
        quote.validate(now=datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM rival_paper_positions
                   WHERE user_id=? AND asset=? AND open_slot=1""",
                (user_id, asset),
            ).fetchone()
            if row is None:
                raise ValueError("No open paper position for this coin.")
            if quote.asset != row["asset"] or quote.venue != row["venue"] or (
                quote.symbol != row["symbol"]
            ):
                raise ValueError("Cannot close using another venue or instrument.")
            started = datetime.fromisoformat(str(row["opened_at"]))
            if quote.exchange_at <= started:
                raise ValueError("Need a newer verified quote to close paper.")
            side = str(row["side"])
            quantity = float(row["quantity"])
            if quantity > min(quote.bid_quantity, quote.ask_quantity):
                raise ValueError("Insufficient exit top-of-book liquidity.")
            entry = float(row["entry"])
            exit_price = quote.bid if side == "long" else quote.ask
            signed = 1 if side == "long" else -1
            gross = quantity * signed * (exit_price - entry)
            assumed_cost = quantity * (entry + exit_price) * (
                TAKER_BPS + SLIPPAGE_BPS_PER_SIDE
            ) / 10000
            net = gross - assumed_cost
            result = conn.execute(
                """UPDATE rival_paper_positions
                   SET open_slot=NULL, exit=?, closed_at=?,
                       gross_usdt=?,assumed_cost_usdt=?,assumed_net_usdt=?
                   WHERE position_id=? AND user_id=? AND open_slot=1""",
                (
                    exit_price, quote.exchange_at.astimezone(UTC).isoformat(),
                    gross, assumed_cost, net, row["position_id"], user_id,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Paper position was concurrently settled.")
        return PaperClose(
            asset, side, quantity, entry, exit_price,
            gross, assumed_cost, net, started, quote.exchange_at,
        )

    def summary(self, user_id: str) -> dict:
        self._user(user_id)
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT
                     SUM(CASE WHEN open_slot=1 THEN 1 ELSE 0 END) AS open_count,
                     SUM(CASE WHEN closed_at IS NOT NULL THEN 1 ELSE 0 END)
                       AS completed,
                     SUM(assumed_net_usdt) AS assumed_net_usdt
                   FROM rival_paper_positions WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        return {
            "open": int(row["open_count"] or 0),
            "closed": int(row["completed"] or 0),
            "assumed_net_usdt": float(row["assumed_net_usdt"] or 0),
            "actual_profit_verified": False,
            "account_receipts_present": False,
        }
