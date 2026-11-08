"""regime_v1 challenger (spec rival-regime-gated-spec-20261107).

Covers: the Kaufman efficiency-ratio gate (values, the frozen 0.30
threshold, closed-bars-only causality, gaps/venue changes abstain),
regime_calls() reusing the incumbent's probability verbatim, the
"regime_" prefix accepted by CHALLENGER_PREFIXES and issue() so rows are
not silently dropped, generic settlement copying the incumbent outcome,
the selected() continuity branch (no D14 pin: no offline artifact),
evaluate() recording the artifact and qualifying on the issued subset,
the D5 selection decomposition (paired gain + selection effect +
coverage) in trial_report(), the regime_summary() family slice, the
kill switch in the 5-minute cycle (flag on issues, flag off issues
nothing new, choppy cycles abstain while the incumbent still runs, a
gate exception never blocks the incumbent), and D8 (no OTG live lean).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.out_the_gate import live_lean
from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import (
    CHALLENGER_PREFIXES,
    ShadowModelCompetition,
)
from parlay_bot.perpetuals.regime_evidence import VERSION as REGIME_VERSION
from parlay_bot.perpetuals.regime_gate import (
    ER_THRESHOLD,
    efficiency_ratio,
    trending_gate,
)
from parlay_bot.perpetuals.regime_model import (
    FAMILY,
    FEATURE_DIGEST,
    artifact_identity,
    regime_calls,
    trial_report,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_archive_v1_shadow import (
    _insert_shadow_row,
    _promotion_row,
    _store,
)

NOW = datetime(2026, 11, 7, 12, 0, 30, tzinfo=UTC)


def _candles(closes, *, start=None, venue="hyperliquid"):
    """Consecutive closed 5-minute MarketCandles with the given closes."""
    start = start or (NOW - timedelta(minutes=5 * len(closes)))
    out = []
    for i, close in enumerate(closes):
        opened = start + timedelta(minutes=5 * i)
        out.append(MarketCandle(
            venue=venue, contract="BTC", quote="USDC", interval_minutes=5,
            opened_at=opened, closed_at=opened + timedelta(minutes=5),
            observed_at=opened + timedelta(minutes=5),
            open_price=float(close), high_price=float(close),
            low_price=float(close), close_price=float(close),
            volume_base=10, trades=12,
        ))
    return out


def _seed_shape_candles(market, *, now, shape, bars=500):
    """500 live 5m bars on hyperliquid/BTC with a controlled shape.

    "trend" is a straight climb (ER 1.0, gate passes); "choppy" alternates
    100/101 (ER 0.0, gate abstains). The standard seeder's sawtooth sits
    at ER 0.2978, just below the frozen threshold, so neither cycle test
    can use it.
    """
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    market.initialize()
    with market.store.connect() as conn:
        for i in range(bars):
            opened = boundary - timedelta(minutes=5 * (bars - i))
            closed = opened + timedelta(minutes=5, milliseconds=-1)
            observed = closed + timedelta(milliseconds=1)
            if shape == "trend":
                p = 100 + i * 0.5
            elif shape == "choppy":
                p = 100 + (i % 2)
            else:  # pragma: no cover - test helper misuse
                raise ValueError(shape)
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "hyperliquid", "BTC", "USDC", 5,
                    opened.isoformat(), closed.isoformat(), observed.isoformat(),
                    p, p + 2, p - 2, p + 0.5, 10, 12,
                ),
            )


def _insert_regime_row(conn, *, target, issued, p, incumbent_p, baseline_p=0.45,
                       y=1, ref=None, settled=True, sha=None):
    """One settled regime_v1 shadow row with explicit per-side probabilities."""
    ref = ref or (target - timedelta(minutes=5))
    settled_at = (target + timedelta(minutes=10)).isoformat() if settled else None
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts
           (venue,contract,quote,asset,horizon_minutes,family,
            reference_opened_at,issued_at,target_opened_at,
            probability_up,incumbent_up,baseline_up,
            training_count,training_sha256,settled_at,outcome_up,
            model_brier,incumbent_brier,baseline_brier)
           VALUES ('hyperliquid','BTC','USDC','BTC',5,'regime_v1',?,?,?,?,?,?,100,?,
                   ?,?,?,?,?)""",
        (ref.isoformat(), issued.isoformat(), target.isoformat(),
         p, incumbent_p, baseline_p, sha or "a" * 64, settled_at,
         y if settled else None,
         (p - y) ** 2 if settled else None,
         (incumbent_p - y) ** 2 if settled else None,
         (baseline_p - y) ** 2 if settled else None),
    )


