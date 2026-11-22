"""Slice 1: materialized scorecard reads -- storage, codec, worker, canary.

The worker computes the Performance scorecards with the bot's own
uncached aggregates and stores them as canonical JSON; these tests pin
the codec's exact round-trip (datetimes and int keys included), the
freshness/fallback contract, the 40-key enumeration, shadow mode, and
the canary's byte comparison.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals import materialized_scorecards as scorecards
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS
from parlay_bot.storage import materialized_reads as mr
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

SUMMARY_SHAPED = {
    "asset": "BTC", "as_of": NOW, "model": "m",
    "last_7d": {5: {"issued": 3, "brier": 0.21}, 15: {"issued": 0, "brier": None}},
    "prior_7d": {60: {"issued": 1, "hit_rate": 0.5}},
    "pair": (1, "x"), "flag": True, "missing": None,
}


@pytest.fixture(autouse=True)
def _flag_cache():
    mr.reset_reads_enabled_cache()
    yield
    mr.reset_reads_enabled_cache()


def test_codec_round_trips_types_exactly():
    text = mr.canonical_json(SUMMARY_SHAPED)
    assert mr.decode_payload(text) == SUMMARY_SHAPED
    decoded = mr.decode_payload(text)
    assert isinstance(decoded["as_of"], datetime)
    assert set(decoded["last_7d"]) == {5, 15}
    assert isinstance(decoded["pair"], tuple)


def test_canonical_json_is_deterministic_and_sensitive():
    shuffled = dict(reversed(list(SUMMARY_SHAPED.items())))
    assert mr.canonical_json(shuffled) == mr.canonical_json(SUMMARY_SHAPED)
    changed = {**SUMMARY_SHAPED, "model": "m2"}
    assert mr.canonical_json(changed) != mr.canonical_json(SUMMARY_SHAPED)


def test_upsert_and_fresh_read_round_trip(learning_store):
    reads = mr.MaterializedReads(learning_store)
    reads.upsert(mr.KIND_FORWARD_SUMMARY, "BTC", 0, SUMMARY_SHAPED,
                 computed_at=NOW)
    row = reads.read_row(mr.KIND_FORWARD_SUMMARY, "BTC", 0)
    assert row is not None and row[0] == NOW
    assert reads.read_fresh(mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=NOW) \
        == SUMMARY_SHAPED
    # Upsert replaces in place: still one row, new payload and stamp.
    later = NOW + timedelta(minutes=5)
    updated = {**SUMMARY_SHAPED, "model": "m2"}
    reads.upsert(mr.KIND_FORWARD_SUMMARY, "BTC", 0, updated, computed_at=later)
    assert reads.read_row(mr.KIND_FORWARD_SUMMARY, "BTC", 0)[0] == later
    assert reads.read_fresh(mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=later) \
        == updated


def test_read_fresh_rejects_stale_missing_and_corrupt(tmp_path):
    store = SQLiteHotStore(tmp_path / "m.sqlite")
    try:
        reads = mr.MaterializedReads(store)
        assert reads.read_fresh(mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=NOW) \
            is None
        reads.upsert(mr.KIND_FORWARD_SUMMARY, "BTC", 0, SUMMARY_SHAPED,
                     computed_at=NOW)
        stale_at = NOW + timedelta(seconds=mr.FRESHNESS_SECONDS + 1)
        assert reads.read_fresh(mr.KIND_FORWARD_SUMMARY, "BTC", 0,
                                now=stale_at) is None
        with store.connect() as conn:
            conn.execute(
                "UPDATE rival_materialized_reads SET payload_json='not json'")
        assert reads.read_fresh(mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=NOW) \
            is None
    finally:
        store.close()


def test_read_on_missing_table_is_a_miss_not_an_error(tmp_path):
    store = SQLiteHotStore(tmp_path / "bare.sqlite")
    try:
        reads = mr.MaterializedReads(store)
        assert reads.read_row(mr.KIND_FORWARD_SUMMARY, "BTC", 0) is None
        assert reads.read_fresh(mr.KIND_FORWARD_SUMMARY, "BTC", 0) is None
    finally:
        store.close()


def test_reads_enabled_defaults_off_and_honors_env(monkeypatch):
    monkeypatch.delenv("RIVAL_MATERIALIZED_READS", raising=False)
    mr.reset_reads_enabled_cache()
    assert mr.reads_enabled() is False
    monkeypatch.setenv("RIVAL_MATERIALIZED_READS", "true")
    mr.reset_reads_enabled_cache()
    assert mr.reads_enabled() is True


def test_lookup_if_enabled_matrix(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "m.sqlite")
    try:
        reads = mr.materialized_reads(store)
        reads.upsert(mr.KIND_FORWARD_SUMMARY, "BTC", 0, SUMMARY_SHAPED,
                     computed_at=NOW)
        # Flag off: the fresh row is invisible.
        monkeypatch.delenv("RIVAL_MATERIALIZED_READS", raising=False)
        mr.reset_reads_enabled_cache()
        assert mr.lookup_if_enabled(
            store, mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=NOW) is None
        # Flag on: fresh hit, stale miss, missing-key miss.
        monkeypatch.setenv("RIVAL_MATERIALIZED_READS", "true")
        mr.reset_reads_enabled_cache()
        assert mr.lookup_if_enabled(
            store, mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=NOW) == SUMMARY_SHAPED
        stale_at = NOW + timedelta(seconds=mr.FRESHNESS_SECONDS + 1)
        assert mr.lookup_if_enabled(
            store, mr.KIND_FORWARD_SUMMARY, "BTC", 0, now=stale_at) is None
        assert mr.lookup_if_enabled(
            store, mr.KIND_FORWARD_SUMMARY, "ETH", 0, now=NOW) is None
    finally:
        store.close()


def test_scorecard_keys_are_complete_and_unique():
    keys = scorecards.scorecard_keys()
    assert len(keys) == 40 and len(set(keys)) == 40
    kinds = {kind for kind, _, _ in keys}
    assert kinds == {mr.KIND_FORWARD_SUMMARY, mr.KIND_FORWARD_CALIBRATION,
                     mr.KIND_ACTIVE_LEARNING, mr.KIND_POLYMARKET_SUMMARY}
    for asset in ASSETS:
        assert (mr.KIND_FORWARD_SUMMARY, asset, 0) in keys
        for horizon in HORIZONS:
            for kind in kinds - {mr.KIND_FORWARD_SUMMARY}:
                assert (kind, asset, horizon) in keys


def _bare_store(tmp_path):
    return SQLiteHotStore(tmp_path / "cycle.sqlite")


def test_cycle_writes_every_key_and_shadow_stays_shadow(tmp_path):
    store = _bare_store(tmp_path)
    try:
        report = scorecards.run_scorecard_cycle(store, now=NOW, shadow=True)
        assert report["failed"] == {}
        assert len(report["written"]) == 40
        reads = mr.materialized_reads(store)
        for kind, asset, horizon in scorecards.scorecard_keys():
            assert reads.read_row(mr.shadow_kind(kind), asset, horizon) \
                is not None
            assert reads.read_row(kind, asset, horizon) is None
        live = scorecards.run_scorecard_cycle(store, now=NOW, shadow=False)
        assert live["failed"] == {}
        for kind, asset, horizon in scorecards.scorecard_keys():
            assert reads.read_row(kind, asset, horizon) is not None
    finally:
        store.close()


def test_cycle_isolates_a_failing_key(tmp_path, monkeypatch):
    store = _bare_store(tmp_path)
    try:
        computer = scorecards.ScorecardComputer(store)
        original = computer.compute

        def flaky(kind, asset, horizon, *, now):
            if kind == mr.KIND_FORWARD_CALIBRATION and asset == "ETH":
                raise RuntimeError("boom")
            return original(kind, asset, horizon, now=now)

        monkeypatch.setattr(computer, "compute", flaky)
        report = scorecards.run_scorecard_cycle(
            store, now=NOW, shadow=True, computer=computer)
        assert len(report["written"]) == 37
        assert set(report["failed"]) == {
            f"{mr.KIND_FORWARD_CALIBRATION}:ETH:{h}" for h in HORIZONS}
    finally:
        store.close()


def test_polymarket_payload_merges_pooled_and_survives_its_failure(
        tmp_path, monkeypatch):
    store = _bare_store(tmp_path)
    try:
        computer = scorecards.ScorecardComputer(store)
        payload = scorecards.polymarket_status_payload(
            computer.learner, asset="BTC", horizon=15)
        assert "pooled" in payload
        assert payload["settled_forward_contracts"] == 0

        def broken(*, asset, horizon, days=7, edge=0.05):
            raise RuntimeError("pooled down")

        monkeypatch.setattr(computer.learner, "pooled_summary", broken)
        degraded = scorecards.polymarket_status_payload(
            computer.learner, asset="BTC", horizon=15)
        assert "pooled" not in degraded
        assert degraded["settled_forward_contracts"] == 0
    finally:
        store.close()


def test_canary_entry_statuses():
    payload = {"a": 1, "b": [1.5, None]}
    stored = mr.canonical_json(payload)
    assert scorecards.canary_entry(stored, payload)["status"] == "MATCH"
    flipped = stored[:-2] + "9}" if stored.endswith("}") else stored + " "
    assert scorecards.canary_entry(flipped, payload)["status"] == "DIFF"
    assert scorecards.canary_entry(None, payload)["status"] == "MISSING"


def test_canary_report_matches_a_shadow_cycle(tmp_path):
    store = _bare_store(tmp_path)
    try:
        scorecards.run_scorecard_cycle(store, now=NOW, shadow=True)
        report = scorecards.run_canary_report(store, now=NOW)
        assert report["counts"] == {"MATCH": 40}
    finally:
        store.close()


def test_cycle_schedule_slots():
    for epoch in (0, 61, 299, 300, 451, 86_400 + 149):
        moment = datetime.fromtimestamp(epoch, tz=UTC)
        delay = scorecards.seconds_until_next_cycle(moment)
        assert 0 < delay <= scorecards.CYCLE_SECONDS
        slot = epoch + delay
        assert slot % scorecards.CYCLE_SECONDS \
            == scorecards.CYCLE_OFFSET_SECONDS


def test_run_forever_runs_bounded_cycles(tmp_path):
    store = _bare_store(tmp_path)
    try:
        scorecards.run_forever(store, shadow=True, max_cycles=2,
                               sleep=lambda _seconds: None)
        reads = mr.materialized_reads(store)
        kind, asset, horizon = scorecards.scorecard_keys()[0]
        assert reads.read_row(mr.shadow_kind(kind), asset, horizon) \
            is not None
    finally:
        store.close()
