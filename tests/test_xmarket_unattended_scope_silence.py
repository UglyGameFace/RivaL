"""Read-only XMarket silence monitoring regression tests."""
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import REGRESSION_RECHECK, RuntimeHealthMonitor
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 15, 15, tzinfo=UTC)


def test_xmarket_scope_silence_is_specific_and_clears(tmp_path):
    store = SQLiteHotStore(tmp_path / "xmarket-watch.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        ShadowModelCompetition(store).initialize()
        XmarketModelSet(store).initialize()
        monitor = RuntimeHealthMonitor(store)
        monitor.started_at = NOW - timedelta(hours=3)
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_xmarket_models "
                "(asset,horizon_minutes,model_sha256,trained_through,gates_passed,"
                "imported_at,drive_file_id,document) VALUES (?,?,?,?,?,?,?,?)",
                ("BTC", 15, "a"*64, (NOW-timedelta(days=1)).isoformat(), 1,
                 (NOW-timedelta(hours=3)).isoformat(), "test-drive", "{}"))
            for i in range(8):
                issued = NOW-timedelta(minutes=25+i)
                conn.execute(
                    "INSERT INTO perp_forward_audit "
                    "(venue,contract,quote,asset,horizon_minutes,model_version,"
                    "reference_opened_at,reference_close,issued_at,target_opened_at,"
                    "probability_up,baseline_up,training_evaluated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("hyperliquid", "BTC", "USDC", "BTC", 15, "online-v1",
                     (issued-timedelta(minutes=5)).isoformat(), 100.,
                     issued.isoformat(), issued.isoformat(), .6, .5, 150))
        with store.connect() as conn:
            faults = monitor._xmarket_issuance_faults(conn, NOW)
        assert "xmarket:no_shadows:BTC:15" in faults
        assert "xmarket:no_shadows:SOL:15" not in faults
        issued = NOW-timedelta(minutes=25)
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_model_shadow_forecasts "
                "(venue,contract,quote,asset,horizon_minutes,family,"
                "reference_opened_at,issued_at,target_opened_at,probability_up,"
                "incumbent_up,baseline_up,training_count,training_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("hyperliquid", "BTC", "USDC", "BTC", 15, "xmarket_v1",
                 (issued-timedelta(minutes=5)).isoformat(), issued.isoformat(),
                 issued.isoformat(), .6, .5, .5, 150, "b"*64))
            # Cache prevents a heavy query on every two-minute watchdog pass.
            assert monitor._xmarket_issuance_faults(conn, NOW+timedelta(minutes=2)) == faults
            assert monitor._xmarket_issuance_faults(
                conn, NOW+REGRESSION_RECHECK+timedelta(seconds=1)) == {}
    finally:
        store.close()


def test_new_fit_without_incumbents_is_not_silent_failure(tmp_path):
    store = SQLiteHotStore(tmp_path / "xmarket-new.sqlite")
    try:
        XmarketModelSet(store).initialize()
        monitor = RuntimeHealthMonitor(store)
        with store.connect() as conn:
            assert monitor._xmarket_issuance_faults(conn, NOW) == {}
            conn.execute(
                "INSERT INTO rival_xmarket_models "
                "(asset,horizon_minutes,model_sha256,trained_through,gates_passed,"
                "imported_at,drive_file_id,document) VALUES (?,?,?,?,?,?,?,?)",
                ("BTC", 60, "a"*64, (NOW-timedelta(days=1)).isoformat(), 1,
                 (NOW-timedelta(minutes=20)).isoformat(), "test-drive", "{}"))
            assert monitor._xmarket_issuance_faults(
                conn, NOW+REGRESSION_RECHECK+timedelta(seconds=1)) == {}
    finally:
        store.close()