def _insert_incumbent_row(conn, *, target, issued, p, y=1, ref=None):
    """One settled incumbent row in perp_forward_audit."""
    ref = ref or (target - timedelta(minutes=5))
    conn.execute(
        """INSERT INTO perp_forward_audit
           (venue,contract,quote,asset,horizon_minutes,model_version,
            reference_opened_at,reference_close,issued_at,target_opened_at,
            probability_up,baseline_up,training_evaluated,
            settled_at,outcome_up,realized_close,model_brier,baseline_brier)
           VALUES ('hyperliquid','BTC','USDC','BTC',5,?,?,?, ?,?,?,?,100,
                   ?,?,101.0,?,?)""",
        (FORWARD_MODEL, ref.isoformat(), 100.0, issued.isoformat(),
         target.isoformat(), p, 0.5,
         (target + timedelta(minutes=10)).isoformat(), y,
         (p - y) ** 2, (0.5 - y) ** 2),
    )


def _seed_promotion(store, now, *, family="regime_v1", quote_count=168,
                    gross=350.0, venue="hyperliquid", horizon=5):
    """21 days x 8 targets of winning regime_v1 rows + matched quote rows.

    Same shape as the family cost-promotion fixture, but for regime_v1:
    the family wins every target (p 0.95 vs incumbent 0.5, outcome up)
    with full quote coverage, so Route B qualifies it on the subset.
    """
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    FamilyQuoteShadow(store).initialize()
    with store.connect() as conn:
        for d in range(21):
            for i in range(8):
                target = now - timedelta(days=23 - d) + timedelta(minutes=horizon * i)
                issued = target - timedelta(seconds=40)
                reference = target - timedelta(minutes=5)
                settled = target + timedelta(minutes=6)
                score = (0.95 - 1.0) ** 2
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (
                      venue,contract,quote,asset,horizon_minutes,family,
                      reference_opened_at,issued_at,target_opened_at,
                      probability_up,incumbent_up,baseline_up,
                      training_count,training_sha256,settled_at,outcome_up,
                      model_brier,incumbent_brier,baseline_brier)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, "BTC", "USDC", "BTC", horizon, family,
                     reference.isoformat(), issued.isoformat(), target.isoformat(),
                     0.95, 0.5, 0.5, 300, "a" * 64, settled.isoformat(), 1,
                     score, 0.25, 0.25),
                )
                if d * 8 + i >= quote_count:
                    continue
                entry = issued + timedelta(seconds=10)
                exit_at = target + timedelta(minutes=5, seconds=20)
                conn.execute(
                    """INSERT INTO rival_family_quote_shadow (
                      venue,contract,quote,asset,horizon_minutes,family,
                      reference_opened_at,issued_at,target_close_at,
                      training_sha256,challenger_up,incumbent_up,
                      challenger_side,incumbent_side,entry_at,
                      entry_bid,entry_ask,quantity,state,
                      exit_at,exit_bid,exit_ask,
                      challenger_gross_bps,incumbent_gross_bps)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, "BTC", "USDC", "BTC", horizon, family,
                     reference.isoformat(), issued.isoformat(),
                     (target + timedelta(minutes=5)).isoformat(),
                     "a" * 64, 0.95, 0.5, "LONG", "CASH", entry.isoformat(),
                     100.0, 100.1, 0.25, "EXIT_QUOTED",
                     exit_at.isoformat(), 104.0, 104.1, gross, 0.0),
                )
    return family


# --- D1: the gate -----------------------------------------------------------

def test_efficiency_ratio_values():
    # A straight line is perfectly efficient; pure noise is not.
    assert efficiency_ratio([100 + i for i in range(21)]) == 1.0
    assert efficiency_ratio([100 + (i % 2) for i in range(21)]) == 0.0
    # Flat closes have a zero denominator: 0.0, not a crash.
    assert efficiency_ratio([100.0] * 21) == 0.0
    # Unevaluatable input is None (abstain), never an exception.
    assert efficiency_ratio([100.0] * 20) is None
    assert efficiency_ratio([100.0, float("nan")] + [100.0] * 19) is None
    assert efficiency_ratio([float("inf")] + [100.0] * 20) is None
    assert efficiency_ratio([0.0] * 21) is None
    assert efficiency_ratio([100.0, -5.0] + [100.0] * 19) is None


def test_threshold_pinned_at_0_30_strict():
    assert ER_THRESHOLD == 0.30
    # ER exactly 0.30 abstains: the gate is strict (>), not >=.
    closes = [100.0]
    for move in [1.0] * 6 + [1.0, -1.0] * 7:
        closes.append(closes[-1] + move)
    assert len(closes) == 21
    assert efficiency_ratio(closes) == 0.30
    assert trending_gate(_candles(closes)) is False
    # ER 0.50 issues.
    closes = [100.0]
    for move in [1.0] * 10 + [1.0, -1.0] * 5:
        closes.append(closes[-1] + move)
    assert efficiency_ratio(closes) == 0.5
    assert trending_gate(_candles(closes)) is True


def test_gate_uses_closed_bars_only():
    """Bars before the last 21, and OHLC/volume, do not change the decision."""
    trending = [100 + i * 0.5 for i in range(21)]
    assert trending_gate(_candles(trending)) is True
    # Thirty flat bars before the trending window: same decision.
    assert trending_gate(_candles([50.0] * 30 + trending)) is True
    # Same closes with wild highs/lows/volume: same decision.
    wild = [
        replace(c, high_price=c.close_price + 500,
                low_price=c.close_price - 500, volume_base=99999)
        for c in _candles(trending)
    ]
    assert trending_gate(wild) is True
    # Too few bars abstains.
    assert trending_gate(_candles(trending[:20])) is False


def test_gate_abstains_on_gap_or_venue_change():
    candles = _candles([100 + i * 0.5 for i in range(21)])
    assert trending_gate(candles) is True
    shifted = list(candles)
    shifted[10] = replace(
        shifted[10], opened_at=shifted[10].opened_at + timedelta(minutes=5))
    assert trending_gate(shifted) is False
    other_venue = list(candles)
    other_venue[10] = replace(other_venue[10], venue="binance")
    assert trending_gate(other_venue) is False
    assert trending_gate([]) is False
    assert trending_gate(None) is False


# --- D3: regime_calls reuses the incumbent's probability --------------------

def test_regime_calls_reuses_incumbent_probability():
    trending = _candles([100 + i * 0.5 for i in range(21)])
    calls = regime_calls(
        candles=trending, probability=0.62, training_rows=100,
        model_sha256="a" * 64,
    )
    assert calls == [{
        "family": "regime_v1", "probability": 0.62,
        "training_rows": 100, "model_sha256": "a" * 64,
    }]
    # Choppy market: no call, no row.
    choppy = _candles([100 + (i % 2) for i in range(21)])
    assert regime_calls(
        candles=choppy, probability=0.62, training_rows=100,
        model_sha256="a" * 64,
    ) == []
    # An invalid incumbent probability or weights hash abstains, never raises.
    for bad_p in (0.0, 1.0, float("nan"), -0.1, 1.5):
        assert regime_calls(
            candles=trending, probability=bad_p, training_rows=100,
            model_sha256="a" * 64,
        ) == []
    for bad_sha in ("a" * 63, "A" * 64, "", None):
        assert regime_calls(
            candles=trending, probability=0.62, training_rows=100,
            model_sha256=bad_sha,
        ) == []
    assert regime_calls(
        candles=None, probability=0.62, training_rows=100,
        model_sha256="a" * 64,
    ) == []


# --- Prefix acceptance + kill switch flag ------------------------------------

def test_regime_prefix_accepted_everywhere_it_must_be():
    assert "regime_" in CHALLENGER_PREFIXES
    assert FAMILY.startswith(CHALLENGER_PREFIXES)
    assert forward_audit.ISSUE_REGIME_V1_CHALLENGERS is True


# --- issue() / settle() ------------------------------------------------------

def test_issue_accepts_regime_challenger_rows(tmp_path):
    store = _store(tmp_path, name="regime-issue.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        with store.connect() as conn:
            comp = ShadowModelCompetition(store)
            saved = comp.issue(
                conn, samples=[], features=(0.25,) * 10, now=NOW,
                venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5,
                reference_opened_at=NOW - timedelta(minutes=30),
                target_opened_at=NOW - timedelta(minutes=25),
                issued_at=NOW - timedelta(minutes=25),
                incumbent_up=0.6, baseline_up=0.45,
                challengers=[{"family": "regime_v1", "probability": 0.6,
                              "training_rows": 100, "model_sha256": "a" * 64}],
            )
            assert saved == 1
            row = conn.execute(
                """SELECT family,probability_up,incumbent_up,training_count,
                          training_sha256
                   FROM rival_model_shadow_forecasts
                   WHERE family='regime_v1'"""
            ).fetchone()
            # The regime call IS the incumbent's call: same probability.
            assert row["probability_up"] == 0.6
            assert row["incumbent_up"] == 0.6
            assert row["training_count"] == 100
            assert row["training_sha256"] == "a" * 64
    finally:
        store.close()


def test_regime_rows_settle_from_incumbent_outcome(tmp_path):
    store = _store(tmp_path, name="regime-settle.sqlite")
    try:
        market = PerpetualMarketStore(store)
        ForwardAccuracyMonitor(market).initialize()
        comp = ShadowModelCompetition(store)
        opened = NOW - timedelta(minutes=35)
        target = NOW - timedelta(minutes=25)
        issued = NOW - timedelta(minutes=30)
        # Settlement must be strictly after the target bar closes
        # (target + 5 min), or _settle_batch refuses the row.
        settled = NOW - timedelta(minutes=19)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,
                     target_opened_at,probability_up,baseline_up,
                     training_evaluated,settled_at,outcome_up)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
                 opened.isoformat(), 100.0, issued.isoformat(),
                 target.isoformat(), 0.6, 0.45, 100,
                 settled.isoformat(), 1),
            )
            assert comp.issue(
                conn, samples=[], features=(0.25,) * 10, now=issued,
                venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5,
                reference_opened_at=opened,
                target_opened_at=target, issued_at=issued,
                incumbent_up=0.6, baseline_up=0.45,
                challengers=[{"family": "regime_v1", "probability": 0.7,
                              "training_rows": 100, "model_sha256": "a" * 64}],
            ) == 1
        assert comp.settle(now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT settled_at,outcome_up,model_brier,incumbent_brier
                   FROM rival_model_shadow_forecasts
                   WHERE family='regime_v1'"""
            ).fetchone()
            assert row["settled_at"] == settled.isoformat()
            assert row["outcome_up"] == 1
            assert row["model_brier"] == pytest.approx((0.7 - 1) ** 2)
            assert row["incumbent_brier"] == pytest.approx((0.6 - 1) ** 2)
    finally:
        store.close()


