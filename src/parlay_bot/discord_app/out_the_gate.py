"""Display-only out-the-gate predictions.

Two values, one wall:

- The **OTG call** is the prediction as first issued: the issuance-time row
  (direction derived once, confidence, ``issued_at``, model version, inputs
  fingerprint). It is immutable from the commit of the issuance transaction
  and is the only thing history, settlement, training, and accuracy stats
  ever see.
- The **live lean** is the same model's current fitted probability for the
  same (stream, horizon, target), recomputed at render time from the latest
  candles and the current persisted weights. It is a view, never written
  anywhere: it never settles, never counts in accuracy, and never feeds
  training.

This module is display-only by construction, not by convention:

- It imports no writer. It never calls ``fit``/``learn``/``call``/``issue``/
  ``settle``/``evaluate``/``review_model``/``_run_once`` or any receipt
  writer, and it contains no INSERT/UPDATE/DELETE statement. An import-audit
  test (``tests/test_out_the_gate.py``) fails the build if any of those
  appear here.
- The live lean reads persisted weights, never fits: the incumbent lean
  reads the latest ``rival_live_model_checkpoints`` row and validates it
  with ``LiveModelCheckpoints._restore`` (digest-checked, read-only).
  flow_v3 is retired (Nov 7, 2026): it has no live lean, like every other
  substituted family. A lean that cannot be recomputed from persisted state
  is reported unavailable, never approximated from a different model.
- Confidence is ``max(p, 1-p) x 100`` in whole percent (the mapping every
  existing display string already uses). 50% is a coin flip, never a call.
  Served probabilities are raw, uncalibrated model output unless the row is
  a flow_v3 probability whose Platt fit passed its gate; the display must
  say which one it is.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import feature_vector, persisted_candles
from parlay_bot.perpetuals.flow_challenger_v3 import FlowChallengerV3
from parlay_bot.perpetuals.fusion import (
    SourceForecast,
    SourceSkill,
    fuse_forecasts,
)
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS
from parlay_bot.perpetuals.model_checkpoints import (
    FEATURE_CONTRACT,
    TARGET_CONTRACT,
    LiveModelCheckpoints,
)
from parlay_bot.perpetuals.multi_exchange import (
    MAX_SOURCE_AGE,
    MIN_QUORUM,
    VENUES,
)
from parlay_bot.perpetuals.promotion import promoted_engine
from parlay_bot.perpetuals.readiness import _source_identity

_LOG = logging.getLogger(__name__)

# Mirrors signals.WATCH_THRESHOLD (signals.py:49). A test asserts the two
# stay equal; this module must not import signals (signals imports the
# opportunity readers, which consume this module).
WATCH_THRESHOLD = 0.56
MIN_REMAINING = timedelta(seconds=90)
CANDLE_FRESHNESS = timedelta(minutes=10)
INDICATOR_BARS = 40
OTG_DISCLOSURE = "raw, uncalibrated model output; its own estimate"
CALIBRATED_DISCLOSURE = "Platt-calibrated (ECE \u2264 0.10 on validation)"
BELOW_WATCH_LINE = "below the 56% watch line, not a qualified call"
JUST_WATCH = "Just watch: this is not a signal to trade."


def direction_of(probability: float) -> str:
    """Canonical direction: FLAT at exactly 0.5 (signals.py:504-507)."""
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("probability must be a finite value in (0, 1)")
    if probability > 0.5:
        return "UP"
    if probability < 0.5:
        return "DOWN"
    return "FLAT"


def confidence_pct(probability: float) -> int:
    """Whole-percent confidence: max(p, 1-p) x 100. 50% = coin flip."""
    if not math.isfinite(probability) or not 0 < probability < 1:
        raise ValueError("probability must be a finite value in (0, 1)")
    return round(max(probability, 1 - probability) * 100)


def strength_band(probability: float) -> str:
    """Confidence bands, identical to signals.model_strength()."""
    if not math.isfinite(probability) or not 0 < probability < 1:
        return "invalid"
    score = max(probability, 1 - probability)
    if score < 0.53:
        return "very weak"
    if score < WATCH_THRESHOLD:
        return "weak"
    return "directional watch"


def _sigmoid_probability(weights, features) -> float:
    """Logistic probability, mirroring forward_audit._probability (clamp +-30)."""
    if len(weights) != len(features) + 1:
        raise ValueError("invalid trained shadow model")
    value = max(-30.0, min(30.0, sum(
        w * x for w, x in zip(weights, (1.0, *features))
    )))
    return 1.0 / (1.0 + math.exp(-value))


def _utc(value) -> datetime:
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return instant.astimezone(UTC)


def _latest_candles(market, *, venue: str, contract: str, quote: str,
                    now: datetime):
    """Latest continuous 5m candles, or None when stale / unusable."""
    rows = market.recent_candles(
        venue=venue, contract=contract, quote=quote,
        interval_minutes=5, count=INDICATOR_BARS + 20, as_of=now,
    )
    if len(rows) < INDICATOR_BARS:
        return None
    candles = persisted_candles(rows)
    last = candles[-1]
    if not timedelta(0) <= now - _utc(last.observed_at) <= CANDLE_FRESHNESS:
        return None
    if last.trades == 0:
        return None
    try:
        features = feature_vector(candles)
    except (ValueError, TypeError):
        return None
    return candles, features, _utc(last.observed_at)


def _incumbent_lean(market, *, venue: str, contract: str, quote: str,
                    horizon: int, now: datetime) -> dict | None:
    """Current incumbent probability from persisted weights + latest candles.

    Read-only: the checkpoint row is SELECTed and digest-validated with
    ``_restore``; ``fit()`` is never called from a render path.
    """
    got = _latest_candles(
        market, venue=venue, contract=contract, quote=quote, now=now,
    )
    if got is None:
        return None
    _, features, observed_at = got
    try:
        with market.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM rival_live_model_checkpoints
                   WHERE venue=? AND contract=? AND quote=?
                     AND horizon_minutes=? AND feature_contract=?
                     AND target_contract=?""",
                (venue, contract, quote, horizon,
                 FEATURE_CONTRACT, TARGET_CONTRACT),
            ).fetchone()
        if row is None:
            return None
        state = LiveModelCheckpoints._restore(dict(row), now)
        if state is None:
            return None
        weights = state["weights"]
        if len(weights) != 11:
            return None
        probability = _sigmoid_probability(tuple(weights), features)
    except Exception:  # noqa: BLE001 - an unavailable lean is None, never an error
        return None
    if not 0 < probability < 1 or not math.isfinite(probability):
        return None
    return {
        "probability": probability,
        "as_of": observed_at,
        "weights_trained_at": row["trained_at"],
        "calibrated": False,
        "engine": "live",
    }


