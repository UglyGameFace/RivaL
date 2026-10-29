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
DEFAULT_FUNDED_USDT = 10000.0
MIN_FUNDED_USDT = 100.0
MAX_FUNDED_USDT = 100000.0
MAX_DAILY_PRACTICE_LOSS_RATIO = 0.05
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
                   CREATE TABLE IF NOT EXISTS rival_practice_accounts (
                     user_id TEXT PRIMARY KEY,
                     starting_usdt DOUBLE PRECISION NOT NULL,
                     created_at TEXT NOT NULL
                   );
                   """
            )

    @staticmethod
    def _user(user_id: str) -> str:
        if not user_id.isdecimal() or int(user_id) <= 0:
            raise ValueError("Invalid private paper account.")
        return user_id

    @staticmethod
    def _ensure_funding(conn, user_id: str) -> float:
        conn.execute(
            """INSERT INTO rival_practice_accounts
               (user_id,starting_usdt,created_at) VALUES (?,?,?)
               ON CONFLICT(user_id) DO NOTHING""",
            (user_id, DEFAULT_FUNDED_USDT, datetime.now(UTC).isoformat()),
        )
        # Serialize competing opens against the same bankroll row.
        lock = " FOR UPDATE" if conn.backend == "postgres" else ""
        row = conn.execute(
            "SELECT starting_usdt FROM rival_practice_accounts WHERE user_id=?" + lock,
            (user_id,),
        ).fetchone()
        return float(row["starting_usdt"])

    def fund(self, user_id: str, *, starting_usdt: float = DEFAULT_FUNDED_USDT) -> dict:
        """Set fictional funding once before the first trade; never mint real money.

        Existing ledger history is immutable and cannot be erased to reset losses.
        """
        self._user(user_id)
        if not math.isfinite(starting_usdt) or not (
            MIN_FUNDED_USDT <= starting_usdt <= MAX_FUNDED_USDT
        ):
            raise ValueError("Choose a fictional starting balance of 100–100,000 USDT.")
        self.initialize()
        with self.store.connect() as conn:
            existing = conn.execute(
                "SELECT starting_usdt FROM rival_practice_accounts WHERE user_id=?",
                (user_id,),
            ).fetchone()
            prior = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_paper_positions WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if existing is None:
                if int(prior["n"]) > 0:
                    # Existing historical paper trades must count toward the
                    # same default-funded practice account.
                    starting_usdt = DEFAULT_FUNDED_USDT
                conn.execute(
                    """INSERT INTO rival_practice_accounts
                       (user_id,starting_usdt,created_at) VALUES (?,?,?)
                       ON CONFLICT(user_id) DO NOTHING""",
                    (user_id, starting_usdt, datetime.now(UTC).isoformat()),
                )
            elif starting_usdt != float(existing["starting_usdt"]):
                if int(prior["n"]) > 0:
                    raise ValueError(
                        "Your existing practice history is preserved. "
                        "Funding cannot be changed after your first trade."
                    )
                conn.execute(
                    "UPDATE rival_practice_accounts SET starting_usdt=? WHERE user_id=?",
                    (starting_usdt, user_id),
                )
        return self.summary(user_id)

    @staticmethod
    def _balance(conn, user_id: str, initial: float) -> dict:
        row = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN open_slot=1 THEN notional_usdt ELSE 0 END)
                        AS committed,
                      SUM(CASE WHEN closed_at IS NOT NULL
                               THEN assumed_net_usdt ELSE 0 END) AS realized,
                      SUM(CASE WHEN closed_at IS NOT NULL THEN 1 ELSE 0 END) AS closed,
                      SUM(CASE WHEN closed_at IS NOT NULL AND assumed_net_usdt>0
                               THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN closed_at IS NOT NULL AND assumed_net_usdt<0
                               THEN 1 ELSE 0 END) AS losses,
                      SUM(CASE WHEN closed_at IS NOT NULL
                               AND closed_at>=? AND assumed_net_usdt<0
                               THEN assumed_net_usdt ELSE 0 END) AS today_losses
               FROM rival_paper_positions WHERE user_id=?""",
            (datetime.now(UTC).date().isoformat(), user_id),
        ).fetchone()
        realized = float(row["realized"] or 0)
        committed = float(row["committed"] or 0)
        equity = initial + realized
        daily_losses = abs(float(row["today_losses"] or 0))
        return {
            "funded": initial, "realized": realized, "equity": equity,
            "available": equity - committed, "committed": committed,
            "completed": int(row["closed"] or 0), "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "day_loss": daily_losses,
            "daily_loss_cap": initial * MAX_DAILY_PRACTICE_LOSS_RATIO,
        }

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
            funded = self._ensure_funding(conn, user_id)
            bankroll = self._balance(conn, user_id, funded)
            if notional_usdt > bankroll["available"]:
                raise ValueError("Not enough fictional available balance.")
            if bankroll["day_loss"] >= bankroll["daily_loss_cap"]:
                raise ValueError("Daily practice loss limit reached. No new entries.")
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
            funded = self._ensure_funding(conn, user_id)
            balance = self._balance(conn, user_id, funded)
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
            "practice": balance,
            "practice_feedback": {
                "completed": balance["completed"],
                "wins": balance["wins"], "losses": balance["losses"],
                "profitable_fraction": (
                    balance["wins"] / balance["completed"]
                    if balance["completed"] else None
                ),
                "training_eligible": False,
                "reason": (
                    "Practice outcomes can be evaluated against real quotes, "
                    "but are not independent labels or proof of model improvement."
                ),
            },
        }
