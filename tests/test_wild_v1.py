"""regime_wild_v1 challenger (audit Step 5, 2026-11-10).

Covers: wild_calls() issuing only when the issuance stamp says wild
("turbulent" from issue_regime(): 5-minute log-return volatility in
pct, features[2], >= 0.35), the "regime_" prefix accepted by
CHALLENGER_PREFIXES and issue() so rows are not silently dropped,
generic settlement copying the incumbent outcome, the selected()
continuity branch (no D14 pin: no offline artifact), evaluate()
recording the artifact and qualifying on the issued subset (Route B:
150 targets + 21 days), the D5 selection decomposition (paired gain +
selection effect + coverage) in trial_report(), the kill switch in the
5-minute cycle (flag on issues, flag off issues nothing new, calm
cycles abstain while the incumbent still runs, a gate exception never
blocks the incumbent), the answer-cards display name, the Performance
page one-liner clock, and no OTG live lean.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app.answer_cards import rename_text
from parlay_bot.discord_app.out_the_gate import live_lean
from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import (
    CHALLENGER_PREFIXES,
    ShadowModelCompetition,
)
from parlay_bot.perpetuals.regime_evidence import issue_regime
from parlay_bot.perpetuals.wild_model import (
    FAMILY,
    FEATURE_DIGEST,
    WILD_REGIME,
    artifact_identity,
    trial_report,
    wild_calls,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_archive_v1_shadow import (
    _insert_shadow_row,
    _promotion_row,
    _store,
)
from tests.test_regime_v1 import (
    _insert_incumbent_row,
    _seed_promotion,
)

NOW = datetime(2026, 11, 7, 12, 0, 30, tzinfo=UTC)


# --- wild_calls(): the stamp decides, nothing else ----------------------------

def test_wild_calls_issues_only_when_stamp_is_turbulent():
    """The existing stamp is the whole gate: turbulent issues, the rest abstain."""
    assert issue_regime((0.0, 0.0, 0.40, 0.0, 0.0)) == "turbulent"
    assert issue_regime((0.0, 0.0, 0.34, 0.0, 0.0)) == "normal"
    assert issue_regime((0.0, 0.0, 0.05, 0.0, 0.0)) == "quiet"
    assert WILD_REGIME == "turbulent"
    calls = wild_calls(
        regime="turbulent", probability=0.62, training_rows=100,
        model_sha256="a" * 64,
    )
    assert calls == [{"family": FAMILY, "probability": 0.62,
                      "training_rows": 100, "model_sha256": "a" * 64}]
    for regime in ("quiet", "normal", "stormy", "", None):
        assert wild_calls(
            regime=regime, probability=0.62, training_rows=100,
            model_sha256="a" * 64,
        ) == []


def test_wild_calls_validates_probability_and_sha():
    """A 0/1 forecast is an abstention, never a call; a bad SHA abstains."""
    for bad_p in (0, 1, 0.0, 1.0, None, float("nan")):
        assert wild_calls(
            regime="turbulent", probability=bad_p, training_rows=100,
            model_sha256="a" * 64,
        ) == []
    for bad_sha in ("a" * 63, "a" * 65, "g" * 64, "A" * 64, None, 1234):
        assert wild_calls(
            regime="turbulent", probability=0.62, training_rows=100,
            model_sha256=bad_sha,
        ) == []


def test_wild_calls_never_raises():
    assert wild_calls(regime=None, probability=None, training_rows=None,
                      model_sha256=None) == []
    assert wild_calls(regime="turbulent", probability="nope",
                      training_rows="nope", model_sha256="a" * 64) == []


# --- Prefix acceptance + kill switch flag ------------------------------------

def test_wild_prefix_accepted_everywhere_it_must_be():
    assert "regime_" in CHALLENGER_PREFIXES
    assert FAMILY.startswith(CHALLENGER_PREFIXES)
    assert FAMILY == "regime_wild_v1"
    assert forward_audit.ISSUE_REGIME_WILD_V1_CHALLENGERS is True


# --- issue() / settle() ------------------------------------------------------

def test_issue_accepts_wild_challenger_rows(tmp_path):
    store = _store(tmp_path, name="wild-issue.sqlite")
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
                challengers=[{"family": "regime_wild_v1", "probability": 0.6,
                              "training_rows": 100, "model_sha256": "a" * 64}],
            )
            assert saved == 1
            row = conn.execute(
                """SELECT family,probability_up,incumbent_up,training_count,
                          training_sha256
                   FROM rival_model_shadow_forecasts
                   WHERE family='regime_wild_v1'"""
            ).fetchone()
            # The wild call IS the incumbent's call: same probability.
            assert row["probability_up"] == 0.6
            assert row["incumbent_up"] == 0.6
            assert row["training_count"] == 100
            assert row["training_sha256"] == "a" * 64
    finally:
        store.close()


def test_wild_rows_settle_from_incumbent_outcome(tmp_path):
    store = _store(tmp_path, name="wild-settle.sqlite")
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
                challengers=[{"family": "regime_wild_v1", "probability": 0.7,
                              "training_rows": 100, "model_sha256": "a" * 64}],
            ) == 1
        assert comp.settle(now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT settled_at,outcome_up,model_brier,incumbent_brier
                   FROM rival_model_shadow_forecasts
                   WHERE family='regime_wild_v1'"""
            ).fetchone()
            assert row["settled_at"] == settled.isoformat()
            assert row["outcome_up"] == 1
            assert row["model_brier"] == pytest.approx((0.7 - 1) ** 2)
            assert row["incumbent_brier"] == pytest.approx((0.6 - 1) ** 2)
    finally:
        store.close()