def _fused_lean(market, *, asset: str, horizon: int,
                now: datetime) -> dict | None:
    """Current fused probability: fuse the per-venue incumbent live leans.

    Mirrors ``MultiExchangeResearch.issue`` (same quorum, same quality
    formula, same 30-day skill query, same ``fuse_forecasts``) with one
    difference: each source probability is the venue's live lean recomputed
    now, not an issued row. The lean is a view computed at render time, so
    its ``observed_at`` is the render instant; staleness is still penalised
    through the quality formula's health/candle lags.
    """
    stamp = now.astimezone(UTC)
    from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL

    try:
        with market.store.connect() as conn:
            skill_rows = conn.execute(
                """SELECT asset,venue,horizon_minutes,
                          COUNT(*) AS n,SUM(model_brier) AS loss
                   FROM perp_forward_audit
                   WHERE model_version=? AND settled_at IS NOT NULL
                     AND model_brier IS NOT NULL
                     AND settled_at<? AND issued_at>=?
                   GROUP BY asset,venue,horizon_minutes""",
                (LIVE_MODEL, stamp.isoformat(),
                 (stamp - timedelta(days=30)).isoformat()),
            ).fetchall()
            feed_rows = conn.execute(
                """SELECT venue,asset,status,last_success_at,last_candle_close
                   FROM perp_feed_health"""
            ).fetchall()
    except Exception:  # noqa: BLE001 - an unavailable lean is None, never an error
        return None
    feed_by_key = {(str(x["venue"]), str(x["asset"])): x for x in feed_rows}
    skill = {
        (str(x["asset"]), str(x["venue"]), int(x["horizon_minutes"])):
            SourceSkill(count=int(x["n"]), brier_sum=float(x["loss"]),
                        recent_count=float(x["n"]),
                        recent_brier_sum=float(x["loss"]))
        for x in skill_rows if x["loss"] is not None
    }
    candidates: list[SourceForecast] = []
    for venue in sorted(VENUES):
        try:
            contract, quote = _source_identity(venue, asset)
        except Exception:  # noqa: BLE001 - unknown venue/asset pair is skipped
            _LOG.debug("RivaL fused lean skips unknown venue=%s asset=%s", venue, asset)
            continue
        health = feed_by_key.get((venue, asset))
        if health is None or health.get("status") != "ok":
            continue
        try:
            health_lag = (stamp - _utc(str(health["last_success_at"]))).total_seconds()
            candle_lag = (stamp - _utc(str(health["last_candle_close"]))).total_seconds()
        except (TypeError, ValueError, KeyError):
            _LOG.debug("RivaL fused lean skips venue=%s with unreadable feed row", venue)
            continue
        if not 0 <= health_lag <= 900 or not 0 <= candle_lag <= 1200:
            continue
        lean = _incumbent_lean(
            market, venue=venue, contract=contract, quote=quote,
            horizon=horizon, now=now,
        )
        if lean is None:
            continue
        quality = max(.2, min(1., 1. - (
            .2 * health_lag / 900.
            + .2 * candle_lag / 1200.
        )))
        got = _latest_candles(
            market, venue=venue, contract=contract, quote=quote, now=now,
        )
        reference_price = float(got[0][-1].close_price) if got else 0.0
        if reference_price <= 0:
            continue
        candidates.append(SourceForecast(
            source=venue, origin=venue, asset=asset,
            quote="USD-DIRECTION", horizon_minutes=horizon,
            observed_at=stamp,
            probability_up=float(lean["probability"]), quality=quality,
            reference_price=reference_price,
        ))
    if len(candidates) < MIN_QUORUM:
        return None

    def historical_skill(candidate_asset: str, _quote: str, origin: str,
                         candidate_horizon: int) -> SourceSkill:
        return skill.get(
            (candidate_asset, origin, candidate_horizon), SourceSkill(),
        )

    try:
        combined = fuse_forecasts(
            candidates, issued_at=stamp, skill_for=historical_skill,
            max_age=MAX_SOURCE_AGE,
        )
    except (ValueError, TypeError):
        return None
    if combined is None:
        return None
    return {
        "probability": float(combined.probability_up),
        "as_of": stamp,
        "weights_trained_at": None,
        "calibrated": False,
        "engine": "multi",
        "contributor_count": len(combined.contributors),
    }


