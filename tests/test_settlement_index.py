"""Settlement-index recording: the fair-value pre-test's data foundation.

The recorder writes only when ticks actually arrive, thins each series
to the configured cadence, and never lets a downstream fault reach the
venue stream that feeds it.
"""
from datetime import UTC, datetime, timedelta

from parlay_bot.config import Settings
from parlay_bot.perpetuals.polybolt_twap import CHANNEL, PolyBoltTWAP
from parlay_bot.perpetuals.settlement_index import (
    IndexTick,
    SettlementIndexRecorder,
)
from parlay_bot.storage.hot import SQLiteHotStore

T0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def _tick(*, venue="polymarket", asset="BTC", at=T0, value=100.0, avg_60s=None):
    return IndexTick(
        venue=venue, asset=asset, source_at=at, received_at=at,
        value=value, avg_60s=avg_60s, receipt_sha256="ab" * 32,
    )


def _recorder(tmp_path, *, throttle=10):
    return SettlementIndexRecorder(
        SQLiteHotStore(tmp_path / "idx.sqlite"), throttle_seconds=throttle)


def test_no_ticks_no_rows(tmp_path):
    recorder = _recorder(tmp_path)
    assert recorder.flush() == 0
    assert recorder.ticks(venue="polymarket", asset="BTC") == []


