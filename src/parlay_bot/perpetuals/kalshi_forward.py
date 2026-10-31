"""Kalshi-only immutable forward model evidence.

An official public Kalshi market quote is never a Polymarket probability.
Training uses only previously verified *settled* Kalshi contract outcomes.
No auto-trading or model promotion is permitted by this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import UTC, datetime, timedelta

MODEL = "kalshi-online-logit-shadow-v1"
MIN_TRAINED = 100
MIN_PROSPECTIVE = 150
MIN_DAYS = 21


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Kalshi timestamps need a timezone")
    return value.astimezone(UTC).isoformat()


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _market_features(market, observed_at: datetime) -> tuple[float, tuple[float, ...]]:
    if (
        market.yes_ask is None or market.no_ask is None
        or not all(
            math.isfinite(x) and 0 < x < 1
            for x in (market.yes_ask, market.no_ask)
        )
    ):
        raise ValueError("Kalshi market needs BOTH real ask quotes")
    to_close = (market.close_time - observed_at).total_seconds()
    if not 0 < to_close <= 60 * 60 * 24 * 90:
        raise ValueError("Kalshi market close must be in the future")
    implied_yes = market.yes_ask / (market.yes_ask + market.no_ask)
    # Exact quote probabilities are a benchmark, not a trained predictor.
    return implied_yes, (
        implied_yes - .5,
        max(-.5, min(.5, market.yes_ask + market.no_ask - 1)),
        min(1., to_close / (24 * 3600)),
    )


class KalshiForwardResearch:
    """Persist one first quote and one genuine settled result per contract."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.store.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS rival_kalshi_forward (
                        ticker TEXT PRIMARY KEY,
                        asset TEXT NOT NULL,
                        series_ticker TEXT NOT NULL,
                        close_at TEXT NOT NULL,
                        issued_at TEXT NOT NULL,
                        first_quote_sha256 TEXT NOT NULL,
                        market_rules_sha256 TEXT NOT NULL,
                        features_json TEXT NOT NULL,
                        baseline_yes DOUBLE PRECISION NOT NULL,
                        yes_ask DOUBLE PRECISION NOT NULL,
                        no_ask DOUBLE PRECISION NOT NULL,
                        candidate_yes DOUBLE PRECISION,
                        candidate_weights_sha256 TEXT,
                        candidate_training_labels INTEGER,
                        result TEXT,
                        settlement_at TEXT,
                        verified_at TEXT,
                        settlement_evidence_sha256 TEXT,
                        learned_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_kalshi_pending
                        ON rival_kalshi_forward (result, close_at);
                    CREATE TABLE IF NOT EXISTS rival_kalshi_shadow_weights (
                        asset TEXT NOT NULL,
                        series_ticker TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        weights_json TEXT NOT NULL,
                        weights_sha256 TEXT NOT NULL,
                        updates INTEGER NOT NULL,
                        trained_at TEXT NOT NULL,
                        PRIMARY KEY(asset,series_ticker)
                    );
                """)
            self._ready = True

    def observe(self, *, asset: str, market, at: datetime) -> bool:
        """First quote frozen forever. No invalid or incomplete ask becomes data."""
        if asset not in ("BTC", "ETH", "SOL", "XRP"):
            raise ValueError("unrecognized Kalshi asset")
        series = getattr(market, "series_ticker", None)
        if (
            re.fullmatch(r"[A-Z0-9-]{2,110}", market.ticker) is None
            or not isinstance(series, str)
            or re.fullmatch(r"[A-Z0-9-]{2,80}", series) is None
            or not market.ticker.startswith(series + "-")
        ):
            raise ValueError("unverified Kalshi market/series identity")
        if not market.rules or market.rules == "Rules not verified":
            return False
        stamp = _iso(at)
        _iso(market.close_time)
        baseline, features = _market_features(market, at)
        payload = json.dumps({
            "ticker": market.ticker, "asset": asset, "issued_at": stamp,
            "closes": _iso(market.close_time), "yes_ask": market.yes_ask,
            "no_ask": market.no_ask, "rules": market.rules,
        }, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        rules_sha = hashlib.sha256(market.rules.encode()).hexdigest()
        self.initialize()
        with self._lock, self.store.connect() as conn:
            zero = json.dumps([0., 0., 0., 0.])
            conn.execute(
                """INSERT INTO rival_kalshi_shadow_weights
                   (asset,series_ticker,model_version,weights_json,
                    weights_sha256,updates,trained_at)
                   VALUES (?,?,?,?,?,0,?) ON CONFLICT DO NOTHING""",
                (asset, series, MODEL, zero,
                 hashlib.sha256(zero.encode()).hexdigest(), stamp),
            )
            state = conn.execute(
                "SELECT * FROM rival_kalshi_shadow_weights "
                "WHERE asset=? AND series_ticker=?",
                (asset,series),
            ).fetchone()
            weights = json.loads(state["weights_json"])
            if (
                len(weights) != 4 or not all(math.isfinite(v) for v in weights)
                or hashlib.sha256(state["weights_json"].encode()).hexdigest()
                   != state["weights_sha256"]
            ):
                raise ValueError("Kalshi challenger state corrupt")
            trained = int(state["updates"])
            candidate = (
                _sigmoid(sum(w * f for w, f in zip(weights, (1., *features))))
                if trained >= MIN_TRAINED else None
            )
            count = conn.execute(
                """INSERT INTO rival_kalshi_forward
                   (ticker,asset,series_ticker,close_at,issued_at,first_quote_sha256,
                    market_rules_sha256,features_json,baseline_yes,
                    yes_ask,no_ask,candidate_yes,candidate_weights_sha256,
                    candidate_training_labels)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO NOTHING""",
                (market.ticker, asset, series, _iso(market.close_time),
                 stamp, digest,
                 rules_sha, json.dumps(features), baseline,
                 market.yes_ask, market.no_ask, candidate,
                 state["weights_sha256"] if candidate is not None else None,
                 trained if candidate is not None else None),
            )
            return count.rowcount == 1

    def pending(self, *, now: datetime, limit: int = 2) -> tuple[str, ...]:
        if not 1 <= limit <= 5:
            raise ValueError("Kalshi settlement checks must stay bounded")
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT ticker FROM rival_kalshi_forward
                   WHERE result IS NULL AND close_at<=?
                   ORDER BY close_at LIMIT ?""",
                (_iso(now), limit),
            ).fetchall()
        return tuple(row["ticker"] for row in rows)

    def settle(self, *, ticker: str, payload: object, checked_at: datetime) -> bool:
        """Only an actual official settled YES/NO receipt becomes a label."""
        now = _iso(checked_at)
        if not isinstance(payload, dict) or not isinstance(payload.get("market"), dict):
            raise ValueError("missing official Kalshi market payload")
        market = payload["market"]
        if market.get("ticker") != ticker or market.get("status") != "settled":
            return False
        result = market.get("result")
        if result not in ("yes", "no"):
            return False  # no void/pending/ambiguous winner substitutions
        settlement_at = market.get("settlement_ts")
        if not isinstance(settlement_at, str):
            return False
        try:
            settled = datetime.fromisoformat(settlement_at.replace("Z", "+00:00"))
            settled_at = _iso(settled)
        except ValueError:
            return False
        if settled_at > now:
            return False
        self.initialize()
        with self._lock, self.store.connect() as conn:
            original = conn.execute(
                "SELECT * FROM rival_kalshi_forward WHERE ticker=?",
                (ticker,),
            ).fetchone()
            if original is None:
                return False
            if original["result"] is not None:
                if original["result"] != result:
                    raise ValueError("Kalshi settled result changed")
                return False
            # No future data leakage or forced early resolution.
            if settled_at < original["close_at"] or settled_at <= original["issued_at"]:
                return False
            receipt = json.dumps({
                "ticker": ticker, "status": market["status"],
                "result": result, "settlement_ts": settlement_at,
            }, sort_keys=True, separators=(",", ":"))
            receipt_hash = hashlib.sha256(receipt.encode()).hexdigest()
            conn.execute(
                """UPDATE rival_kalshi_forward
                   SET result=?,settlement_at=?,verified_at=?,
                       settlement_evidence_sha256=?,learned_at=?
                   WHERE ticker=? AND result IS NULL""",
                (result, settled_at, now, receipt_hash, now, ticker),
            )
            asset = original["asset"]
            series = original["series_ticker"]
            suffix = " FOR UPDATE" if self.store.database.backend == "postgres" else ""
            state = conn.execute(
                "SELECT * FROM rival_kalshi_shadow_weights "
                "WHERE asset=? AND series_ticker=?" + suffix,
                (asset,series),
            ).fetchone()
            weights = json.loads(state["weights_json"])
            features = json.loads(original["features_json"])
            label = int(result == "yes")
            error = label - _sigmoid(
                sum(w * f for w, f in zip(weights, (1., *features)))
            )
            for i, feature in enumerate((1., *features)):
                weights[i] += .02 * (
                    error * feature - (.002 * weights[i] if i else 0)
                )
            encoded = json.dumps(weights)
            conn.execute(
                """UPDATE rival_kalshi_shadow_weights SET
                   weights_json=?,weights_sha256=?,updates=updates+1,trained_at=?
                   WHERE asset=? AND series_ticker=?""",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                 now, asset, series),
            )
            return True

    def summary(self, *, asset: str) -> dict:
        if asset not in ("BTC", "ETH", "SOL", "XRP"):
            raise ValueError("unrecognized Kalshi asset")
        self.initialize()
        with self.store.connect() as conn:
            counts = conn.execute(
                """SELECT COUNT(*) AS observed,
                          SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) AS settled
                   FROM rival_kalshi_forward WHERE asset=?""", (asset,),
            ).fetchone()
            rows = conn.execute(
                """SELECT candidate_yes,baseline_yes,result,issued_at,yes_ask,no_ask
                   FROM rival_kalshi_forward WHERE asset=? AND result IS NOT NULL
                     AND candidate_yes IS NOT NULL AND settlement_at>issued_at
                   ORDER BY issued_at""", (asset,),
            ).fetchall()
            states = conn.execute(
                "SELECT updates FROM rival_kalshi_shadow_weights WHERE asset=?",
                (asset,),
            ).fetchall()
        n = len(rows)
        cand = (
            sum((r["candidate_yes"] - int(r["result"] == "yes")) ** 2 for r in rows) / n
            if n else None
        )
        baseline = (
            sum((r["baseline_yes"] - int(r["result"] == "yes")) ** 2 for r in rows) / n
            if n else None
        )
        days = len({r["issued_at"][:10] for r in rows})
        # Do not promote based merely on retrospective training, or a
        # favorable few bets. Kalshi after-fee execution isn't proven.
        evidence_qualified = bool(
            n >= MIN_PROSPECTIVE and days >= MIN_DAYS
            and cand is not None and baseline is not None
            and cand + .005 < baseline
        )
        return {
            "provider": "kalshi", "model": MODEL, "asset": asset,
            "observed": int(counts["observed"]),
            "settled": int(counts["settled"] or 0),
            "trained_labels": sum(int(state["updates"]) for state in states),
            "candidate_forward_resolved": n, "candidate_forward_days": days,
            "candidate_brier": cand, "kalshi_quote_baseline_brier": baseline,
            "candidate_research_evidence_qualified": evidence_qualified,
            "approved_model": False, "verified_betting_advantage": False,
        }