# --- Cycle: kill switch, abstention, gate safety ----------------------------

def _seed_shape_candles(market, *, now, shape, bars=500):
    """500 live 5m bars on hyperliquid/BTC with a controlled shape.

    "trend" is a straight climb (low volatility: the stamp says quiet,
    so the wild family abstains); "wild" alternates 100/102 (log
    returns alternate about +/-2%, so features[2] -- the 5-minute
    log-return volatility in pct -- is about 1.98, well above the 0.35
    turbulent threshold, so the stamp says wild and the family issues).
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
            elif shape == "wild":
                p = 100 + (i % 2) * 2
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


def _cycle_counts(store):
    with store.connect() as conn:
        shadow = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
            "WHERE family='regime_wild_v1'"
        ).fetchone()["n"]
        incumbent = conn.execute(
            "SELECT COUNT(*) AS n FROM perp_forward_audit "
            "WHERE model_version=?", (FORWARD_MODEL,),
        ).fetchone()["n"]
    return shadow, incumbent


def test_kill_switch_stops_wild_issuance_in_cycle(tmp_path, monkeypatch):
    """Flag on: the cycle issues wild shadow rows. Flag off: none new.

    Control first: with the flag forced on, the same cycle issues wild
    forecasts; with the flag off -- its kill-switch state -- the same
    cycle issues nothing new for regime_wild_v1, while the incumbent
    cycle still runs.
    """
    assert forward_audit.ISSUE_REGIME_WILD_V1_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "wild-cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="wild")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_WILD_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow_on, incumbent_on = _cycle_counts(store)
        assert shadow_on >= 1  # the control proves this cycle can issue wild rows
        assert incumbent_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_WILD_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _cycle_counts(store) == (shadow_on, incumbent_on)
    finally:
        store.close()


def test_calm_cycle_abstains_but_incumbent_runs(tmp_path, monkeypatch, caplog):
    """Abstention writes no shadow row; the incumbent still forecasts."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "wild-calm.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_WILD_V1_CHALLENGERS", True)
        with caplog.at_level(logging.INFO):
            monitor.run_once(now=now)
        shadow, incumbent = _cycle_counts(store)
        assert shadow == 0
        assert incumbent >= 1
        match = re.search(r"wild_issued=(\d+) wild_abstained=(\d+)",
                          caplog.text)
        assert match is not None
        assert int(match.group(1)) == 0
        assert int(match.group(2)) >= 1
    finally:
        store.close()


