"""Exact KuCoin source-gap repair using immutable, due prospective path rows."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.path_forward import ProspectivePathLedger

CLOCK = datetime(2026, 11, 16, 17, tzinfo=UTC)
REFERENCE = datetime(2026, 11, 8, 10, 45, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _seed(store, *, reference=REFERENCE, after=True, duplicate_gap=False,
          valid_clock=True):
    market = PerpetualMarketStore(store)
    market.initialize()
    ProspectivePathLedger(store).initialize()
    worker = KucoinHistoryRecovery(market, assets=("ETH",), page_bars=12)
    worker.initialize()
    source = {
        "venue": "kucoin", "contract": "ETHUSDTM", "quote": "USDT",
        "asset": "ETH", "horizon_minutes": 15,
        "model_version": "paper-path-v1", "side": "long",
        "reference_opened_at": reference.isoformat(),
        "issued_at": (
            reference + (timedelta(minutes=11) if not valid_clock
                         else STEP + timedelta(seconds=1))
        ).isoformat(),
        "target_opened_at": (reference + 4 * STEP).isoformat(),
        "reference_close": 100.0, "sigma_horizon": 0.001,
        "tp1_probability": 0.4, "tp2_probability": 0.2,
        "tp1_baseline": 0.4, "tp2_baseline": 0.2,
        "signed_return_p10": -0.01, "signed_return_p50": 0.0,
        "signed_return_p90": 0.01,
        "training_count": 50, "cohort_count": 30,
        "training_sha256": "0" * 64, "state": "pending",
    }
    with store.connect() as conn:
        columns = list(source)
        conn.execute(
            "INSERT INTO rival_forward_paths (" + ",".join(columns)
            + ") VALUES (" + ",".join("?" for _ in columns) + ")",
            tuple(source.values()),
        )
        # Expected future slots: +10m, +15m, +20m. +20m is the
        # missing candle; +25m is the authentic closing-side neighbor.
        times = [reference + t * STEP for t in (1, 2)]
        if not duplicate_gap:
            times.append(reference + 3 * STEP)
        if after:
            times.append(reference + 5 * STEP)
        for opened in times:
            closed = opened + STEP - timedelta(milliseconds=1)
            observed = opened + STEP
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,
                    close_price,volume_base,trades)
                   VALUES ('kucoin','ETHUSDTM','USDT',5,?,?,?,?,?,?,?,?,?)""",
                (opened.isoformat(), closed.isoformat(), observed.isoformat(),
                 100., 105., 95., 101., 10., 1),
            )
    return worker, reference + 4 * STEP


@pytest.mark.asyncio
async def test_one_due_path_hole_repairs_without_rewinding_bulk_or_relabeling(learning_store):
    worker, hole = _seed(learning_store)
    original = worker._position("ETH", CLOCK)
    assert datetime.fromisoformat(original["next_opened_at"]) < hole
    assert worker._queue_due_path_gaps(
        "ETH", now=CLOCK, end=CLOCK - timedelta(hours=4),
    ) == 1
    assert worker._queue_due_path_gaps(
        "ETH", now=CLOCK, end=CLOCK - timedelta(hours=4),
    ) == 0

    calls = []

    async def multiplier(asset, symbol, now):
        assert asset == "ETH" and symbol == "ETHUSDTM"
        return 1.0

    async def public(_url, params):
        calls.append(params)
        return {"code": "200000", "data": {
            "symbol": "ETHUSDTM", "tradeType": "FUTURES",
            "list": [[int(hole.timestamp()), "100", "105", "95",
                      "101", "1", "101"]],
        }}

    worker._multiplier = multiplier
    worker._public = public
    try:
        assert await worker.fetch_asset("ETH", now=CLOCK) == 1
        assert len(calls) == 1
        assert calls[0]["startAt"] == calls[0]["endAt"] == int(hole.timestamp())
        with learning_store.connect() as conn:
            recovered = conn.execute(
                """SELECT first_observed_at FROM perp_market_candles
                   WHERE venue='kucoin' AND contract='ETHUSDTM'
                     AND opened_at=?""", (hole.isoformat(),),
            ).fetchone()
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM perp_history_gap_repair "
                "WHERE venue='kucoin' AND asset='ETH'",
            ).fetchone()["n"]
            path = conn.execute(
                "SELECT state,issued_at FROM rival_forward_paths "
                "WHERE venue='kucoin' AND asset='ETH'",
            ).fetchone()
        assert recovered is not None
        assert datetime.fromisoformat(recovered["first_observed_at"]) > CLOCK
        assert active == 0
        assert path["state"] == "pending"
        assert path["issued_at"] == (REFERENCE + STEP + timedelta(seconds=1)).isoformat()
        assert worker._position("ETH", CLOCK)["next_opened_at"] == original["next_opened_at"]
    finally:
        await worker.aclose()