def test_ticks_persist_with_their_fields(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.note(_tick(value=67234.5))
    recorder.note(_tick(venue="kalshi", asset="ETH", value=3200.25, avg_60s=3199.5))
    assert recorder.flush() == 2
    pm = recorder.ticks(venue="polymarket", asset="BTC")
    assert len(pm) == 1
    assert pm[0]["value"] == 67234.5
    assert pm[0]["avg_60s"] is None  # the TWAP value already IS the average
    assert pm[0]["source_at"] == T0.isoformat()
    kalshi = recorder.ticks(venue="kalshi", asset="ETH")
    assert kalshi[0]["avg_60s"] == 3199.5


def test_flush_thins_to_the_throttle_cadence(tmp_path):
    recorder = _recorder(tmp_path, throttle=10)
    recorder.note(_tick(at=T0, value=100.0))
    assert recorder.flush() == 1
    # Inside the throttle the newest print is held, not written.
    recorder.note(_tick(at=T0 + timedelta(seconds=5), value=101.0))
    assert recorder.flush() == 0
    # Once the cadence elapses the held (newest) print lands.
    recorder.note(_tick(at=T0 + timedelta(seconds=11), value=102.0))
    assert recorder.flush() == 1
    rows = recorder.ticks(venue="polymarket", asset="BTC")
    assert [r["value"] for r in rows] == [100.0, 102.0]


def test_out_of_order_and_invalid_ticks_are_dropped(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.note(_tick(at=T0 + timedelta(seconds=30), value=100.0))
    recorder.note(_tick(at=T0 + timedelta(seconds=20), value=99.0))  # older
    recorder.note(_tick(venue="binance"))  # untracked venue
    recorder.note(_tick(asset="DOGE"))  # untracked asset
    recorder.note(_tick(at=T0 + timedelta(seconds=40), value=-1.0))  # bad price
    assert recorder.flush() == 1
    rows = recorder.ticks(venue="polymarket", asset="BTC")
    assert [r["value"] for r in rows] == [100.0]


def _polybolt_snapshot(now, *, seq=1):
    points = []
    for n in range(12):
        ts = now - timedelta(seconds=112 - n * 10)
        points.append({
            "timestamp": int(ts.timestamp() * 1000),
            "full_accuracy_value": str(65000 + n * 3),
        })
    return {
        "v": 1, "channel": CHANNEL, "seq": seq, "snapshot": True,
        "payload": {"symbol": "btcusd", "source": "chainlink", "data": points},
    }


def _feed():
    return PolyBoltTWAP(api_key="k", secret="s", passphrase="p")


def test_polybolt_ticks_reach_the_recorder_only_with_a_sink(tmp_path):
    now = datetime(2026, 11, 20, 12, 2, 30, tzinfo=UTC)
    recorder = _recorder(tmp_path)

    bare = _feed()
    bare.accept({"op": "authed"}, received_at=now)
    bare.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert bare.accept(_polybolt_snapshot(now), received_at=now) == 12
    assert recorder.flush() == 0  # no sink wired: nothing is recorded

    wired = _feed()
    wired.tick_sink = lambda tick: recorder.note(IndexTick(
        venue="polymarket", asset=tick.asset, source_at=tick.observed_at,
        received_at=tick.received_at, value=tick.price, avg_60s=None,
        receipt_sha256=tick.receipt_sha256))
    wired.accept({"op": "authed"}, received_at=now)
    wired.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert wired.accept(_polybolt_snapshot(now), received_at=now) == 12
    # The batch's newest print per asset is what the throttle persists.
    assert recorder.flush() == 1
    rows = recorder.ticks(venue="polymarket", asset="BTC")
    assert rows[0]["value"] == 65000 + 11 * 3


def test_a_failing_sink_never_breaks_the_stream(tmp_path):
    now = datetime(2026, 11, 20, 12, 2, 30, tzinfo=UTC)
    feed = _feed()

    def broken(_tick):
        raise RuntimeError("recorder down")

    feed.tick_sink = broken
    feed.accept({"op": "authed"}, received_at=now)
    feed.accept({"op": "subscribed", "channel": CHANNEL}, received_at=now)
    assert feed.accept(_polybolt_snapshot(now), received_at=now) == 12
    assert feed.status(now=now)["accepted_twap_samples"] == 12


def test_fair_value_collection_config_defaults():
    fields = Settings.model_fields
    assert fields["rival_polybolt_recorder_enabled"].default is True
    assert fields["rival_kalshi_brti_enabled"].default is True
    assert fields["rival_settlement_index_seconds"].default == 10
    assert fields["kalshi_api_key_id"].default is None
    assert fields["kalshi_api_private_key"].default is None


def test_ticks_streams_past_the_chunk_boundary(tmp_path):
    """4,500 stored ticks (FETCH_CHUNK=2000) read back complete, in
    source-time order, with contents identical to what was stored."""
    recorder = _recorder(tmp_path)
    recorder.initialize()
    expected = []
    with recorder.store.connect() as conn:
        for i in range(4500):
            at = T0 + timedelta(seconds=i * 10)
            row = {
                "venue": "polymarket", "asset": "BTC",
                "source_at": at.isoformat(), "received_at": at.isoformat(),
                "value": 67000.0 + i, "avg_60s": None,
                "receipt_sha256": "ab" * 32,
            }
            expected.append(row)
            conn.execute(
                "INSERT INTO rival_settlement_index_ticks VALUES (?,?,?,?,?,?,?)",
                (row["venue"], row["asset"], row["source_at"],
                 row["received_at"], row["value"], row["avg_60s"],
                 row["receipt_sha256"]),
            )
    assert recorder.ticks(venue="polymarket", asset="BTC") == expected


def test_note_drops_stale_and_future_ticks(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.note(IndexTick(
        venue="polymarket", asset="BTC", source_at=T0,
        received_at=T0 + timedelta(hours=2), value=100.0,
        avg_60s=None, receipt_sha256="ab" * 32))
    assert recorder.pending_count() == 0
    recorder.note(IndexTick(
        venue="polymarket", asset="BTC", source_at=T0 + timedelta(hours=1),
        received_at=T0, value=100.0,
        avg_60s=None, receipt_sha256="ab" * 32))
    assert recorder.pending_count() == 0
    recorder.note(_tick(at=T0))
    assert recorder.pending_count() == 1


def test_failed_flush_keeps_the_tick_for_retry(tmp_path):
    import pytest

    class FlakyStore:
        def __init__(self, inner):
            self.inner = inner
            self.fail = True

        def connect(self):
            if self.fail:
                raise RuntimeError("db down")
            return self.inner.connect()

    flaky = FlakyStore(SQLiteHotStore(tmp_path / "idx.sqlite"))
    recorder = SettlementIndexRecorder(flaky)
    recorder.note(_tick(at=T0, value=100.0))
    with pytest.raises(RuntimeError):
        recorder.flush()
    # The tick is still pending and the throttle clock has not moved:
    # the retry must be able to write the very same tick.
    assert recorder.pending_count() == 1
    flaky.fail = False
    assert recorder.flush() == 1
    assert recorder.pending_count() == 0
    rows = recorder.ticks(venue="polymarket", asset="BTC")
    assert len(rows) == 1 and rows[0]["value"] == 100.0