# --- Cycle: kill switch, abstention, gate safety ----------------------------

def _cycle_counts(store):
    with store.connect() as conn:
        shadow = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
            "WHERE family='regime_v1'"
        ).fetchone()["n"]
        incumbent = conn.execute(
            "SELECT COUNT(*) AS n FROM perp_forward_audit "
            "WHERE model_version=?", (FORWARD_MODEL,),
        ).fetchone()["n"]
    return shadow, incumbent


def test_kill_switch_stops_regime_issuance_in_cycle(tmp_path, monkeypatch):
    """Flag on: the cycle issues regime_v1 shadow rows. Flag off: none new.

    Control first: with the flag forced on, the same cycle issues regime
    forecasts; with the flag off -- its kill-switch state -- the same cycle
    issues nothing new for regime_v1, while the incumbent cycle still runs.
    """
    assert forward_audit.ISSUE_REGIME_V1_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime-cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow_on, incumbent_on = _cycle_counts(store)
        assert shadow_on >= 1  # the control proves this cycle can issue regime_v1
        assert incumbent_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _cycle_counts(store) == (shadow_on, incumbent_on)
    finally:
        store.close()


def test_choppy_cycle_abstains_but_incumbent_runs(tmp_path, monkeypatch, caplog):
    """Abstention writes no shadow row; the incumbent still forecasts."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime-choppy.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="choppy")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V1_CHALLENGERS", True)
        with caplog.at_level(logging.INFO):
            monitor.run_once(now=now)
        shadow, incumbent = _cycle_counts(store)
        assert shadow == 0
        assert incumbent >= 1
        match = re.search(r"regime_issued=(\d+) regime_abstained=(\d+)",
                          caplog.text)
        assert match is not None
        assert int(match.group(1)) == 0
        assert int(match.group(2)) >= 1
    finally:
        store.close()


def test_gate_exception_never_blocks_incumbent(tmp_path, monkeypatch):
    """A gate that explodes is an abstention, never a failed incumbent cycle."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime-boom.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")

        def _boom(*args, **kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(forward_audit, "regime_calls", _boom)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow, incumbent = _cycle_counts(store)
        assert shadow == 0
        assert incumbent >= 1
    finally:
        store.close()


# --- selected(): continuity, not a pin ---------------------------------------

def test_selected_continuity_branch(tmp_path):
    """No offline artifact, so no D14 pin: continuity of the live weights.

    The served weights must be the continuation of the promoted ones --
    same feature digest, trained-through and graded count monotone. A
    weight change is accepted only with more graded learning behind it.
    """
    store = _store(tmp_path, name="regime-selected.sqlite")
    try:
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                  "quote": "USDC", "horizon": 5, "now": NOW}
        identity = {
            "feature_digest": FEATURE_DIGEST,
            "weights_sha256": "a" * 64,
            "trained_through": (NOW - timedelta(hours=1)).isoformat(),
            "graded_count": 0,
        }
        # No rows at all: no identity, no serve.
        _promotion_row(store, family="regime_v1", evaluated_at=NOW,
                       artifact=identity)
        assert promoter.selected(**kwargs) is None
        # One graded row under the promoted weights: serves.
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha="a" * 64, family=FAMILY,
                               issued=NOW - timedelta(hours=1),
                               target=NOW - timedelta(hours=1))
        assert artifact_identity(store, "BTC", 5)["graded_count"] == 1
        _promotion_row(store, family="regime_v1", evaluated_at=NOW,
                       artifact={**identity, "graded_count": 1})
        assert promoter.selected(**kwargs) == "regime_v1"
        # A mismatched weights SHA with no further graded learning: not
        # the weights that earned promotion.
        tampered = {**identity, "graded_count": 1, "weights_sha256": "0" * 64}
        _promotion_row(store, family="regime_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # A mismatched feature digest: a different model, not a continuation.
        tampered = {**identity, "graded_count": 1,
                    "feature_digest": "0" * 64}
        _promotion_row(store, family="regime_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="regime_v1",
                       evaluated_at=NOW - timedelta(hours=49),
                       artifact={**identity, "graded_count": 1})
        assert promoter.selected(**kwargs) is None
        # A weight change WITH more graded learning behind it is a
        # continuation (the live learner moved), not a refit: still serves.
        _promotion_row(store, family="regime_v1", evaluated_at=NOW,
                       artifact={**identity, "graded_count": 1})
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha="b" * 64, family=FAMILY,
                               ref=NOW - timedelta(minutes=50),
                               issued=NOW - timedelta(minutes=30),
                               target=NOW - timedelta(minutes=30))
        current = artifact_identity(store, "BTC", 5)
        assert current["weights_sha256"] == "b" * 64
        assert current["graded_count"] == 2
        assert promoter.selected(**kwargs) == "regime_v1"
    finally:
        store.close()


