"""Source-matched Polymarket oracle challenger, never an exchange-price fallback.

The live CLOB book alone does not contain the resolution oracle's current
price. This research track runs ONLY when a separately validated, recent,
settlement-source-matched reference observation has been supplied. Until then
the existing book-only learner continues without invented threshold distance.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.learning import _sigmoid
from parlay_bot.perpetuals.model_checkpoints import canonical
from parlay_bot.perpetuals.polymarket_learning import book_features

MODEL = "polymarket-oracle-context-shadow-v2"
FEATURE_CONTRACT = "book10-oracle-distance-volatility3-v2"
MIN_LABELS = 100


@dataclass(frozen=True)
class OracleReference:
    asset: str
    resolution_source: str
    observed_at: datetime
    price: float
    horizon_volatility_pct: float
    source_receipt_sha256: str
    method: str  # chainlink-twap-60s or binance-spot-hourly


def oracle_features(notice, reference: OracleReference, *, now: datetime) -> tuple[float, ...]:
    """Reject mismatched oracle, missing strike, stale price and unknown rules."""
    if not isinstance(reference, OracleReference):
        raise TypeError("verified oracle reference required")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("UTC reference clock required")
    checked = now.astimezone(UTC)
    quoted, observed = notice.quote_at, reference.observed_at
    if (quoted.tzinfo is None or quoted.utcoffset() is None
        or observed.tzinfo is None or observed.utcoffset() is None):
        raise ValueError("oracle timestamps must be timezone-aware")
    if (
        reference.asset != notice.asset
        or not notice.resolution_source
        or reference.resolution_source != notice.resolution_source
        or not notice.price_to_beat
        or not isinstance(reference.source_receipt_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", reference.source_receipt_sha256) is None
        or not math.isfinite(reference.price) or reference.price <= 0
        or not math.isfinite(notice.price_to_beat) or notice.price_to_beat <= 0
        or not math.isfinite(reference.horizon_volatility_pct)
        or not 0 < reference.horizon_volatility_pct <= 20
        or not (notice.issued <= quoted <= checked < notice.expires)
        or not (notice.issued <= observed <= checked < notice.expires)
        or not timedelta(0) <= checked-quoted <= timedelta(seconds=25)
        or not timedelta(0) <= checked-observed <= timedelta(seconds=25)
        or abs((quoted-observed).total_seconds()) > 20
    ):
        raise ValueError("oracle evidence stale, mismatched or incomplete")
    source = reference.resolution_source.lower()
    rule = (notice.rules or "").lower()
    if notice.horizon in (5, 15):
        if (reference.method != "chainlink-twap-60s"
            or not ("chainlink" in source or "chain.link" in source) or "twap" not in rule
            or not re.search(r"60[ -]?(?:second|sec|s)", rule)):
            raise ValueError("current short-window oracle rule not verified")
    elif notice.horizon == 60:
        # Do not hardcode an exchange from the timeframe: current official
        # Up/Down rules describe Chainlink TWAP, while individual listed
        # contracts may publish another exact settlement source.
        chainlink = (
            reference.method == "chainlink-twap-60s"
            and ("chainlink" in source or "chain.link" in source) and "twap" in rule
            and re.search(r"60[ -]?(?:second|sec|s)", rule)
        )
        binance = (
            reference.method == "binance-spot-hourly"
            and "binance" in source and "binance" in rule and "hour" in rule
        )
        if not (chainlink or binance):
            raise ValueError("hourly oracle rule not verified")
    else:
        raise ValueError("unsupported oracle settlement horizon")
    base = book_features(notice)
    distance_pct = 100. * math.log(reference.price/notice.price_to_beat)
    expected_vol = reference.horizon_volatility_pct
    return (*base, max(-5., min(5., distance_pct)),
            max(-5., min(5., distance_pct/expected_vol)),
            min(5., expected_vol))


class PolymarketOracleChallenger:
    """Independent persistent forward-only experiment, with no auto-promotion."""

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
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS rival_poly_oracle_observations (
                        slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
                        asset TEXT NOT NULL, horizon INTEGER NOT NULL,
                        resolution_source TEXT NOT NULL, expires_at TEXT NOT NULL,
                        observed_at TEXT NOT NULL, features_json TEXT NOT NULL,
                        evidence_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL,
                        learned_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_oracle_weights (
                        asset TEXT NOT NULL, horizon INTEGER NOT NULL,
                        resolution_source TEXT NOT NULL, model_version TEXT NOT NULL,
                        weights_json TEXT NOT NULL, weights_sha256 TEXT NOT NULL,
                        updates INTEGER NOT NULL, trained_at TEXT NOT NULL,
                        PRIMARY KEY(asset,horizon,resolution_source,model_version)
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_oracle_forward (
                        slug TEXT NOT NULL, model_version TEXT NOT NULL,
                        issued_at TEXT NOT NULL, weights_sha256 TEXT NOT NULL,
                        probability_up DOUBLE PRECISION NOT NULL,
                        market_baseline DOUBLE PRECISION NOT NULL,
                        input_evidence_sha256 TEXT NOT NULL,
                        oracle_receipt_sha256 TEXT NOT NULL,
                        PRIMARY KEY(slug,model_version)
                    );
                """)
            self._initialized = True

    def observe(self, notice, reference: OracleReference, *, now: datetime | None = None):
        stamp = now or datetime.now(UTC)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("oracle prediction clock must be timezone-aware")
        stamp = stamp.astimezone(UTC)
        features = oracle_features(notice, reference, now=stamp)
        if (not notice.condition_id or not notice.up_token_id
            or not notice.down_token_id or notice.up_token_id == notice.down_token_id):
            raise ValueError("unverified contract identity")
        self.initialize()
        evidence = canonical({
            "feature_contract": FEATURE_CONTRACT, "slug": notice.slug,
            "condition_id": notice.condition_id, "resolution_source": notice.resolution_source,
            "rules": notice.rules, "quote_at": notice.quote_at.isoformat(),
            "reference_at": reference.observed_at.isoformat(),
            "reference_receipt": reference.source_receipt_sha256,
            "method": reference.method, "price": reference.price,
            "price_to_beat": notice.price_to_beat,
            "volatility_pct": reference.horizon_volatility_pct,
            "features": features,
        })
        digest = hashlib.sha256(evidence.encode()).hexdigest()
        scope = (notice.asset, notice.horizon, notice.resolution_source, MODEL)
        with self._lock, self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_poly_oracle_observations
                   (slug,condition_id,asset,horizon,resolution_source,expires_at,
                    observed_at,features_json,evidence_sha256,evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING""",
                (notice.slug, notice.condition_id, notice.asset, notice.horizon,
                 notice.resolution_source, notice.expires.isoformat(),
                 stamp.isoformat(), canonical(features), digest, evidence),
            )
            zeros = canonical([0.] * (len(features) + 1))
            conn.execute(
                """INSERT INTO rival_poly_oracle_weights
                   (asset,horizon,resolution_source,model_version,weights_json,
                    weights_sha256,updates,trained_at) VALUES (?,?,?,?,?,?,0,?)
                   ON CONFLICT DO NOTHING""",
                (*scope, zeros, hashlib.sha256(zeros.encode()).hexdigest(), stamp.isoformat()),
            )
            suffix = " FOR UPDATE" if self.store.database.backend == "postgres" else ""
            state = conn.execute(
                """SELECT weights_json,weights_sha256,updates
                   FROM rival_poly_oracle_weights WHERE asset=? AND horizon=?
                     AND resolution_source=? AND model_version=?""" + suffix, scope,
            ).fetchone()
            weights = json.loads(state["weights_json"])
            if (len(weights) != len(features) + 1
                or not all(math.isfinite(x) for x in weights)
                or hashlib.sha256(canonical(weights).encode()).hexdigest()
                   != state["weights_sha256"]):
                raise ValueError("corrupt oracle challenger weights")
            rows = conn.execute(
                """SELECT v.slug,v.features_json,o.direction
                   FROM rival_poly_oracle_observations v
                   JOIN rival_polymarket_ai_outcomes o
                     ON o.slug=v.slug AND o.condition_id=v.condition_id
                   WHERE v.asset=? AND v.horizon=? AND v.resolution_source=?
                     AND v.learned_at IS NULL
                     AND v.observed_at<v.expires_at
                     AND o.resolved_at>=v.expires_at AND o.observed_at<=?
                   ORDER BY o.observed_at,v.slug LIMIT 200""",
                (*scope[:3], stamp.isoformat()),
            ).fetchall()
            updates = int(state["updates"])
            for row in rows:
                x = (1., *json.loads(row["features_json"]))
                y = int(row["direction"] == "UP")
                error = y - _sigmoid(sum(w*v for w, v in zip(weights, x)))
                for i, v in enumerate(x):
                    weights[i] += .025 * (error*v - (.001 * weights[i] if i else 0))
                conn.execute(
                    """UPDATE rival_poly_oracle_observations SET learned_at=?
                       WHERE slug=? AND learned_at IS NULL""",
                    (stamp.isoformat(), row["slug"]),
                )
                updates += 1
            if rows:
                encoded = canonical(weights)
                conn.execute(
                    """UPDATE rival_poly_oracle_weights
                       SET weights_json=?,weights_sha256=?,updates=?,trained_at=?
                       WHERE asset=? AND horizon=? AND resolution_source=?
                         AND model_version=?""",
                    (encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                     updates, stamp.isoformat(), *scope),
                )
            if updates < MIN_LABELS:
                return None
            if stamp >= notice.expires:
                return None
            p = _sigmoid(sum(w*x for w, x in zip(weights, (1., *features))))
            sha = hashlib.sha256(canonical(weights).encode()).hexdigest()
            conn.execute(
                """INSERT INTO rival_poly_oracle_forward
                   (slug,model_version,issued_at,weights_sha256,probability_up,
                    market_baseline,input_evidence_sha256,oracle_receipt_sha256)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (notice.slug, MODEL, stamp.isoformat(), sha, p,
                 notice.up_ask/(notice.up_ask+notice.down_ask),
                 digest, reference.source_receipt_sha256),
            )
        return {"model": MODEL, "probability_up": p, "training_labels": updates,
                "research_only": True, "verified_trading_edge": False}

    def summary(self) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            counts = conn.execute(
                """SELECT COUNT(*) AS observed,
                   SUM(CASE WHEN learned_at IS NOT NULL THEN 1 ELSE 0 END) AS learned
                   FROM rival_poly_oracle_observations"""
            ).fetchone()
            forward = conn.execute(
                """SELECT f.probability_up,f.market_baseline,o.direction
                   FROM rival_poly_oracle_forward f
                   JOIN rival_poly_oracle_observations v ON v.slug=f.slug
                   JOIN rival_polymarket_ai_outcomes o
                     ON o.slug=v.slug AND o.condition_id=v.condition_id
                   WHERE f.issued_at<o.resolved_at"""
            ).fetchall()
        return {
            "model": MODEL, "observed_contracts": int(counts["observed"]),
            "learned_contracts": int(counts["learned"] or 0),
            "forward_resolved": len(forward),
            "model_brier": (
                sum((r["probability_up"] - int(r["direction"] == "UP"))**2
                    for r in forward)/len(forward) if forward else None
            ),
            "market_baseline_brier": (
                sum((r["market_baseline"] - int(r["direction"] == "UP"))**2
                    for r in forward)/len(forward) if forward else None
            ),
            "promoted": False,
        }
