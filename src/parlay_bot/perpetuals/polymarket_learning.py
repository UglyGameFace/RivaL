"""Book-only Polymarket learning from exact terminal payout receipts.

Exchange candles, AI confidence and retrospective oracle guesses never become
labels. Research scores are uncalibrated until measured on frozen live forecasts.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.learning import _sigmoid
from parlay_bot.perpetuals.model_checkpoints import canonical

MODEL = "polymarket-terminal-book-logistic-v1"
MIN_LABELS = 100


def book_features(notice):
    up, down = notice.up_ask, notice.down_ask
    fraction = (notice.expires-notice.quote_at).total_seconds()/(notice.horizon*60)
    depth_up = notice.up_average_ask if notice.up_average_ask is not None else up
    depth_down = notice.down_average_ask if notice.down_average_ask is not None else down
    values = (up, down, up/(up+down), up+down-1,
              depth_up-up, depth_down-down, max(0., min(1., fraction)),
              float(notice.price_to_beat is not None), up-down, depth_up-depth_down)
    if any(not math.isfinite(v) or abs(v) > 5 for v in values):
        raise ValueError("invalid contract book features")
    return values


class PolymarketOutcomeLearner:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self.store.connect() as conn:
                conn.executescript("""CREATE TABLE IF NOT EXISTS rival_polymarket_observations (
                    slug TEXT PRIMARY KEY,condition_id TEXT NOT NULL,asset TEXT NOT NULL,
                    horizon INTEGER NOT NULL,resolution_source TEXT NOT NULL,
                    expires_at TEXT NOT NULL,observed_at TEXT NOT NULL,
                    features_json TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    learned_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_rival_poly_observed_expiry
                    ON rival_polymarket_observations(expires_at);
                CREATE INDEX IF NOT EXISTS idx_rival_poly_learning_scope
                    ON rival_polymarket_observations(asset,horizon,resolution_source,learned_at);
                CREATE TABLE IF NOT EXISTS rival_polymarket_model_state (
                    asset TEXT NOT NULL,horizon INTEGER NOT NULL,resolution_source TEXT NOT NULL,
                    model_version TEXT NOT NULL,weights_json TEXT NOT NULL,weights_sha256 TEXT NOT NULL,
                    updates INTEGER NOT NULL,trained_at TEXT NOT NULL,
                    PRIMARY KEY(asset,horizon,resolution_source,model_version)
                );
                CREATE TABLE IF NOT EXISTS rival_polymarket_model_forward (
                    slug TEXT NOT NULL,model_version TEXT NOT NULL,issued_at TEXT NOT NULL,
                    weights_sha256 TEXT NOT NULL,probability_up DOUBLE PRECISION NOT NULL,market_baseline DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY(slug,model_version)
                );""")
            self._initialized = True

    def observe(self, notice):
        """Persist one original observation, update state, issue fresh research.

        Every live quote can be scored. Only the first pre-resolution forecast
        per contract counts as forward evidence, regardless of quote frequency.
        """
        self.initialize()
        now = datetime.now(UTC)
        if not (notice.issued <= notice.quote_at <= now < notice.expires and
                now-notice.quote_at <= timedelta(seconds=25)):
            return None
        features = book_features(notice)
        encoded = canonical(features)
        raw_evidence = canonical({
            "slug": notice.slug, "condition_id": notice.condition_id,
            "up_token_id": notice.up_token_id, "down_token_id": notice.down_token_id,
            "source_quote_at": notice.quote_at.isoformat(),
            "resolution_source": notice.resolution_source, "rules": notice.rules,
            "opens_at": notice.issued.isoformat(), "expires_at": notice.expires.isoformat(),
            "features": features,
        })
        evidence = hashlib.sha256(raw_evidence.encode()).hexdigest()
        scope = (notice.asset, notice.horizon, notice.resolution_source, MODEL)
        with self._lock, self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_polymarket_observations "
                "(slug,condition_id,asset,horizon,resolution_source,expires_at,observed_at,features_json,evidence_sha256,evidence_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
                (notice.slug, notice.condition_id, notice.asset, notice.horizon, notice.resolution_source,
                 notice.expires.isoformat(), now.isoformat(), encoded, evidence, raw_evidence),
            )
            zeros = canonical([0.] * 11)
            conn.execute(
                "INSERT INTO rival_polymarket_model_state "
                "(asset,horizon,resolution_source,model_version,weights_json,weights_sha256,updates,trained_at) "
                "VALUES (?,?,?,?,?,?,0,?) ON CONFLICT DO NOTHING",
                (*scope, zeros, hashlib.sha256(zeros.encode()).hexdigest(), now.isoformat()),
            )
            lock = " FOR UPDATE" if self.store.database.backend == "postgres" else ""
            state = conn.execute(
                "SELECT * FROM rival_polymarket_model_state WHERE asset=? AND horizon=? "
                "AND resolution_source=? AND model_version=?"+lock, scope,
            ).fetchone()
            weights = json.loads(state["weights_json"])
            if (len(weights) != 11 or any(not math.isfinite(w) for w in weights)
                or hashlib.sha256(canonical(weights).encode()).hexdigest() != state["weights_sha256"]):
                raise ValueError("corrupt Polymarket learned state")
            rows = conn.execute(
                "SELECT v.slug,v.features_json,o.direction FROM rival_polymarket_observations v "
                "JOIN rival_polymarket_ai_outcomes o ON o.slug=v.slug AND o.condition_id=v.condition_id "
                "WHERE v.asset=? AND v.horizon=? AND v.resolution_source=? AND v.learned_at IS NULL "
                "AND v.observed_at<v.expires_at AND o.resolved_at>=v.expires_at AND o.observed_at<=? "
                "ORDER BY o.observed_at,v.slug LIMIT 200",
                (*scope[:3], now.isoformat()),
            ).fetchall()
            updates = int(state["updates"])
            for row in rows:
                x = (1., *json.loads(row["features_json"]))
                y = int(row["direction"] == "UP")
                error = y-_sigmoid(sum(w*v for w, v in zip(weights, x)))
                for i, value in enumerate(x):
                    weights[i] += .025 * (error*value-(.001*weights[i] if i else 0))
                conn.execute("UPDATE rival_polymarket_observations SET learned_at=? WHERE slug=? AND learned_at IS NULL",
                             (now.isoformat(), row["slug"]))
                updates += 1
            weights_json = canonical(weights)
            weight_sha = hashlib.sha256(weights_json.encode()).hexdigest()
            if rows:
                conn.execute(
                    "UPDATE rival_polymarket_model_state SET weights_json=?,weights_sha256=?,updates=?,trained_at=? "
                    "WHERE asset=? AND horizon=? AND resolution_source=? AND model_version=?",
                    (weights_json, weight_sha, updates, now.isoformat(), *scope),
                )
            if updates < MIN_LABELS:
                return None
            issued = datetime.now(UTC)
            if issued >= notice.expires or issued-notice.quote_at > timedelta(seconds=25):
                return None
            p = _sigmoid(sum(w*x for w, x in zip(weights, (1., *features))))
            conn.execute(
                "INSERT INTO rival_polymarket_model_forward "
                "(slug,model_version,issued_at,weights_sha256,probability_up,market_baseline) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (notice.slug, MODEL, issued.isoformat(), weight_sha, p, notice.up_ask/(notice.up_ask+notice.down_ask)),
            )
        return {"direction": "UP" if p > .5 else "DOWN" if p < .5 else "WAIT",
                "issued_at": issued, "model": MODEL, "evidence_sha256": evidence,
                "training_labels": updates, "weights_sha256": weight_sha}

    def summary(self, *, asset=None, horizon=None):
        """Recent *issued* model calls; lifetime training is a separate count.

        A contract resolved this week is not a seven-day prediction if the
        forecast was issued months ago. Invalid or incomplete receipts must
        never be counted as correct predictions or zero Brier error.
        """
        self.initialize()
        clock = datetime.now(UTC)
        since = (clock - timedelta(days=7)).isoformat()
        where, params = (
            (" WHERE asset=? AND horizon=?", (asset, horizon))
            if asset is not None else ("", ())
        )
        with self.store.connect() as conn:
            counts = conn.execute(
                "SELECT COUNT(*) AS observed, "
                "SUM(CASE WHEN learned_at IS NOT NULL THEN 1 ELSE 0 END) AS learned "
                "FROM rival_polymarket_observations" + where, params,
            ).fetchone()
            rows = conn.execute(
                "SELECT f.probability_up,f.market_baseline,o.direction "
                "FROM rival_polymarket_model_forward f "
                "JOIN rival_polymarket_observations v ON v.slug=f.slug "
                "JOIN rival_polymarket_ai_outcomes o "
                "ON o.slug=f.slug AND o.condition_id=v.condition_id "
                "WHERE f.model_version=? AND f.issued_at>=? AND f.issued_at<=? "
                "AND f.issued_at<v.expires_at AND f.issued_at<o.resolved_at "
                "AND o.resolved_at>=v.expires_at "
                "AND o.observed_at>=v.expires_at AND o.observed_at<=? "
                "AND o.direction IN ('UP','DOWN') "
                "AND f.probability_up BETWEEN 0 AND 1 "
                "AND f.market_baseline BETWEEN 0 AND 1 "
                + ("AND v.asset=? AND v.horizon=?" if asset is not None else ""),
                (MODEL, since, clock.isoformat(), clock.isoformat(), *params),
            ).fetchall()
        count = len(rows)
        return {
            "observed_contracts": int(counts["observed"]),
            "learned_contracts": int(counts["learned"] or 0),
            "settled_forward_contracts": count,
            "score_window_days": 7,
            "model": MODEL,
            "forward_brier": (
                sum((r["probability_up"]-int(r["direction"] == "UP"))**2
                    for r in rows)/count if count else None
            ),
            "market_baseline_brier": (
                sum((r["market_baseline"]-int(r["direction"] == "UP"))**2
                    for r in rows)/count if count else None
            ),
            "verified_trading_edge": False,
        }