def test_feature_digest_pins_live_model():
    """A MODEL bump deliberately breaks continuity instead of silently
    serving a different decider under the same family name."""
    from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL

    expected = hashlib.sha256(
        f"regime_v1|efficiency-ratio-20|er>0.30|{LIVE_MODEL}|10-features".encode()
    ).hexdigest()
    assert FEATURE_DIGEST == expected


# --- evaluate(): artifact + Route B qualification on the subset --------------

def test_evaluate_records_regime_artifact(tmp_path):
    store = _store(tmp_path, name="regime-eval.sqlite")
    try:
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha="a" * 64, family=FAMILY)
            _insert_shadow_row(conn, p=0.4, y=0, sha="a" * 64, family=FAMILY,
                               ref=NOW - timedelta(minutes=60),
                               issued=NOW - timedelta(minutes=55),
                               target=NOW - timedelta(minutes=55))
        result = FamilyPromotion(store).evaluate(
            asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
            horizon=5, now=NOW)
        artifact = result["families"]["regime_v1"]["artifact"]
        assert artifact is not None
        assert artifact["graded_count"] == 2
        assert artifact["feature_digest"] == FEATURE_DIGEST
        assert artifact["weights_sha256"] == "a" * 64
        # Two graded rows is far below the Route B minimums: held, never
        # promoted on a backtest-sized sample.
        assert result["families"]["regime_v1"]["qualified"] is False  # 2 < 150
        assert result["status"] == "held"
        assert result["selected_family"] is None
    finally:
        store.close()


