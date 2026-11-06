"""Shadow rows copied from a withdrawn (no-trade) incumbent result are withdrawn too."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.storage.hot import SQLiteHotStore

MODEL = "prospective-logistic-persistent-v3"


def test_stale_no_trade_labels_are_withdrawn_from_the_challenger_ledger(tmp_path):
    store = SQLiteHotStore(tmp_path / "shadow.sqlite")
    market = PerpetualMarketStore(store)
    try:
        ForwardAccuracyMonitor(market).initialize()
        ShadowModelCompetition(store).initialize()
        target = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        ref = (target - timedelta(minutes=5)).isoformat()
        issued = (target - timedelta(minutes=4)).isoformat()
        settled = (target + timedelta(minutes=6)).isoformat()
        with store.connect() as conn:
            for venue, outcome in (("okx", None), ("gate", 1)):
                # okx: incumbent result withdrawn (no-trade); gate: genuine result.
                conn.execute(
                    """INSERT INTO perp_forward_audit (venue,contract,quote,asset,
                         horizon_minutes,model_version,reference_opened_at,reference_close,
                         issued_at,target_opened_at,probability_up,baseline_up,
                         training_evaluated,settled_at,outcome_up,realized_close,
                         model_brier,baseline_brier)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, "BTC", "USDT", "BTC", 5, MODEL, ref, 100., issued,
                     target.isoformat(), .6, .5, 100, settled, outcome, 100.,
                     None if outcome is None else .16, None if outcome is None else .25))
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                         horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                         probability_up,incumbent_up,baseline_up,training_count,
                         training_sha256,settled_at,outcome_up,model_brier,incumbent_brier,
                         baseline_brier)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, "BTC", "USDT", "BTC", 5, "knn21_v1", ref, issued,
                     target.isoformat(), .55, .6, .5, 300, "a" * 64, settled,
                     0 if outcome is None else 1, .3025, .36, .25))
        store._shadow_competition_ready = False  # next start-up runs the correction
        ShadowModelCompetition(store).initialize()
        with store.connect() as conn:
            rows = {r["venue"]: r for r in conn.execute(
                "SELECT venue, settled_at, outcome_up, model_brier FROM rival_model_shadow_forecasts"
            ).fetchall()}
        assert rows["okx"]["outcome_up"] is None and rows["okx"]["model_brier"] is None
        assert rows["okx"]["settled_at"] == settled  # kept, like the incumbent fix
        assert rows["gate"]["outcome_up"] == 1 and rows["gate"]["model_brier"] is not None
    finally:
        store.close()
