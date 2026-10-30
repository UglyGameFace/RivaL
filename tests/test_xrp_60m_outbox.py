"""New market compatibility and crash-safe delivery behavior, no live orders."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app import auto_hunt
from parlay_bot.perpetuals.forward_audit import (
    ForwardAccuracyMonitor,
    select_live_streams,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS, asset_for_contract
from parlay_bot.storage.hot import SQLiteHotStore


def test_market_catalog_and_live_stream_selection_cover_xrp():
    assert ASSETS == ("BTC", "ETH", "SOL", "XRP")
    assert HORIZONS == (5, 15, 60)
    assert asset_for_contract("XRPUSDT") == "XRP"
    assert asset_for_contract("XBTUSDTM") == "BTC"
    chosen = select_live_streams([
        ("hyperliquid", "BTC", "USDC"),
        ("hyperliquid", "ETH", "USDC"),
        ("hyperliquid", "SOL", "USDC"),
        ("hyperliquid", "XRP", "USDC"),
    ], slot=0)
    assert {contract for _, contract, _ in chosen} == set(ASSETS)
    # Existing private DM opt-ins must not silently start fourth-coin alerts.
    assert auto_hunt.ASSETS == ("BTC", "ETH", "SOL")
    assert auto_hunt.HORIZONS == (5, 15)


def test_xrp_60m_is_genuinely_issued_from_live_history_and_settles_later(tmp_path):
    store = SQLiteHotStore(tmp_path / "xrp-60m.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    now = datetime.now(UTC).replace(second=20, microsecond=0)
    end = now.replace(minute=now.minute // 5 * 5, second=0)
    market.initialize()
    count = 180
    try:
        with store.connect() as conn:
            for i in range(count):
                opened = end - timedelta(minutes=5 * (count - i))
                closed = opened + timedelta(minutes=5, milliseconds=-1)
                observed = opened + timedelta(minutes=5)
                price = 1.0 + .001 * i + .002 * (i % 7)
                conn.execute(
                    """INSERT INTO perp_market_candles (
                       venue,contract,quote,interval_minutes,opened_at,closed_at,
                       first_observed_at,open_price,high_price,low_price,close_price,
                       volume_base,trades)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "XRP", "USDC", 5, opened.isoformat(),
                     closed.isoformat(), observed.isoformat(),
                     price, price + .02, price - .02, price + .001, 100, 40),
                )
        # Avoid relying on mock network/health: inspect the real SQL
        # eligibility just as production does, but use only seeded XRP.
        from parlay_bot.perpetuals import shadow_monitor
        original = shadow_monitor.ShadowLearningMonitor._eligible_streams
        shadow_monitor.ShadowLearningMonitor._eligible_streams = (
            lambda self, **kwargs: [("hyperliquid", "XRP", "USDC")]
        )
        try:
            issued, settled = monitor.run_once(now=now)
        finally:
            shadow_monitor.ShadowLearningMonitor._eligible_streams = original
        assert issued == 3 and settled == 0
        rows = monitor.recent_issued(asset="XRP", limit=8)
        assert {int(row["horizon_minutes"]) for row in rows} == {5, 15, 60}
        one_hour = next(row for row in rows if row["horizon_minutes"] == 60)
        assert one_hour["settled_at"] is None
        # Not a Polymarket 60-minute Up/Down event: it predicts the
        # 5m candle that opens one hour after the reference bar.
        latest = end - timedelta(minutes=5)
        target = latest + timedelta(minutes=60)
        assert datetime.fromisoformat(one_hour["target_opened_at"]) == target
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_market_candles (
                    venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "XRP", "USDC", 5, target.isoformat(),
                 (target + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                 (target + timedelta(minutes=5, seconds=1)).isoformat(),
                 1.2, 1.25, 1.18, 1.22, 90, 30),
            )
        assert monitor._settle(target+timedelta(minutes=6)) == 1
        assert monitor._settle(target+timedelta(minutes=6)) == 0
    finally:
        store.close()


def test_outbox_preflight_retries_after_restart_but_unknown_send_never_retries(tmp_path):
    store = SQLiteHotStore(tmp_path / "outbox.sqlite")
    store.initialize()
    outbox = auto_hunt.AutoHuntStore(store)
    outbox.initialize()
    now = datetime.now(UTC).replace(microsecond=0)
    watch = auto_hunt.HuntCandidate(
        asset="BTC", horizon=5, venue="hyperliquid",
        issued=now-timedelta(seconds=30), expires=now+timedelta(minutes=5),
        probability_up=.7, bid=100, ask=100.1, quote_at=now,
        engine="live", model_version="test",
    )
    try:
        outbox.subscribe("123", now=now-timedelta(minutes=1))
        assert outbox.claim("123", watch, now=now)
        assert not outbox.claim("123", watch, now=now)
        outbox.defer_preflight("123", watch, now=now)
        restarted = auto_hunt.AutoHuntStore(store)
        restarted.initialize()
        assert not restarted.claim("123", watch, now=now+timedelta(seconds=10))
        assert restarted.claim("123", watch, now=now+timedelta(seconds=21))
        assert restarted.begin_delivery(
            "123", watch, now=now+timedelta(seconds=21),
        )
        assert not restarted.begin_delivery(
            "123", watch, now=now+timedelta(seconds=21),
        )
        restarted.finish("123", watch, sent=False)
        assert restarted.delivery_health("123")["unknown"] == 1
        assert not restarted.claim(
            "123", watch, now=now+timedelta(seconds=60),
        )
        restarted.initialize()
        assert not restarted.claim(
            "123", watch, now=now+timedelta(seconds=60),
        )
    finally:
        store.close()