def test_promotion_qualifies_on_regime_subset(learning_store):
    """Route B on the issued subset: 168 winning targets, full quote
    coverage, after-cost proofs pass -> promoted_paper, and selected()
    serves it (continuity holds: same weights, same count)."""
    now = datetime(2026, 10, 31, 16, tzinfo=UTC)
    family = _seed_promotion(learning_store, now)
    owner = FamilyPromotion(learning_store)
    kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
              "quote": "USDC", "horizon": 5}
    result = owner.evaluate(now=now, **kwargs)
    assert result["selected_family"] == family
    assert result["status"] == "promoted_paper"
    evidence = result["families"][family]
    assert evidence["targets"] == 168
    assert evidence["days"] == 21
    assert evidence["closed_quotes"] == 168
    assert evidence["quote_coverage"] == 1.0
    assert evidence["calibration_status"] == "research_screen_passed"
    assert evidence["cost_proof"]["severe"]["qualified"]
    assert evidence["artifact"]["graded_count"] == 168
    assert owner.selected(now=now, **kwargs) == family
    assert owner.selected(now=now + timedelta(hours=49), **kwargs) is None


# --- D5: selection decomposition ---------------------------------------------

def _report_fixture(tmp_path, name, *, subset_incumbent_p, subset_p, rest_p):
    """5 regime rows on a subset + 15 incumbent rows total in the window.

    The subset targets carry the regime rows (and their incumbent rows);
    the other 10 incumbent rows are the rest of the window, at rest_p.
    """
    store = _store(tmp_path, name=name)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    with store.connect() as conn:
        for i in range(5):
            target = NOW - timedelta(days=1, minutes=5 * i)
            _insert_regime_row(conn, target=target,
                               issued=target - timedelta(minutes=5),
                               p=subset_p, incumbent_p=subset_incumbent_p, y=1)
            _insert_incumbent_row(conn, target=target,
                                  issued=target - timedelta(minutes=5),
                                  p=subset_incumbent_p, y=1)
        for i in range(10):
            target = NOW - timedelta(days=1, minutes=5 * (i + 5))
            _insert_incumbent_row(conn, target=target,
                                  issued=target - timedelta(minutes=5),
                                  p=rest_p, y=1)
    return store, trial_report(store, asset="BTC", horizon=5, now=NOW)