@pytest.mark.parametrize("blocked", ["no_after", "two_gaps", "invalid_clock", "not_due"])
def test_path_queue_refuses_unproven_or_untimely_source_holes(learning_store, blocked):
    reference = CLOCK - timedelta(minutes=20) if blocked == "not_due" else REFERENCE
    worker, _hole = _seed(
        learning_store, reference=reference,
        after=blocked != "no_after",
        duplicate_gap=blocked == "two_gaps",
        valid_clock=blocked != "invalid_clock",
    )
    assert worker._queue_due_path_gaps(
        "ETH", now=CLOCK, end=CLOCK - timedelta(hours=4),
    ) == 0
    assert worker._pending_gaps("ETH", now=CLOCK) == 0


@pytest.mark.asyncio
async def test_unpublished_kucoin_original_keeps_path_gap_pending(learning_store):
    worker, hole = _seed(learning_store)
    requests = []

    async def multiplier(*_args):
        return 1.0

    async def empty(_url, params):
        requests.append(params)
        return {"code": "200000", "data": {
            "symbol": "ETHUSDTM", "tradeType": "FUTURES", "list": [],
        }}

    worker._multiplier = multiplier
    worker._public = empty
    try:
        assert await worker.fetch_asset("ETH", now=CLOCK) == 0
        assert len(requests) == 1
        assert requests[0]["startAt"] == requests[0]["endAt"] == int(hole.timestamp())
        assert worker._pending_gaps("ETH", now=CLOCK) == 1
        with learning_store.connect() as conn:
            gap = conn.execute(
                "SELECT retry_after FROM perp_history_gap_repair "
                "WHERE venue='kucoin' AND asset='ETH' AND opened_at=?",
                (hole.isoformat(),),
            ).fetchone()
            assert datetime.fromisoformat(gap["retry_after"]) == CLOCK + timedelta(hours=6)
            unchanged = conn.execute(
                "SELECT COUNT(*) AS n FROM perp_market_candles "
                "WHERE venue='kucoin' AND opened_at=?", (hole.isoformat(),),
            ).fetchone()
        assert unchanged["n"] == 0
    finally:
        await worker.aclose()


def test_path_gap_probe_skips_uninitialized_forecast_table(learning_store):
    market = PerpetualMarketStore(learning_store)
    market.initialize()
    worker = KucoinHistoryRecovery(market, assets=("ETH",))
    worker.initialize()
    assert worker._queue_due_path_gaps(
        "ETH", now=CLOCK, end=CLOCK - timedelta(hours=4),
    ) == 0


