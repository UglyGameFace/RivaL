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


def test_identical_cycle_reuses_verified_checkpoint_without_rewriting(tmp_path):
    """A 5-minute cycle with unchanged matured labels needs just one tiny read."""
    hot = SQLiteHotStore(tmp_path / "checkpoint_cache.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        model = module.LiveModelCheckpoints(hot)
        first = model.fit(rows, as_of=now)
        with hot.connect() as conn:
            saved = conn.execute(
                "SELECT training_digest,trained_at FROM rival_live_model_checkpoints"
            ).fetchone()
            original = (saved["training_digest"], saved["trained_at"])
        calls = []
        real = hot.connect

        def counted():
            calls.append(1)
            return real()

        hot.connect = counted
        repeated = model.fit(rows, as_of=now + timedelta(minutes=1))
        assert repeated == first
        assert len(calls) == 1  # digest check, not a second write checkout
        assert model._cache_hits == 1
        with hot.connect() as conn:
            current = conn.execute(
                "SELECT training_digest,trained_at FROM rival_live_model_checkpoints"
            ).fetchone()
        assert (current["training_digest"], current["trained_at"]) == original
    finally:
        hot.close()


def test_cache_revalidates_another_writers_source_correction(tmp_path):
    """Digest mismatch must reload and correct weights, never use stale cache."""
    from dataclasses import replace

    hot = SQLiteHotStore(tmp_path / "external_writer.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        primary = module.LiveModelCheckpoints(hot)
        original = primary.fit(rows, as_of=now)

        changed = (*rows[:-1], replace(rows[-1], label_up=1-rows[-1].label_up))
        other_writer = module.LiveModelCheckpoints(hot)
        updated = other_writer.fit(changed, as_of=now + timedelta(seconds=1))
        assert updated.final_weights != original.final_weights

        restored = primary.fit(rows, as_of=now + timedelta(seconds=2))
        assert restored == original  # historical correction triggers rebuild
        assert primary._cache_hits == 0
        assert module.LiveModelCheckpoints(hot).fit(
            rows, as_of=now + timedelta(seconds=3)
        ) == original
    finally:
        hot.close()


def test_newly_matured_label_still_persists_and_survives_restart(tmp_path):
    """Optimization must not skip genuine incremental training."""
    from dataclasses import replace

    hot = SQLiteHotStore(tmp_path / "new_label.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        learner = module.LiveModelCheckpoints(hot)
        first = learner.fit(rows, as_of=now)
        newest = replace(
            rows[-1], issued_at=rows[-1].issued_at + timedelta(minutes=5),
            label_known_at=rows[-1].label_known_at + timedelta(minutes=5),
            label_up=1 - rows[-1].label_up,
        )
        expanded = (*rows, newest)
        later = learner.fit(expanded, as_of=now + timedelta(minutes=1))
        assert later.updates == first.updates + 1
        assert later.final_weights != first.final_weights
        assert module.LiveModelCheckpoints(hot).fit(
            expanded, as_of=now + timedelta(minutes=2)
        ) == later
    finally:
        hot.close()


def test_concurrent_checkpoint_update_never_issues_unpersisted_weights(tmp_path):
    """A commit race must fail closed, not return weights the database lacks."""
    from dataclasses import replace

    hot = SQLiteHotStore(tmp_path / "write_race.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        primary = module.LiveModelCheckpoints(hot)
        primary.fit(rows, as_of=now)
        changed = (*rows[:-1], replace(rows[-1], label_up=1 - rows[-1].label_up))
        competitor = module.LiveModelCheckpoints(hot)

        next_sample = replace(
            rows[-1], issued_at=rows[-1].issued_at + timedelta(minutes=5),
            label_known_at=rows[-1].label_known_at + timedelta(minutes=5),
        )
        expanded = (*rows, next_sample)
        original_connect = hot.connect
        calls = [0]

        def race_connect():
            calls[0] += 1
            if calls[0] == 2:
                # Between the primary digest probe and its conditional write,
                # simulate a second process committing a corrected source row.
                hot.connect = original_connect
                try:
                    competitor.fit(changed, as_of=now + timedelta(seconds=30))
                finally:
                    hot.connect = race_connect
            return original_connect()

        hot.connect = race_connect
        with pytest.raises(RuntimeError, match="checkpoint changed during fit"):
            primary.fit(expanded, as_of=now + timedelta(minutes=1))
        hot.connect = original_connect
        # The losing worker can retry on the next cycle from the real row.
        expected = module.LiveModelCheckpoints(hot).fit(
            expanded, as_of=now + timedelta(minutes=2)
        )
        recovered = primary.fit(expanded, as_of=now + timedelta(minutes=3))
        assert recovered == expected
    finally:
        hot.close()


def test_cache_adopts_external_metadata_even_if_weights_are_identical(tmp_path):
    """A harmless remote trained_at refresh should not force endless full reads."""
    hot = SQLiteHotStore(tmp_path / "cache_refresh.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        learner = module.LiveModelCheckpoints(hot)
        first = learner.fit(rows, as_of=now)
        with hot.connect() as conn:
            conn.execute(
                "UPDATE rival_live_model_checkpoints SET trained_at=?",
                ((now + timedelta(seconds=1)).isoformat(),),
            )
        assert learner.fit(rows, as_of=now + timedelta(seconds=2)) == first
        assert learner._cache_hits == 0  # full read of external metadata
        assert learner.fit(rows, as_of=now + timedelta(seconds=3)) == first
        assert learner._cache_hits == 1  # subsequent digest probe is enough
    finally:
        hot.close()


def test_checkpoint_saved_with_full_digests_is_reused_not_cold_restarted(tmp_path, monkeypatch):
    """Checkpoints written before shortened fingerprints keep their learned weights."""
    import hashlib
    import json

    hot = SQLiteHotStore(tmp_path / "legacy_digests.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        first = module.LiveModelCheckpoints(hot).fit(rows, as_of=now)
        # Rewrite the saved state the way the previous code stored it: full
        # 64-hex SHA-256 fingerprints, with digests that still verify.
        with hot.connect() as conn:
            saved = conn.execute("SELECT * FROM rival_live_model_checkpoints").fetchone()
            state = json.loads(saved["state_json"])
            state["recent"] = {
                issued: hashlib.sha256(module.canonical([
                    r.label_known_at.isoformat(), r.features, r.label_up,
                ]).encode()).hexdigest()
                for r in rows for issued in [r.issued_at.isoformat()]
            }
            encoded = module.canonical(state)
            conn.execute(
                "UPDATE rival_live_model_checkpoints SET state_json=?, training_digest=?",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest()),
            )
        restarted = module.LiveModelCheckpoints(hot)
        monkeypatch.setattr(restarted, "_cold_state",
                            lambda *a, **kw: pytest.fail("legacy checkpoint was discarded"))
        restored = restarted.fit(rows, as_of=now + timedelta(seconds=1))
        assert restored.final_weights == first.final_weights
        with hot.connect() as conn:
            after = json.loads(conn.execute(
                "SELECT state_json FROM rival_live_model_checkpoints").fetchone()["state_json"])
        assert {len(v) for v in after["recent"].values()} == {module.FINGERPRINT_HEX}
    finally:
        hot.close()


def test_legacy_full_digest_still_detects_a_source_correction(tmp_path):
    from dataclasses import replace

    saved_full = "ab" * 32
    assert module._same_fingerprint(saved_full, saved_full[:module.FINGERPRINT_HEX])
    assert not module._same_fingerprint(saved_full, "cd" * (module.FINGERPRINT_HEX // 2))
    hot = SQLiteHotStore(tmp_path / "correction.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now)
        model = module.LiveModelCheckpoints(hot)
        original = model.fit(rows, as_of=now)
        corrected = (*rows[:-1], replace(rows[-1], features=(0.3,) * 10))
        rebuilt = model.fit(corrected, as_of=now + timedelta(seconds=1))
        # A changed input for an issuance already trained on forces a rebuild.
        assert rebuilt.updates == original.updates
        assert rebuilt.final_weights != original.final_weights
    finally:
        hot.close()