def test_trial_report_decomposition_hard_subset(tmp_path):
    """Regime beats the incumbent on its subset while the incumbent is
    better overall: paired gain positive, selection effect large positive
    (the subset was harder than average for the incumbent).

    Subset: incumbent 0.3025, regime 0.16. Rest of the window: incumbent
    0.0625, so all-target incumbent Brier is 0.1425 -- better than the
    regime's 0.16 on its subset, even though the regime beat the incumbent
    on the subset itself.
    """
    store, report = _report_fixture(
        tmp_path, "regime-report-hard.sqlite",
        subset_incumbent_p=0.45, subset_p=0.6, rest_p=0.75)
    try:
        assert report["issued"] == 5
        assert report["decided"] == 5
        assert report["hits"] == 5
        assert report["windows"] == 5
        assert report["model_brier"] == pytest.approx(0.16)
        assert report["incumbent_brier"] == pytest.approx(0.3025)
        # Paired gain on the subset: regime beat the incumbent there.
        assert report["paired_gain"] == pytest.approx(0.3025 - 0.16)
        # Selection effect: incumbent Brier on the subset (0.3025) minus
        # incumbent Brier on all 15 targets ((5*0.3025 + 10*0.0625)/15).
        all_brier = (5 * 0.3025 + 10 * 0.0625) / 15
        assert all_brier == pytest.approx(0.1425)
        assert report["selection_effect"] == pytest.approx(0.3025 - all_brier)
        assert report["selection_effect"] > 0
        # The incumbent is better overall (0.1425) than the regime's
        # subset Brier (0.16): the win is on the subset, not overall.
        assert report["model_brier"] > all_brier
        assert report["coverage"] == pytest.approx(5 / 15)
    finally:
        store.close()


