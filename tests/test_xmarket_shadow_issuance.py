from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.out_the_gate import live_lean
from parlay_bot.perpetuals import forward_audit, offline_model_pins
from parlay_bot.perpetuals.family_governance import FAMILIES, FamilyPromotion
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import CHALLENGER_PREFIXES, ShadowModelCompetition
from parlay_bot.perpetuals.xmarket_inputs import XmarketInputs
from parlay_bot.perpetuals.xmarket_model import (
    XmarketModelSet,
    artifact_identity,
    is_xmarket_document,
    xmarket_calls,
)
from tests.test_volcal_v1_shadow import _seed_cycle_candles
from tests.xmarket_fixtures import NOW, point, valid_doc


def test_immutable_forecast_diagnostics_and_replay_on_both_backends(learning_store):
    comp = ShadowModelCompetition(learning_store)
    comp.initialize()
    identity = {"samples": [], "features": (.25,) * 10, "now": NOW, "venue": "hyperliquid",
        "contract": "BTC", "quote": "USDC", "asset": "BTC", "horizon": 5,
        "reference_opened_at": NOW - timedelta(minutes=5), "target_opened_at": NOW,
        "issued_at": NOW, "incumbent_up": .6, "baseline_up": .45}
    item = {"family": "xmarket_v1", "probability": .7, "training_rows": 1000,
            "model_sha256": "a" * 64, "diagnostics": {"pm_spread": .02, "kalshi_spread": None}}
    with learning_store.connect() as conn:
        assert comp.issue(conn, **identity, challengers=[item]) == 1
        old = dict(conn.execute("SELECT * FROM rival_model_shadow_forecasts WHERE family='xmarket_v1'").fetchone())
        changed = {**item, "probability": .2, "model_sha256": "b" * 64,
                   "diagnostics": {"pm_spread": .5}}
        assert comp.issue(conn, **identity, challengers=[changed]) == 0
        assert dict(conn.execute("SELECT * FROM rival_model_shadow_forecasts WHERE family='xmarket_v1'").fetchone()) == old
        row = conn.execute("SELECT model_sha256,diagnostics_json FROM rival_xmarket_diagnostics").fetchone()
        assert row["model_sha256"] == "a" * 64
        assert json.loads(row["diagnostics_json"])["pm_spread"] == .02


def test_artifact_pin_and_promotion_never_transfer_to_new_fit(learning_store):
    ShadowModelCompetition(learning_store).initialize()
    models = XmarketModelSet(learning_store)
    first = valid_doc()
    payload = json.dumps(first).encode()
    assert is_xmarket_document(payload)
    assert not is_xmarket_document(b'{"schema":"wrong"}')
    assert models.import_document("file-1", payload) == 1
    assert models.import_document("file-1", payload) == 0
    initial = models.active("BTC", 5, now=NOW)
    identity = artifact_identity(learning_store, "BTC", 5)
    assert identity and identity["graded_count"] == 0
    promoter = FamilyPromotion(learning_store)
    promoter.initialize()
    args = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC", "quote": "USDC", "horizon": 5, "now": NOW}
    assert promoter.selected(**args) is None
    with learning_store.connect() as conn:
        conn.execute("""INSERT INTO rival_family_promotion
            (venue,contract,quote,asset,horizon_minutes,evaluated_at,selected_family,status,reason,evidence_json)
            VALUES ('hyperliquid','BTC','USDC','BTC',5,?,'xmarket_v1','promoted_paper','test',?)""",
            (NOW.isoformat(), json.dumps({"xmarket_v1": {"artifact": identity}})))
    assert promoter.selected(**args) == "xmarket_v1"
    newer = valid_doc(now=NOW + timedelta(hours=1))
    newer["weights"][0] += .1
    newer["weights_sha256"] = hashlib.sha256(json.dumps(newer["weights"], separators=(",", ":")).encode()).hexdigest()
    models.import_document("file-2", json.dumps(newer).encode())
    models._loaded_at = None
    assert models.active("BTC", 5, now=NOW)["_sha256"] == initial["_sha256"]
    assert models.queue_depth("BTC", 5, now=NOW) == 1
    offline_model_pins.set_verdict(learning_store, "xmarket_v1", "BTC", 5,
                                 initial["_sha256"], "rejected", now=NOW)
    models._loaded_at = None
    assert models.active("BTC", 5, now=NOW)["_sha256"] != initial["_sha256"]
    assert promoter.selected(**args) is None


@pytest.mark.parametrize("enabled,has_model,expected", [(False, True, 0), (True, False, 0), (True, True, 1)])
def test_actual_cycle_kill_switch_and_missing_artifact(learning_store, monkeypatch, enabled, has_model, expected):
    now = datetime.now(UTC)
    market = PerpetualMarketStore(learning_store)
    monitor = ForwardAccuracyMonitor(market)
    _seed_cycle_candles(market, now=now)
    if has_model:
        XmarketModelSet(learning_store).import_document("fit", json.dumps(valid_doc(venues=(), now=now)).encode())
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    rows = [point("polymarket", boundary - timedelta(minutes=lag), .55) for lag in (0, 15, 60)]
    rows.append(point("kalshi", boundary, .6))
    for row in rows:
        row["observed_at"] = boundary.isoformat()
    XmarketInputs(learning_store).record(rows)
    monkeypatch.setattr(forward_audit, "ISSUE_XMARKET_V1_CHALLENGERS", enabled)
    monitor.run_once(now=now)
    with learning_store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts WHERE family='xmarket_v1'").fetchone()["n"]
        assert count == expected
        assert conn.execute("SELECT COUNT(*) AS n FROM perp_forward_audit").fetchone()["n"] >= 1


def test_family_is_not_a_live_lean_or_outbound_alert(tmp_path):
    from parlay_bot.discord_app.research_alerts import research_alerts
    from tests.test_research_alerts import NOW as ALERT_NOW
    from tests.test_research_alerts import _store

    assert "xmarket_v1" not in FAMILIES
    assert "xmarket_" in CHALLENGER_PREFIXES
    assert live_lean(object(), asset="BTC", horizon=5, venue="hyperliquid",
                     contract="BTC", quote="USDC", now=NOW, model_version="xmarket_v1") is None
    store = _store(tmp_path, forecast_age=timedelta(minutes=3), trained_age=timedelta(days=2),
                   review_age=timedelta(minutes=10), pooled_scores=[(.24, .25)] * 400)
    try:
        FamilyPromotion(store).initialize()
        with store.connect() as conn:
            conn.execute("""INSERT INTO rival_family_promotion
                (venue,contract,quote,asset,horizon_minutes,evaluated_at,selected_family,status,reason,evidence_json)
                VALUES ('hyperliquid','BTC','USDC','BTC',5,?,NULL,'held','test',?)""",
                (ALERT_NOW.isoformat(), json.dumps({"xmarket_v1": {"days": 1, "targets": 10,
                    "qualified": False, "forward_gain": .01}})))
        assert all("xmarket" not in (key + text).lower() for key, text in research_alerts(store, now=ALERT_NOW))
    finally:
        store.close()


def test_no_model_or_missing_input_never_creates_a_call():
    assert xmarket_calls(None, asset="BTC", horizon=5, vector=([0.] * 18, {}), now=NOW) == []
    assert xmarket_calls(object(), asset="BTC", horizon=5, vector=None, now=NOW) == []
