"""Track the NEXT frozen Polymarket Up/Down calls, the way a bettor would.

A session starts at a persisted UTC time. Only contracts whose first model
call was issued after that time count, in issue order, and the first N are
fixed: wins are never selected after the fact. Each call is graded against
Polymarket's own settlement receipt. "What buying would have paid" uses the
share price RivaL recorded when it first saw the contract, plus Polymarket's
crypto taker fee, and only when that price was recorded within
PRICE_WINDOW of the call. It is a paper result, not a real fill.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.embed_text import fit_embed_field_value
from parlay_bot.perpetuals.forward_tracking import GOALS, ForwardTracking, TrackingUnavailable
from parlay_bot.perpetuals.polymarket_learning import (
    MIN_DIRECTIONAL_MARGIN,
    MODEL,
    polymarket_fee,
)

PLATFORM = "polymarket"
PRICE_WINDOW = timedelta(seconds=30)


def _clock(now: datetime | None) -> datetime:
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("tracking requires a timezone-aware clock")
    return stamp.astimezone(UTC)


def _date(value) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("naive timestamp")
    return stamp.astimezone(UTC)


class PolymarketTracking:
    """Sessions share the forward-tracking table with venue='polymarket'."""

    def __init__(self, store):
        self.store = store

    def initialize(self):
        ForwardTracking(self.store).initialize()

    @staticmethod
    def _batch(conn, session):
        return conn.execute(
            """SELECT f.slug,f.issued_at,f.probability_up,f.market_baseline,
                      v.observed_at,v.features_json,v.expires_at,
                      o.direction AS outcome,o.resolved_at
               FROM rival_polymarket_model_forward f
               JOIN rival_polymarket_observations v ON v.slug=f.slug
               LEFT JOIN rival_polymarket_ai_outcomes o
                 ON o.slug=f.slug AND o.condition_id=v.condition_id
               WHERE v.asset=? AND v.horizon=? AND f.model_version=?
                 AND f.issued_at>?
               ORDER BY f.issued_at,f.slug
               LIMIT ?""",
            (session["asset"], session["horizon_minutes"], session["model_version"],
             session["started_at"], session["target_count"]),
        ).fetchall()

    def _current(self, conn, *, owner_id, asset, horizon):
        return conn.execute(
            """SELECT * FROM rival_forward_tracking_sessions
               WHERE owner_id=? AND asset=? AND venue=? AND horizon_minutes=?
               ORDER BY started_at DESC,session_id DESC LIMIT 1""",
            (str(owner_id), asset, PLATFORM, horizon),
        ).fetchone()

    def start(self, *, owner_id: int, asset: str, horizon: int, target: int,
              now: datetime | None = None) -> dict:
        if asset not in ("BTC", "ETH", "SOL", "XRP") or horizon not in (5, 15, 60):
            raise ValueError("unsupported tracking source")
        if target not in GOALS:
            raise ValueError("unsupported future sample goal")
        stamp = _clock(now)
        self.initialize()
        with self.store.connect() as conn:
            existing = self._current(conn, owner_id=owner_id, asset=asset, horizon=horizon)
            if existing is not None:
                current = self._evaluate(existing, self._batch(conn, existing), stamp)
                if not current["complete"]:
                    return {"status": "already_tracking",
                            "session_id": existing["session_id"],
                            "target": int(existing["target_count"])}
            try:
                live = conn.execute(
                    "SELECT 1 FROM rival_polymarket_model_forward f "
                    "JOIN rival_polymarket_observations v ON v.slug=f.slug "
                    "WHERE v.asset=? AND v.horizon=? AND f.model_version=? LIMIT 1",
                    (asset, horizon, MODEL),
                ).fetchone()
            except Exception as exc:
                raise TrackingUnavailable(
                    "RivaL has not issued any Polymarket calls for this coin and timeframe yet"
                ) from exc
            if live is None:
                raise TrackingUnavailable(
                    "RivaL has not issued any Polymarket calls for this coin and timeframe yet"
                )
            session_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO rival_forward_tracking_sessions
                   (session_id,owner_id,asset,venue,contract,quote,horizon_minutes,
                    model_version,started_at,target_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session_id, str(owner_id), asset, PLATFORM, "up-or-down", "USDC",
                 horizon, MODEL, stamp.isoformat(), target),
            )
        return {"status": "started", "session_id": session_id, "target": target}

    def report(self, *, owner_id: int, asset: str, now: datetime | None = None) -> dict:
        stamp = _clock(now)
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_forward_tracking_sessions
                   WHERE owner_id=? AND asset=? AND venue=?
                   ORDER BY started_at DESC LIMIT 30""",
                (str(owner_id), asset, PLATFORM),
            ).fetchall()
            by_horizon = {}
            for session in rows:
                horizon = int(session["horizon_minutes"])
                if horizon not in by_horizon:
                    by_horizon[horizon] = self._evaluate(
                        session, self._batch(conn, session), stamp)
        return {"status": "tracking" if by_horizon else "not_started",
                "platform": PLATFORM, "asset": asset,
                "cohorts": [by_horizon[key] for key in sorted(by_horizon)]}

    @staticmethod
    def _evaluate(session, batch, now: datetime) -> dict:
        target = int(session["target_count"])
        started = _date(session["started_at"])
        calls = []
        invalid = 0
        for row in batch:
            try:
                issued = _date(row["issued_at"])
                expires = _date(row["expires_at"])
                p = float(row["probability_up"])
                m = float(row["market_baseline"])
                if not (started < issued < expires and math.isfinite(p) and 0 <= p <= 1
                        and math.isfinite(m) and 0 <= m <= 1):
                    raise ValueError("invalid call")
                side = ("WAIT" if abs(p - .5) < MIN_DIRECTIONAL_MARGIN
                        else "UP" if p > .5 else "DOWN")
                outcome = row["outcome"]
                settled = (outcome in ("UP", "DOWN") and row["resolved_at"] is not None
                           and _date(row["resolved_at"]) >= expires and expires <= now)
                price = None
                observed = _date(row["observed_at"])
                if side != "WAIT" and abs(issued - observed) <= PRICE_WINDOW:
                    features = json.loads(row["features_json"])
                    ask = float(features[0] if side == "UP" else features[1])
                    if math.isfinite(ask) and 0 < ask < 1:
                        price = ask
                calls.append({
                    "issued_at": issued, "expires": expires, "side": side,
                    "probability_up": p, "market": m,
                    "outcome": outcome if settled else None, "price": price,
                })
            except (ValueError, TypeError, KeyError, IndexError, OverflowError):
                invalid += 1
        graded = [c for c in calls if c["outcome"] is not None]
        picked = [c for c in graded if c["side"] != "WAIT"]
        wins = sum(c["side"] == c["outcome"] for c in picked)
        priced = [c for c in picked if c["price"] is not None]
        stake = sum(c["price"] + polymarket_fee(c["price"]) for c in priced)
        paid = sum(1.0 for c in priced if c["side"] == c["outcome"])
        ys = [int(c["outcome"] == "UP") for c in graded]
        model_brier = (sum((c["probability_up"] - y) ** 2 for c, y in zip(graded, ys, strict=True))
                       / len(graded) if graded else None)
        market_brier = (sum((c["market"] - y) ** 2 for c, y in zip(graded, ys, strict=True))
                        / len(graded) if graded else None)
        return {
            "session_id": str(session["session_id"]),
            "horizon": int(session["horizon_minutes"]), "target": target,
            "started_at": started.isoformat(),
            "issued": len(calls), "settled": len(graded),
            "pending": len(calls) - len(graded), "invalid": invalid,
            "picked": len(picked), "waits": sum(c["side"] == "WAIT" for c in graded),
            "wins": wins, "priced": len(priced),
            "return_per_dollar": (paid - stake) / stake if stake else None,
            "model_brier": model_brier, "market_brier": market_brier,
            "recent": calls[-6:],
            "complete": len(calls) == target and len(graded) == target and not invalid,
            "real_profit_verified": False,
        }