def test_gate_exception_never_blocks_incumbent(tmp_path, monkeypatch):
    """A gate that explodes is an abstention, never a failed incumbent cycle."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "wild-boom.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="wild")

        def _boom(*args, **kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(forward_audit, "wild_calls", _boom)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_WILD_V1_CHALLENGERS", True)
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
    store = _store(tmp_path, name="wild-selected.sqlite")
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
        _promotion_row(store, family="regime_wild_v1", evaluated_at=NOW,
                       artifact=identity)
        assert promoter.selected(**kwargs) is None
        # One graded row under the promoted weights: serves.
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha="a" * 64, family=FAMILY,
                               issued=NOW - timedelta(hours=1),
                               target=NOW - timedelta(hours=1))
        assert artifact_identity(store, "BTC", 5)["graded_count"] == 1
        _promotion_row(store, family="regime_wild_v1", evaluated_at=NOW,
                       artifact={**identity, "graded_count": 1})
        assert promoter.selected(**kwargs) == "regime_wild_v1"
        # A mismatched weights SHA with no further graded learning: not
        # the weights that earned promotion.
        tampered = {**identity, "graded_count": 1, "weights_sha256": "0" * 64}
        _promotion_row(store, family="regime_wild_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # A mismatched feature digest: a different model, not a continuation.
        tampered = {**identity, "graded_count": 1,
                    "feature_digest": "0" * 64}
        _promotion_row(store, family="regime_wild_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="regime_wild_v1",
                       evaluated_at=NOW - timedelta(hours=49),
                       artifact={**identity, "graded_count": 1})
        assert promoter.selected(**kwargs) is None
        # A weight change WITH more graded learning behind it is a
        # continuation (the live learner moved), not a refit: still serves.
        _promotion_row(store, family="regime_wild_v1", evaluated_at=NOW,
                       artifact={**identity, "graded_count": 1})
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha="b" * 64, family=FAMILY,
                               ref=NOW - timedelta(minutes=50),
                               issued=NOW - timedelta(minutes=30),
                               target=NOW - timedelta(minutes=30))
        current = artifact_identity(store, "BTC", 5)
        assert current["weights_sha256"] == "b" * 64
        assert current["graded_count"] == 2
        assert promoter.selected(**kwargs) == "regime_wild_v1"
    finally:
        store.close()


def test_feature_digest_pins_live_model():
    """A MODEL bump deliberately breaks continuity instead of silently
    serving a different decider under the same family name."""
    expected = hashlib.sha256(
        f"regime_wild_v1|issue-regime-turbulent|vol>=0.35|{FORWARD_MODEL}|10-features".encode()
    ).hexdigest()
    assert FEATURE_DIGEST == expected


# --- evaluate(): artifact + Route B qualification on the subset --------------

def test_evaluate_records_wild_artifact(tmp_path):
    store = _store(tmp_path, name="wild-eval.sqlite")
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
        artifact = result["families"]["regime_wild_v1"]["artifact"]
        assert artifact is not None
        assert artifact["graded_count"] == 2
        assert artifact["feature_digest"] == FEATURE_DIGEST
        assert artifact["weights_sha256"] == "a" * 64
        # Two graded rows is far below the Route B minimums: held, never
        # promoted on a backtest-sized sample.
        assert result["families"]["regime_wild_v1"]["qualified"] is False  # 2 < 150
        assert result["status"] == "held"
        assert result["selected_family"] is None
    finally:
        store.close()


def test_promotion_qualifies_on_wild_subset(learning_store):
    """Route B on the issued subset: 168 winning targets, full quote
    coverage, after-cost proofs pass -> promoted_paper, and selected()
    serves it (continuity holds: same weights, same count)."""
    now = datetime(2026, 10, 31, 16, tzinfo=UTC)
    family = _seed_promotion(learning_store, now, family="regime_wild_v1")
    assert family == "regime_wild_v1"
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

def _insert_wild_row(conn, *, target, issued, p, incumbent_p, baseline_p=0.45,
                     y=1, ref=None, settled=True, sha=None):
    """One settled regime_wild_v1 shadow row with explicit probabilities."""
    ref = ref or (target - timedelta(minutes=5))
    settled_at = (target + timedelta(minutes=10)).isoformat() if settled else None
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts
           (venue,contract,quote,asset,horizon_minutes,family,
            reference_opened_at,issued_at,target_opened_at,
            probability_up,incumbent_up,baseline_up,
            training_count,training_sha256,settled_at,outcome_up,
            model_brier,incumbent_brier,baseline_brier)
           VALUES ('hyperliquid','BTC','USDC','BTC',5,'regime_wild_v1',?,?,?,?,?,?,100,?,
                   ?,?,?,?,?)""",
        (ref.isoformat(), issued.isoformat(), target.isoformat(),
         p, incumbent_p, baseline_p, sha or "a" * 64, settled_at,
         y if settled else None,
         (p - y) ** 2 if settled else None,
         (incumbent_p - y) ** 2 if settled else None,
         (baseline_p - y) ** 2 if settled else None),
    )


