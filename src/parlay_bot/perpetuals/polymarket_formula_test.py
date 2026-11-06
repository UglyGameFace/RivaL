"""Locked, pre-registered test of the price formula against Polymarket's price.

Every ~10 s, for each open contract, RivaL computes the formula's chance of UP
from the live Coinbase price (``price_formula``) and compares it with
Polymarket's live share prices. The rule is fixed here, before any results:

* one bet per contract at most: the FIRST moment the formula's chance for a
  side beats that side's ask plus Polymarket's crypto taker fee by EDGE, with
  at least 90 s left (the live reader refuses later quotes);
* the bet is frozen when written (INSERT ... DO NOTHING) and scored only by
  Polymarket's own settlement receipt: profit = payout - ask - fee;
* pings unlock only when, over the last WINDOW_DAYS, at least MIN_BETS bets
  settled AND the average profit is above zero by more than 2 standard errors.

Contracts the formula looked at but never bet on are counted too, so the
share of contracts it trades is visible.
"""
from __future__ import annotations

import math
import threading
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.polymarket_learning import polymarket_fee
from parlay_bot.perpetuals.price_formula import chance_up

EDGE = 0.03
MIN_BETS = 1000
WINDOW_DAYS = 14


class FormulaTest:
    def __init__(self, store) -> None:
        self.store = store
        self._initialized = False
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._bet: set[str] = set()
        # slug -> latest live reading, for the DM card (in memory only).
        self.latest: dict[str, dict] = {}

    def initialize(self) -> None:
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rival_polymarket_formula_seen (
                    slug TEXT PRIMARY KEY, asset TEXT NOT NULL, horizon INTEGER NOT NULL,
                    expires_at TEXT NOT NULL, first_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rival_polymarket_formula_bets (
                    slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
                    asset TEXT NOT NULL, horizon INTEGER NOT NULL,
                    opens_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    issued_at TEXT NOT NULL, side TEXT NOT NULL,
                    chance DOUBLE PRECISION NOT NULL, ask DOUBLE PRECISION NOT NULL,
                    cost DOUBLE PRECISION NOT NULL, price DOUBLE PRECISION NOT NULL,
                    open_price DOUBLE PRECISION NOT NULL,
                    sigma_per_second DOUBLE PRECISION NOT NULL,
                    seconds_left DOUBLE PRECISION NOT NULL, quote_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rival_poly_formula_scope
                    ON rival_polymarket_formula_bets(asset,horizon,issued_at);
            """)
        self._initialized = True

    def consider(self, notice, *, price: float, open_price: float,
                 sigma_per_second: float, now: datetime | None = None) -> dict | None:
        """Evaluate one live quote; freeze the contract's first qualifying bet."""
        stamp = now or datetime.now(UTC)
        left = (notice.expires - stamp).total_seconds()
        if (not notice.issued <= stamp < notice.expires or left < 90
                or not timedelta(0) <= stamp - notice.quote_at <= timedelta(seconds=25)):
            return None
        chance = chance_up(price, open_price, sigma_per_second, left)
        if chance is None:
            return None
        reading = {"slug": notice.slug, "chance_up": chance, "at": stamp,
                   "up_ask": notice.up_ask, "down_ask": notice.down_ask, "bet": None}
        best = None
        for side, side_chance, ask in (("UP", chance, notice.up_ask),
                                       ("DOWN", 1 - chance, notice.down_ask)):
            if not 0 < ask < 1:
                continue
            cost = ask + polymarket_fee(ask)
            if side_chance - cost >= EDGE and (best is None or side_chance - cost > best[3]):
                best = (side, side_chance, ask, side_chance - cost, cost)
        self.initialize()
        with self._lock:
            new_seen = notice.slug not in self._seen
            new_bet = best is not None and notice.slug not in self._bet
            self._seen.add(notice.slug)
            if new_bet:
                self._bet.add(notice.slug)
            if len(self._seen) > 5000:
                self._seen = {notice.slug}
                self._bet = {notice.slug} if best is not None else set()
            self.latest[notice.slug] = reading
            if len(self.latest) > 64:
                self.latest = {k: v for k, v in self.latest.items()
                               if v["at"] > stamp - timedelta(hours=2)}
        if new_seen or new_bet:
            with self.store.connect() as conn:
                if new_seen:
                    conn.execute(
                        "INSERT INTO rival_polymarket_formula_seen "
                        "(slug,asset,horizon,expires_at,first_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(slug) DO NOTHING",
                        (notice.slug, notice.asset, notice.horizon,
                         notice.expires.isoformat(), stamp.isoformat()))
                if new_bet:
                    side, side_chance, ask, _, cost = best
                    conn.execute(
                        "INSERT INTO rival_polymarket_formula_bets "
                        "(slug,condition_id,asset,horizon,opens_at,expires_at,issued_at,side,"
                        "chance,ask,cost,price,open_price,sigma_per_second,seconds_left,quote_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
                        (notice.slug, notice.condition_id, notice.asset, notice.horizon,
                         notice.issued.isoformat(), notice.expires.isoformat(),
                         stamp.isoformat(), side, side_chance, ask, cost, price, open_price,
                         sigma_per_second, left, notice.quote_at.isoformat()))
        if best is not None:
            reading["bet"] = {"side": best[0], "chance": best[1], "cost": best[4]}
        return reading


def formula_record(store, *, asset: str, horizon: int, now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    since = (stamp - timedelta(days=WINDOW_DAYS)).isoformat()
    with store.connect() as conn:
        bets = conn.execute(
            "SELECT b.side,b.cost,o.direction FROM rival_polymarket_formula_bets b "
            "JOIN rival_polymarket_ai_outcomes o ON o.slug=b.slug AND o.condition_id=b.condition_id "
            "WHERE b.asset=? AND b.horizon=? AND b.issued_at>=? AND b.issued_at<b.expires_at "
            "AND o.resolved_at>=b.expires_at AND o.direction IN ('UP','DOWN')",
            (asset, horizon, since),
        ).fetchall()
        seen = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_polymarket_formula_seen "
            "WHERE asset=? AND horizon=? AND first_at>=? AND expires_at<=?",
            (asset, horizon, since, stamp.isoformat()),
        ).fetchone()
    profits = [(1.0 if r["direction"] == r["side"] else 0.0) - float(r["cost"]) for r in bets]
    stakes = [float(r["cost"]) for r in bets]
    n = len(profits)
    mean = sum(profits) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in profits) / (n - 1)) if n > 1 else None
    lower = mean - 2 * sd / math.sqrt(n) if n > 1 else None
    return {
        "contracts": int(seen["n"]) if seen else 0, "bets": n,
        "wins": sum(1 for r in bets if r["direction"] == r["side"]),
        "return_per_dollar": sum(profits) / sum(stakes) if stakes else None,
        "lower_bound_per_share": lower,
        "unlocked": n >= MIN_BETS and lower is not None and lower > 0,
        "window_days": WINDOW_DAYS,
    }


def formula_text(record: dict) -> str:
    state = ("✅ UNLOCKED" if record["unlocked"] else "🔒 LOCKED: no formula pings until it passes")
    n = record["bets"]
    if not n:
        return (f"Price-formula test, last {record['window_days']} days: watched "
                f"{record['contracts']:,} settled contracts, no settled bets yet. {state}.")
    return (
        f"Price-formula test, last {record['window_days']} days: {n:,} bets on "
        f"{record['contracts']:,} contracts, won {record['wins']:,} ({record['wins'] / n:.0%}), "
        f"return **{record['return_per_dollar']:+.1%}** per $1 after fees. Needs "
        f"{MIN_BETS:,}+ bets and a profit too big to be luck. {state}."
    )