def test_trial_report_decomposition_easy_subset(tmp_path):
    """The same decomposition with an easy subset must come out negative:
    the subset was easier than average for the incumbent, so a naive
    regime-vs-overall comparison would overstate the edge."""
    store, report = _report_fixture(
        tmp_path, "regime-report-easy.sqlite",
        subset_incumbent_p=0.55, subset_p=0.6, rest_p=0.5)
    try:
        assert report["paired_gain"] == pytest.approx(0.2025 - 0.16)
        assert report["paired_gain"] > 0
        all_brier = (5 * 0.2025 + 10 * 0.25) / 15
        assert report["selection_effect"] == pytest.approx(0.2025 - all_brier)
        assert report["selection_effect"] < 0
    finally:
        store.close()


def test_trial_report_empty(tmp_path):
    store = _store(tmp_path, name="regime-report-empty.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        assert trial_report(store, asset="BTC", horizon=5, now=NOW) == {"issued": 0}
    finally:
        store.close()


# --- regime_summary() family slice -------------------------------------------

def test_regime_summary_family_slice(tmp_path):
    """The dashboard slice over regime_v1's issued subset only: its model
    Brier against the incumbent Brier on the same targets."""
    store = _store(tmp_path, name="regime-summary.sqlite")
    try:
        monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        monitor.initialize()
        with store.connect() as conn:
            for i in range(3):
                target = NOW - timedelta(days=1, minutes=5 * i)
                ref = target - timedelta(minutes=5)
                _insert_regime_row(conn, target=target,
                                   issued=target, p=0.7, incumbent_p=0.6, y=1,
                                   ref=ref)
                conn.execute(
                    """INSERT INTO perp_forward_regimes
                       (venue,contract,quote,horizon_minutes,model_version,
                        reference_opened_at,issued_at,method_version,regime,
                        five_min_volatility_pct,feature_sha256,weights_sha256)
                       VALUES ('hyperliquid','BTC','USDC',5,?,?,?,?,
                               'normal',0.2,?,?)""",
                    (FORWARD_MODEL, ref.isoformat(), target.isoformat(),
                     REGIME_VERSION, "a" * 64, "a" * 64),
                )
        summary = monitor.regime_summary(
            asset="BTC", horizon_minutes=5, family="regime_v1", now=NOW)
        part = summary["regimes"]["normal"]
        assert part["unique_targets"] == 3
        # Regime 0.09 vs incumbent 0.16 on the same targets: gain +0.07.
        assert part["paired_brier_gain"] == pytest.approx(0.07)
        # The incumbent-only slice is untouched by the family parameter.
        incumbent = monitor.regime_summary(
            asset="BTC", horizon_minutes=5, now=NOW)
        assert incumbent["regimes"]["normal"]["unique_targets"] == 0
    finally:
        store.close()


# --- D8: never an OTG live lean ----------------------------------------------

def test_live_lean_none_for_regime():
    """regime_v1 has no read-only recompute path: its live lean is None,
    so it can never become a WATCH/alert/DM/Auto-Hunt candidate or appear
    in the OTG display as a live call."""
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     model_version="regime_v1") is None
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     model_version="regime_v1", contract="BTC",
                     quote="USDC") is None
