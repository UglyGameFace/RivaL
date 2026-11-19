"""Price-first pivot (2026-11-19): directional trial issuance is stopped.

Pins Felix's scope decision at the shipped defaults:
(i) the config defaults stop the three feeding intakes (Model Lab refill,
    scheduled variants, LLM hypotheses) while Track 1 convergence
    collection and the research loop's advancing stay on;
(ii) a forecast cycle at the default issuance switches issues the live
    per-stream logistic and zero shadow-family rows — even with a full
    Model Lab roster open;
(iii) settlement and grading of already-issued forecasts (incumbent and
    shadow alike) keep processing;
(iv) the research-loop driver advances/closes but opens nothing while
    scheduled variants are disabled.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.config import Settings
from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.model_lab import ModelLab
from parlay_bot.research.research_loop_driver import drive_research_loop_tick
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_autoloop_variants import NOW, _FakeDrive, _store
from tests.test_regime_live import _seed_shape_candles

_FLAG_ENV = (
    "RIVAL_LAB_REFILL_ENABLED",
    "RIVAL_SCHEDULED_VARIANTS_ENABLED",
    "RIVAL_LLM_HYPOTHESES_ENABLED",
    "RIVAL_CONVERGENCE_QUOTES_ENABLED",
    "RIVAL_RESEARCH_LOOP_ENABLED",
)


def test_config_defaults_stop_the_intakes_only(monkeypatch):
    for name in _FLAG_ENV:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    # The three intakes stop.
    assert settings.rival_lab_refill_enabled is False
    assert settings.rival_scheduled_variants_enabled is False
    assert settings.rival_llm_hypotheses_enabled is False
    # Track 1 convergence collection stays on, untouched.
    assert settings.rival_convergence_quotes_enabled is True
    # The research loop still advances and closes open experiments.
    assert settings.rival_research_loop_enabled is True


def test_issuance_switches_default_to_stopped():
    for name in (
        "ISSUE_ARCHIVE_V1_CHALLENGERS",
        "ISSUE_GBM_V1_CHALLENGERS",
        "ISSUE_REGIME_V1_CHALLENGERS",
        "ISSUE_REGIME_WILD_V1_CHALLENGERS",
        "ISSUE_VOLCAL_V1_CHALLENGERS",
        "ISSUE_XMARKET_V1_CHALLENGERS",
        "ISSUE_REGIME_LIVE_V1_CHALLENGERS",
        "ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS",
        "ISSUE_CROSS_V1_CHALLENGERS",
        "ISSUE_LAB_CHALLENGERS",
        "ISSUE_CALIBRATED_CHALLENGERS",
        "ISSUE_POOLED_CHALLENGERS",
        "ISSUE_COMPETITION_CHALLENGERS",
    ):
        assert getattr(forward_audit, name) is False, name


def _counts(store):
    with store.connect() as conn:
        shadow = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts").fetchone()["n"]
        incumbent = conn.execute(
            "SELECT COUNT(*) AS n FROM perp_forward_audit WHERE model_version=?",
            (MODEL,)).fetchone()["n"]
    return shadow, incumbent


def test_cycle_at_defaults_issues_incumbent_and_no_shadow_rows(tmp_path):
    # Fixed past stamp (as in test_model_lab's cycle test) so the +5/+10
    # minute cycles stay behind the wall clock the cycle validates against.
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    store = SQLiteHotStore(tmp_path / "pivot-cycle.sqlite")
    market = PerpetualMarketStore(store)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        # A full lab roster is open, so lab challengers WOULD issue here
        # if their switch were on: zero rows proves the gate, not an
        # empty lab.
        market.model_lab = ModelLab(store, refill_enabled=True)
        market.model_lab.review(now)
        assert market.model_lab.active(5)
        monitor = ForwardAccuracyMonitor(market)
        monitor.run_once(now=now)
        monitor.run_once(now=now + timedelta(minutes=5))
        monitor.run_once(now=now + timedelta(minutes=10))
        shadow, incumbent = _counts(store)
        assert incumbent >= 1  # the live logistic is untouched
        assert shadow == 0
    finally:
        store.close()


def test_cycle_still_settles_preexisting_forecasts(tmp_path):
    now = datetime.now(UTC)
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    reference = boundary - timedelta(minutes=65)
    target = boundary - timedelta(minutes=60)
    issued = reference + timedelta(minutes=5)
    store = SQLiteHotStore(tmp_path / "pivot-settle.sqlite")
    market = PerpetualMarketStore(store)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        monitor = ForwardAccuracyMonitor(market)
        monitor.initialize()
        ShadowModelCompetition(store).initialize()
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,target_opened_at,
                     probability_up,baseline_up,training_evaluated,settled_at,outcome_up)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
                 reference.isoformat(), 100.0, issued.isoformat(),
                 target.isoformat(), 0.6, 0.45, 100, None, None),
            )
            conn.execute(
                """INSERT INTO rival_model_shadow_forecasts
                   (venue,contract,quote,asset,horizon_minutes,family,
                    reference_opened_at,issued_at,target_opened_at,
                    probability_up,incumbent_up,baseline_up,
                    training_count,training_sha256,settled_at,outcome_up,
                    model_brier,incumbent_brier,baseline_brier)
                   VALUES ('hyperliquid','BTC','USDC','BTC',5,?,?,?,?,?,?,?,100,?,
                           ?,?,?,?,?)""",
                ("gaussian_nb_v1", reference.isoformat(), issued.isoformat(),
                 target.isoformat(), 0.6, 0.6, 0.45, "a" * 64,
                 None, None, None, None, None),
            )
        monitor.run_once(now=now)
        with store.connect() as conn:
            incumbent = conn.execute(
                "SELECT settled_at, outcome_up FROM perp_forward_audit "
                "WHERE model_version=?", (MODEL,)).fetchall()
            shadow = conn.execute(
                "SELECT settled_at, outcome_up FROM rival_model_shadow_forecasts"
            ).fetchall()
        # The pre-existing pair settled and graded...
        assert any(row["settled_at"] is not None and row["outcome_up"] is not None
                   for row in incumbent)
        assert len(shadow) == 1
        assert shadow[0]["settled_at"] is not None
        assert shadow[0]["outcome_up"] is not None
        # ...and settling issued nothing new for any shadow family.
        assert _counts(store)[0] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_tick_opens_nothing_while_scheduled_variants_disabled(tmp_path):
    store = _store(tmp_path)
    try:
        drive = _FakeDrive()
        out = await drive_research_loop_tick(store, drive=drive, now=NOW)
        assert out["status"] == "nothing_to_advance", out
        with store.connect() as conn:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            opened = 0
            if "research_experiments" in tables:
                opened = conn.execute(
                    "SELECT COUNT(*) AS n FROM research_experiments"
                ).fetchone()["n"]
        assert opened == 0
        # Control: the same tick opens when the flag is explicitly on.
        out = await drive_research_loop_tick(
            store, drive=drive, now=NOW, scheduled_variants_enabled=True)
        assert out["status"] == "opened", out
    finally:
        store.close()