def live_lean(market, *, asset: str, horizon: int, venue: str,
              contract: str | None = None, quote: str | None = None,
              now: datetime | None = None, engine: str = "live",
              model_version: str | None = None) -> dict | None:
    """Current fitted probability for one stream/slot, recomputed at render.

    Returns ``{"probability", "as_of", "weights_trained_at", "calibrated",
    "engine"}`` or None when the lean cannot be recomputed from persisted
    state (no checkpoint, stale candles, fusion quorum unmet, or a family
    with no read-only recompute path). Never raises, never writes, never
    approximates one model with another: a substituted family has no lean
    here by design. flow_v3 is retired (Nov 7, 2026) and has no lean.
    """
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("lean clock requires timezone")
    stamp = stamp.astimezone(UTC)
    if asset not in ASSETS or horizon not in HORIZONS:
        raise ValueError("unknown asset or horizon")
    try:
        if engine == "multi":
            return _fused_lean(market, asset=asset, horizon=horizon, now=stamp)
        if contract is None or quote is None:
            contract, quote = _source_identity(venue, asset)
        from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL
        from parlay_bot.perpetuals.model_competition import (
            CHALLENGER_PREFIXES,
            FAMILIES,
        )
        if model_version is not None and model_version != LIVE_MODEL and (
            model_version in FAMILIES
            or model_version.startswith(CHALLENGER_PREFIXES)
        ):
            # A substituted shadow family with no read-only recompute path:
            # its live lean is unavailable, never the incumbent's lean.
            return None
        return _incumbent_lean(
            market, venue=venue, contract=contract, quote=quote,
            horizon=horizon, now=stamp,
        )
    except Exception:  # noqa: BLE001 - an unavailable lean is None, never an error
        return None


def otg_snapshot(watch: dict, *, market=None) -> dict:
    """The out-the-gate call for one issued watch row, derived once.

    ``watch`` is a row as returned by ``recent_watches`` /
    ``recent_multi_watches``: the issuance-time snapshot (direction derived
    once from ``probability_up``, confidence, ``issued``/``target``, model
    version). ``market`` is only needed to label a flow_v3 row calibrated
    (Platt fit passed its gate); without it the row reads uncalibrated.
    """
    probability = float(watch["probability_up"])
    direction = direction_of(probability)
    calibrated = False
    if market is not None and str(watch.get("model_version")) == "flow_v3":
        try:
            status = FlowChallengerV3.calibration_status(
                market.store, str(watch["asset"]),
                int(watch["horizon_minutes"]),
            )
            calibrated = (status or {}).get("status") == "evaluated"
        except Exception:  # noqa: BLE001 - a failed label check reads uncalibrated
            calibrated = False
    return {
        "asset": str(watch["asset"]),
        "venue": str(watch["venue"]),
        "horizon_minutes": int(watch["horizon_minutes"]),
        "direction": direction,
        "confidence_pct": confidence_pct(probability),
        "probability": probability,
        "issued": watch["issued"],
        "target": watch["target"],
        "model_version": watch.get("model_version"),
        "calibrated": calibrated,
    }


