"""Automatic early stop for test models that are clearly losing.

Promotion needs 150 targets and 21 days. A losing test model used to keep
issuing until someone flipped its switch by hand. The predeclared rule in
perpetuals/early_stop.py stops it per stream, keeps every row it already
issued, and never touches the main model or promotion.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.config import Settings
from parlay_bot.discord_app.bot import RivalDiscordClient
from parlay_bot.perpetuals import early_stop
from parlay_bot.perpetuals.early_stop import EarlyStop, verdict
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
STREAM = ("hyperliquid", "BTC", "USDC")


def _score(**changes) -> dict:
    """A clear loser on a 5-minute stream: 0.41 vs 0.25 over 200 targets."""
    base = {"venue": "hyperliquid", "contract": "BTC", "quote": "USDC",
            "horizon_minutes": 5, "family": "liq_v1", "n": 200, "days": 2,
            "model_score": 0.41, "incumbent_score": 0.25,
            "loss_square": 0.1856, "last_issued": NOW.isoformat()}
    return {**base, **changes}


# --- The rule ----------------------------------------------------------------

def test_a_clear_loser_is_stopped_with_its_evidence():
    evidence = verdict(_score())
    assert evidence is not None
    assert evidence["targets"] == 200 and evidence["days"] == 2
    assert evidence["loss"] == pytest.approx(0.16)
    assert 0 < evidence["loss_lower"] < evidence["loss"]


@pytest.mark.parametrize("changes", [
    {"n": early_stop.MIN_TARGETS - 1},           # too few graded targets
    {"days": 1},                                  # one day is one mood
    {"model_score": 0.25},                        # level with the main model
    {"model_score": 0.20, "loss_square": 0.01},   # better than the main model
    {"model_score": 0.255, "loss_square": 0.0001},  # worse, but under the margin
    {"model_score": 0.27, "loss_square": 0.16},   # worse, but inside the noise
    {"horizon_minutes": 60},                      # overlapping targets: not yet
    {"model_score": float("nan")},
    {"loss_square": None},
    {"n": "many"},
])
def test_everything_short_of_clearly_worse_keeps_running(changes):
    assert verdict(_score(**changes)) is None


def test_a_family_that_reuses_the_main_models_call_can_never_be_stopped():
    # regime_v1 / regime_wild_v1: identical calls, so the paired loss is zero.
    same = _score(family="regime_wild_v1", n=5000, days=7,
                  model_score=0.26, incumbent_score=0.26, loss_square=0.0)
    assert verdict(same) is None


def test_sixty_minute_targets_need_more_evidence_than_five_minute_ones():
    # Same score, twelve times the overlap: stopped only once n is large.
    assert verdict(_score(horizon_minutes=60, n=200)) is None
    assert verdict(_score(horizon_minutes=60, n=2000, days=7)) is not None


# --- End to end on the ledger ------------------------------------------------

def _store(tmp_path, name="early-stop.sqlite") -> SQLiteHotStore:
    store = SQLiteHotStore(tmp_path / name)
    store.initialize()
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    return store


def _graded(conn, *, family, probability, horizon=5, venue="hyperliquid",
            rows=200, newest=NOW - timedelta(minutes=5)):
    """Graded shadow rows, outcomes alternating, main model always at 50%."""
    for i in range(rows):
        at = (newest - timedelta(minutes=5 * i)).isoformat()
        outcome = i % 2
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                 horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                 probability_up,incumbent_up,baseline_up,training_count,training_sha256,
                 settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, "BTC", "USDC", "BTC", horizon, family, at, at, at,
             probability, .5, .5, 10, "0" * 64, at, outcome,
             (probability - outcome) ** 2, .25, .25))


def _issue(store, *, venue="hyperliquid", horizon=5, families=("liq_v1", "gbm_v1")):
    with store.connect() as conn:
        return ShadowModelCompetition(store).issue(
            conn, samples=[], features=(0.25,) * 10, now=NOW,
            venue=venue, contract="BTC", quote="USDC", asset="BTC", horizon=horizon,
            reference_opened_at=NOW,
            target_opened_at=NOW + timedelta(minutes=5), issued_at=NOW,
            incumbent_up=0.6, baseline_up=0.5,
            challengers=[{"family": family, "probability": 0.7,
                          "training_rows": 100, "model_sha256": "a" * 64}
                         for family in families],
        )


def _families(store, *, reference=NOW):
    with store.connect() as conn:
        return {(row["venue"], row["horizon_minutes"], row["family"])
                for row in conn.execute(
                    """SELECT venue,horizon_minutes,family
                       FROM rival_model_shadow_forecasts
                       WHERE reference_opened_at=?""", (reference.isoformat(),))}


def test_a_losing_model_stops_on_its_stream_only_and_keeps_its_history(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            _graded(conn, family="liq_v1", probability=.9)             # 0.41 vs 0.25
            _graded(conn, family="gbm_v1", probability=.5)             # level
            _graded(conn, family="liq_v1", probability=.5, venue="okx")  # fine on OKX
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts").fetchone()["n"]
        health = RuntimeHealthMonitor(store)
        health.refresh_regressions(now=NOW)
        scores, in_use_since = health.paired_scores()
        new = EarlyStop(store).record(scores, now=NOW, in_use_since=in_use_since)

        assert [(s["venue"], s["horizon_minutes"], s["family"]) for s in new] == [
            ("hyperliquid", 5, "liq_v1")]
        stop = EarlyStop(store).stops()[0]
        assert stop["rule_version"] == early_stop.RULE_VERSION
        assert stop["targets"] == 200 and stop["stopped_at"] == NOW.isoformat()
        assert stop["model_brier"] == pytest.approx(0.41)
        # Recording again is not a second stop.
        assert EarlyStop(store).record(scores, now=NOW, in_use_since=in_use_since) == []

        # The stopped model issues nothing new on that stream; the other
        # family there still does, and so does liq_v1 on OKX.
        assert _issue(store) == 1
        assert _issue(store, venue="okx") == 2
        assert _families(store) == {
            ("hyperliquid", 5, "gbm_v1"), ("okx", 5, "gbm_v1"), ("okx", 5, "liq_v1")}
        # Nothing it had already issued was removed.
        with store.connect() as conn:
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts").fetchone()["n"]
        assert after == before + 3
    finally:
        store.close()


def test_a_retired_family_needs_no_stop(tmp_path):
    store = _store(tmp_path, "retired.sqlite")
    try:
        with store.connect() as conn:  # same loser, last issued 3 hours ago
            _graded(conn, family="flow_v3", probability=.9,
                    newest=NOW - timedelta(hours=3))
        health = RuntimeHealthMonitor(store)
        assert health.refresh_regressions(now=NOW) == {}
        scores, in_use_since = health.paired_scores()
        assert EarlyStop(store).record(scores, now=NOW, in_use_since=in_use_since) == []
    finally:
        store.close()


def test_the_regression_alert_still_pools_a_familys_horizons(tmp_path):
    """The scan now groups by horizon; the alert must read exactly as before."""
    store = _store(tmp_path, "alert.sqlite")
    try:
        with store.connect() as conn:  # 30 + 30 rows: only the pool reaches 50
            _graded(conn, family="liq_v1", probability=.9, horizon=5, rows=30)
            _graded(conn, family="liq_v1", probability=.9, horizon=15, rows=30)
        health = RuntimeHealthMonitor(store)
        assert health.refresh_regressions(now=NOW) == {
            "regression:hyperliquid:BTC:liq_v1":
                "paired Brier 0.4100 vs incumbent 0.2500, n=60; promotion must stay gated"}
        # Too few targets per horizon for a stop: alerted, still running.
        scores, in_use_since = health.paired_scores()
        assert EarlyStop(store).record(scores, now=NOW, in_use_since=in_use_since) == []
    finally:
        store.close()


def test_the_kill_switch_turns_the_rule_off_without_erasing_the_record(tmp_path, monkeypatch):
    store = _store(tmp_path, "switch.sqlite")
    try:
        assert len(EarlyStop(store).record([_score()], now=NOW)) == 1
        monkeypatch.setattr(early_stop, "ENABLED", False)
        assert EarlyStop(store).record([_score(family="gbm_v1")], now=NOW) == []
        assert _issue(store) == 2            # the stopped family issues again
        assert len(EarlyStop(store).stops()) == 1  # and its record is still there
    finally:
        store.close()


def test_a_missing_stop_table_never_costs_the_other_families_their_rows(tmp_path):
    store = _store(tmp_path, "fail-open.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("DROP TABLE rival_family_early_stop")
        assert _issue(store) == 2
    finally:
        store.close()


def test_the_watchdog_side_task_records_stops_from_its_one_scan(tmp_path):
    client = RivalDiscordClient(Settings(
        _env_file=None, rival_storage_backend="sqlite",
        rival_db_path=str(tmp_path / "rival.sqlite"), discord_token=None,
        rival_llm_background_opt_in=False,
    ))
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(client.store)).initialize()
        with client.store.connect() as conn:
            _graded(conn, family="liq_v1", probability=.9)
        client._score_test_models(NOW)
        assert [(s["venue"], s["family"]) for s in EarlyStop(client.store).stops()] == [
            ("hyperliquid", "liq_v1")]
    finally:
        client.store.close()