def _report_fixture(tmp_path, name, *, subset_incumbent_p, subset_p, rest_p):
    """5 wild rows on a subset + 15 incumbent rows total in the window."""
    store = _store(tmp_path, name=name)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    with store.connect() as conn:
        for i in range(5):
            target = NOW - timedelta(days=1, minutes=5 * i)
            _insert_wild_row(conn, target=target,
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
    """Wild beats the incumbent on its subset while the incumbent is
    better overall: paired gain positive, selection effect large positive
    (the subset was harder than average for the incumbent)."""
    store, report = _report_fixture(
        tmp_path, "wild-report-hard.sqlite",
        subset_incumbent_p=0.45, subset_p=0.6, rest_p=0.75)
    try:
        assert report["issued"] == 5
        assert report["decided"] == 5
        assert report["hits"] == 5
        assert report["windows"] == 5
        assert report["model_brier"] == pytest.approx(0.16)
        assert report["incumbent_brier"] == pytest.approx(0.3025)
        assert report["paired_gain"] == pytest.approx(0.3025 - 0.16)
        all_brier = (5 * 0.3025 + 10 * 0.0625) / 15
        assert all_brier == pytest.approx(0.1425)
        assert report["selection_effect"] == pytest.approx(0.3025 - all_brier)
        assert report["selection_effect"] > 0
        assert report["model_brier"] > all_brier
        assert report["coverage"] == pytest.approx(5 / 15)
    finally:
        store.close()


def test_trial_report_empty(tmp_path):
    store = _store(tmp_path, name="wild-report-empty.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        assert trial_report(store, asset="BTC", horizon=5, now=NOW) == {"issued": 0}
    finally:
        store.close()


# --- Display: name table, one-liner clock, empty record ----------------------

def test_display_name_is_wild_only_test():
    """The Step 2 name table gives the family one display name, and
    renaming is idempotent: already-renamed text passes through."""
    assert rename_text("regime_wild_v1") == "wild-only test"
    assert rename_text("wild-only test") == "wild-only test"
    # The bare family id inside a longer label renames the same way.
    assert rename_text("regime_wild_v1: 3 predictions") == (
        "wild-only test: 3 predictions")


def test_one_liner_clock_and_length():
    """The Performance-page headline carries the Route B clock (day/21,
    targets/150) and stays under the 140-char pin even at the extreme."""
    record = {"issued": 3, "graded": 0, "decided": 0, "hits": 0, "windows": 0,
              "model_brier": None, "incumbent_brier": None,
              "baseline_brier": None,
              "first_issued": (NOW - timedelta(days=1)).isoformat()}
    text = home_module._trial_one_liner("wild-only test", record, NOW)
    assert text.startswith("**wild-only test:** 3 predictions made")
    assert "· day 1/21 · 0/150 targets" in text
    extreme = {"issued": 77760, "graded": 77760, "decided": 77760,
               "hits": 77760, "windows": 8640, "model_brier": 0.001,
               "incumbent_brier": 0.999, "baseline_brier": 0.999,
               "first_issued": (NOW - timedelta(days=30)).isoformat()}
    long_text = home_module._trial_one_liner("wild-only test", extreme, NOW)
    assert "\n" not in long_text
    assert len(long_text) < 140, (len(long_text), long_text)


def test_wild_record_empty_store(tmp_path):
    """No wild rows yet: the Performance reader reports issued 0, so no
    field renders (Discord rejects empty field values)."""
    store = SQLiteHotStore(tmp_path / "wild-empty.sqlite")
    try:
        market = SimpleNamespace(store=store)
        assert home_module._wild_record(market, "BTC", 5) == {"issued": 0}
    finally:
        store.close()


# --- Never an OTG live lean ---------------------------------------------------

def test_live_lean_none_for_wild():
    """regime_wild_v1 has no read-only recompute path: its live lean is
    None, so it can never become a WATCH/alert/DM/Auto-Hunt candidate
    or appear in the OTG display as a live call."""
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     model_version="regime_wild_v1") is None
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     model_version="regime_wild_v1", contract="BTC",
                     quote="USDC") is None