@pytest.mark.asyncio
async def test_due_source_gap_can_recover_during_unrelated_bulk_empty_page_cooldown(
    learning_store,
):
    worker, hole = _seed(learning_store)
    original = worker._position("ETH", CLOCK)
    bulk_retry = CLOCK + timedelta(hours=6)
    with learning_store.connect() as conn:
        conn.execute(
            """UPDATE perp_history_recovery SET status='upstream_empty',
                      retry_after=? WHERE venue='kucoin' AND asset='ETH'""",
            (bulk_retry.isoformat(),),
        )
    calls = []

    async def multiplier(*_args):
        return 1.0

    async def public(_url, params):
        calls.append(params)
        return {"code": "200000", "data": {
            "symbol": "ETHUSDTM", "tradeType": "FUTURES",
            "list": [[int(hole.timestamp()), "100", "105", "95",
                      "101", "1", "101"]],
        }}

    worker._multiplier = multiplier
    worker._public = public
    try:
        assert await worker.fetch_asset("ETH", now=CLOCK) == 1
        assert len(calls) == 1
        assert calls[0]["startAt"] == calls[0]["endAt"] == int(hole.timestamp())
        position = worker.progress()[0]
        assert position["next_opened_at"] == original["next_opened_at"]
        # An independent repair must not erase the bulk page's own cooldown.
        assert position["status"] == "upstream_empty"
        assert position["retry_after"] == bulk_retry.isoformat()
        assert worker._pending_gaps("ETH", now=CLOCK) == 0
        assert await worker.fetch_asset(
            "ETH", now=CLOCK + timedelta(minutes=1)
        ) == 0
        assert len(calls) == 1
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_gap_recovery_never_bypasses_provider_wide_block(learning_store):
    worker, hole = _seed(learning_store)
    worker._position("ETH", CLOCK)
    with learning_store.connect() as conn:
        conn.execute(
            """UPDATE perp_history_recovery SET status='blocked',
                      retry_after=? WHERE venue='kucoin' AND asset='ETH'""",
            ((CLOCK + timedelta(hours=6)).isoformat(),),
        )

    async def fail(*_args, **_kwargs):
        pytest.fail("provider-wide cooldown must prevent an HTTP request")

    worker._multiplier = fail
    worker._public = fail
    try:
        assert await worker.fetch_asset("ETH", now=CLOCK) == 0
        assert worker._pending_gaps("ETH", now=CLOCK) == 0
        with learning_store.connect() as conn:
            missing = conn.execute(
                """SELECT 1 FROM perp_market_candles
                   WHERE venue='kucoin' AND opened_at=?""",
                (hole.isoformat(),),
            ).fetchone()
        assert missing is None
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_empty_gap_does_not_clear_independent_bulk_retry(learning_store):
    worker, hole = _seed(learning_store)
    worker._position("ETH", CLOCK)
    bulk_retry = CLOCK + timedelta(hours=6)
    with learning_store.connect() as conn:
        conn.execute(
            """UPDATE perp_history_recovery SET status='upstream_empty',
                      retry_after=? WHERE venue='kucoin' AND asset='ETH'""",
            (bulk_retry.isoformat(),),
        )
    requests = []

    async def multiplier(*_args):
        return 1.0

    async def empty(_url, params):
        requests.append(params)
        return {"code": "200000", "data": {
            "symbol": "ETHUSDTM", "tradeType": "FUTURES", "list": [],
        }}

    worker._multiplier = multiplier
    worker._public = empty
    try:
        assert await worker.fetch_asset("ETH", now=CLOCK) == 0
        assert len(requests) == 1
        assert requests[0]["startAt"] == requests[0]["endAt"] == int(hole.timestamp())
        assert worker.progress()[0]["retry_after"] == bulk_retry.isoformat()
        with learning_store.connect() as conn:
            pending = conn.execute(
                """SELECT retry_after FROM perp_history_gap_repair
                   WHERE venue='kucoin' AND asset='ETH' AND opened_at=?""",
                (hole.isoformat(),),
            ).fetchone()
        assert pending["retry_after"] == bulk_retry.isoformat()
        assert await worker.fetch_asset("ETH", now=CLOCK + timedelta(minutes=1)) == 0
        assert len(requests) == 1
    finally:
        await worker.aclose()