def otg_display(market, *, asset: str, horizon: int,
                now: datetime | None = None) -> dict:
    """The composed slot view: serving-engine OTG call + per-venue leans.

    Headline is the serving call for the slot (first priority venue with a
    fresh forecast: hyperliquid, binance, okx, bitget); the expansion lists
    every venue that issued for the slot. Each row carries its OTG snapshot
    and its live lean (or ``None`` when the lean is unavailable). The
    readers are imported here, not at module level: the opportunity readers
    are consumed by this module's callers, so a top-level import would be
    circular.
    """
    from parlay_bot.discord_app.opportunities import (
        recent_multi_watches,
        recent_watches,
    )

    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("display clock requires timezone")
    stamp = stamp.astimezone(UTC)
    if asset not in ASSETS or horizon not in HORIZONS:
        raise ValueError("unknown asset or horizon")
    try:
        engine = promoted_engine(
            market.store, asset=asset, horizon=horizon, now=stamp,
        )
    except Exception:  # noqa: BLE001 - promotion lookup never blocks display
        engine = "live"
    reader = recent_multi_watches if engine == "multi" else recent_watches
    try:
        watches = reader(
            market, asset=asset, now=stamp, limit=40,
            dedupe_horizons=False, horizon_minutes=horizon,
        )
    except Exception:  # noqa: BLE001 - no watches is an empty slot, not an error
        watches = []
    live = [
        watch for watch in watches
        if watch["target"] + timedelta(minutes=5) - stamp >= MIN_REMAINING
    ]
    if not live:
        return {
            "asset": asset, "horizon_minutes": horizon, "engine": engine,
            "status": "no_forecast", "headline": None, "venues": [],
            "as_of": stamp,
        }

    def _priority(watch: dict) -> tuple[int, float]:
        order = {"hyperliquid": 0, "binance": 1, "okx": 2, "bitget": 3}
        return (order.get(str(watch["venue"]), 4),
                -watch["issued"].timestamp())

    ordered = sorted(live, key=_priority)
    rows: list[dict] = []
    seen: set[str] = set()
    for watch in ordered:
        venue = str(watch["venue"])
        if venue in seen:
            continue
        seen.add(venue)
        snapshot = otg_snapshot(watch, market=market)
        lean = live_lean(
            market, asset=asset, horizon=horizon, venue=venue,
            contract=watch.get("contract"), quote=watch.get("quote"),
            now=stamp, engine=engine,
            model_version=watch.get("model_version"),
        )
        rows.append({"watch": watch, "otg": snapshot, "lean": lean})
    headline = rows[0] if rows else None
    return {
        "asset": asset, "horizon_minutes": horizon, "engine": engine,
        "status": "ok", "headline": headline, "venues": rows,
        "as_of": stamp,
    }


def lean_short(lean: dict | None) -> str | None:
    """Short lean text for inline lists (\"UP 58% as of 14:37\"), else None."""
    if lean is None:
        return None
    direction = direction_of(float(lean["probability"]))
    as_of = lean["as_of"]
    as_of_text = as_of.strftime("%H:%M") if isinstance(as_of, datetime) else str(as_of)
    return f"{direction} {confidence_pct(float(lean['probability']))}% as of {as_of_text}"


def lean_line(lean: dict | None, *, as_of_label: str = "Live lean") -> str:
    """One display line for a live lean, or its unavailable form."""
    if lean is None:
        return f"{as_of_label} unavailable \u2014 data stale"
    direction = direction_of(float(lean["probability"]))
    as_of = lean["as_of"]
    as_of_text = as_of.strftime("%H:%M:%S") if isinstance(as_of, datetime) else str(as_of)
    return (
        f"{as_of_label}: {direction} {confidence_pct(float(lean['probability']))}% "
        f"as of {as_of_text} \u2014 updating as data arrives (not the issued call)"
    )


def otg_line(snapshot: dict) -> str:
    """One display line for an OTG call, with its calibration disclosure."""
    issued = snapshot["issued"]
    issued_text = issued.strftime("%H:%M") if isinstance(issued, datetime) else str(issued)
    disclosure = CALIBRATED_DISCLOSURE if snapshot.get("calibrated") else OTG_DISCLOSURE
    return (
        f"Out the gate: {snapshot['direction']} {snapshot['confidence_pct']}% "
        f"\u2014 issued {issued_text} ({disclosure})"
    )


def below_line_label(snapshot: dict) -> str | None:
    """The D1(a) label for a sub-threshold lean, else None."""
    if snapshot["direction"] == "FLAT":
        return None
    if strength_band(snapshot["probability"]) == "directional watch":
        return None
    return (
        f"Lean: {snapshot['direction']} {snapshot['confidence_pct']}% \u2014 "
        f"{BELOW_WATCH_LINE}"
    )
