"""Restarting must reuse identical learned weights without refitting or leakage."""
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals import model_checkpoints as module
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.storage.hot import SQLiteHotStore


def samples(now):
    first = now-timedelta(minutes=500)
    return tuple(LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=first+timedelta(minutes=5*i),
        label_known_at=first+timedelta(minutes=5*i+6),
        features=(.1 if i % 2 else -.1, .2, .01, .03, .05, .2, .1, .1, .1, .1),
        label_up=i % 2,
    ) for i in range(80))


def test_restart_reuses_exact_checkpoint_without_zero_weight_rebuild(tmp_path, monkeypatch):
    hot = SQLiteHotStore(tmp_path / "weights.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        first = module.LiveModelCheckpoints(hot).fit(rows, as_of=now)
        assert first.updates == 80
        restarted = module.LiveModelCheckpoints(hot)
        monkeypatch.setattr(restarted, "_cold_state", lambda *a, **kw: pytest.fail("rebuilt saved weights"))
        restored = restarted.fit(rows, as_of=now+timedelta(seconds=1))
        assert restored == first
        assert restarted.summary()["reused_this_process"] == 1
        assert restarted.summary()["persisted_checkpoints"] == 1
    finally:
        hot.close()


def test_checkpoint_never_reuses_future_feedback_at_an_earlier_clock(tmp_path):
    hot = SQLiteHotStore(tmp_path / "asof.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        checkpoints = module.LiveModelCheckpoints(hot)
        later = checkpoints.fit(rows, as_of=now)
        earlier = checkpoints.fit(rows, as_of=rows[-1].issued_at)
        assert earlier.updates < later.updates
        assert earlier.final_weights != later.final_weights
        with hot.connect() as conn:
            checkpoint = conn.execute("SELECT * FROM rival_live_model_checkpoints").fetchone()
        assert checkpoint["trained_at"] == now.isoformat()
    finally:
        hot.close()


def test_changed_training_data_fits_new_weights_and_keeps_scope(tmp_path):
    from dataclasses import replace

    hot = SQLiteHotStore(tmp_path / "scope.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        checkpoints = module.LiveModelCheckpoints(hot)
        one = checkpoints.fit(rows, as_of=now)
        other = (*rows[:-1], replace(rows[-1], label_up=1-rows[-1].label_up))
        two = checkpoints.fit(other, as_of=now+timedelta(seconds=1))
        assert one.final_weights != two.final_weights
        assert checkpoints.summary()["fitted_this_process"] == 2
        assert checkpoints.summary()["persisted_checkpoints"] == 1
    finally:
        hot.close()
