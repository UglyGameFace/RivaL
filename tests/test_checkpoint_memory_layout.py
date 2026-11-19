"""Memory layout of LiveModelCheckpoints' in-process caches (Nov 18 fix).

The checkpoint cache holds the persisted JSON text instead of a parsed
state copy, and the correction-fingerprint memo is shared process-wide,
keyed by sample scope + target contract, with each entry packed into one
int. Both are pure representation changes: these tests pin that fit
results are identical to the uncached/uncrossed paths and that the
layouts actually have the claimed shape.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals import model_checkpoints as module
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.fixture(autouse=True)
def _isolate_shared_memo():
    module._FINGERPRINT_MEMO.clear()
    yield
    module._FINGERPRINT_MEMO.clear()


def samples(now, venue):
    first = now - timedelta(minutes=500)
    return tuple(LabeledExample(
        venue=venue, contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=first + timedelta(minutes=5 * i),
        label_known_at=first + timedelta(minutes=5 * i + 6),
        features=(.1 if i % 2 else -.1, .2, .01, .03, .05, .2, .1, .1, .1, .1),
        label_up=i % 2,
    ) for i in range(80))


def test_pack_fingerprint_round_trips_content_and_digest():
    for content, digest in ((0, "0" * 16), (-1, "00ab34cd56ef7801"),
                            (2**62 + 7, "ffffffffffffffff"),
                            (-(2**62), "0123456789abcdef")):
        packed = module._pack_fingerprint(content, digest)
        assert packed & module._CONTENT_MASK == content & module._CONTENT_MASK
        assert format(packed >> 64, "016x") == digest


def test_checkpoint_cache_holds_persisted_json_text(tmp_path):
    hot = SQLiteHotStore(tmp_path / "layout.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now, "layoutvenue")
        model = module.LiveModelCheckpoints(hot)
        first = model.fit(rows, as_of=now)
        assert len(model._checkpoint_cache) == 1
        _cached_row, cached_state = next(iter(model._checkpoint_cache.values()))
        assert isinstance(cached_state, str)
        with hot.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_live_model_checkpoints").fetchone()
        assert cached_state == row["state_json"]
        assert json.loads(cached_state)["weights"] == list(first.final_weights)
        # A cache-hit fit must agree with a from-database restore.
        extra = samples(now + timedelta(minutes=5), "layoutvenue")
        via_cache = model.fit(extra, as_of=now + timedelta(minutes=5))
        control = module.LiveModelCheckpoints(hot)
        via_restore = control.fit(extra, as_of=now + timedelta(minutes=5))
        assert via_cache == via_restore
        assert model.summary()["verified_checkpoint_cache_hits"] >= 1
    finally:
        hot.close()


def test_fingerprint_memo_is_shared_across_feature_contracts(tmp_path):
    hot = SQLiteHotStore(tmp_path / "shared.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now, "sharedvenue")
        incumbent = module.LiveModelCheckpoints(hot)
        family = module.LiveModelCheckpoints(
            hot, feature_contract="some-other-contract-v1", learning_rate=0.05)
        one = incumbent.fit(rows, as_of=now)
        two = family.fit(rows, as_of=now)
        # One partition for the stream despite two instances/contracts.
        assert len(module._FINGERPRINT_MEMO) == 1
        partition = next(iter(module._FINGERPRINT_MEMO.values()))
        assert len(partition) == 80
        assert all(isinstance(v, int) for v in partition.values())
        # Sharing changes no result: a memo-free control agrees exactly.
        module._FINGERPRINT_MEMO.clear()
        control = module.LiveModelCheckpoints(
            hot, feature_contract="some-other-contract-v1", learning_rate=0.05)
        assert control.fit(rows, as_of=now) == two
        assert one.final_weights is not None and two.final_weights is not None
    finally:
        hot.close()


def test_shared_memo_still_detects_a_corrected_label(tmp_path):
    from dataclasses import replace

    hot = SQLiteHotStore(tmp_path / "correction.sqlite")
    try:
        now = datetime.now(UTC)
        rows = samples(now, "correctvenue")
        incumbent = module.LiveModelCheckpoints(hot)
        family = module.LiveModelCheckpoints(
            hot, feature_contract="some-other-contract-v1", learning_rate=0.05)
        incumbent.fit(rows, as_of=now)
        family.fit(rows, as_of=now)
        corrected = (*rows[:-1], replace(rows[-1], label_up=1 - rows[-1].label_up))
        changed = family.fit(corrected, as_of=now + timedelta(seconds=1))
        module._FINGERPRINT_MEMO.clear()
        control = module.LiveModelCheckpoints(
            hot, feature_contract="some-other-contract-v1", learning_rate=0.05)
        assert control.fit(corrected, as_of=now + timedelta(seconds=1)) == changed
    finally:
        hot.close()