def polymarket_tracking_text(result: dict) -> str:
    if not result["cohorts"]:
        return ("**Platform: Polymarket (Up/Down contracts).** No tracking session yet. "
                "Use **Start tracking the NEXT predictions** below to follow RivaL's next "
                "frozen calls on this coin. Only calls made after you start count.")
    lines = ["**Platform: Polymarket (Up/Down contracts)**"]
    for item in result["cohorts"]:
        line = (f"**{item['horizon']}m** · {item['issued']}/{item['target']} calls issued, "
                f"{item['settled']} settled")
        if item["settled"]:
            # Audit Step 1: no rate on fewer than 20 results.
            if item["picked"] >= 20:
                picks = f" ({item['wins'] / item['picked']:.0%})"
            elif item["picked"]:
                picks = f" (too few to judge — {item['picked']} picked)"
            else:
                picks = ""
            line += (f"\nPicked a side {item['picked']} times, right **{item['wins']}**"
                     + picks
                     + f"; WAIT (no pick) {item['waits']} times.")
            if item["return_per_dollar"] is not None:
                if item["priced"] >= 20:
                    line += (f"\nBuying each pick at the price shown, after Polymarket fees: "
                             f"**{item['return_per_dollar']:+.1%}** per $1 "
                             f"({item['priced']} picks with a recorded price).")
                else:
                    line += ("\nBuying each pick at the price shown, after Polymarket fees: "
                             f"too few to judge — {item['priced']} picks with a recorded price.")
            line += (f"\nError {item['model_brier']:.4f} vs the market price's own "
                     f"{item['market_brier']:.4f} (lower is better).")
        for call in reversed(item["recent"][-3:]):
            mark = ("⏳" if call["outcome"] is None else
                    "➖" if call["side"] == "WAIT" else
                    "✅" if call["side"] == call["outcome"] else "❌")
            price = f" at ${call['price']:.2f}" if call["price"] is not None else ""
            line += (f"\n{mark} <t:{int(call['issued_at'].timestamp())}:t> "
                     f"{call['side']}{price} · {call['probability_up']:.0%} UP")
        lines.append(line)
    return fit_embed_field_value(
        "\n".join(lines) + "\nPaper results from frozen calls; no real trades."
    )
