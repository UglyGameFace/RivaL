"""XMarket production receipt must never confuse one working model with all 12."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.learning_health_receipt import _xmarket_progress
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.xmarket_inputs import XmarketInputs
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
from tests.xmarket_fixtures import point, valid_doc


def _clock() -> datetime:
    return (datetime.now(UTC) - timedelta(minutes=2)).replace(second=0, microsecond=0)


def _scopes(report: dict) -> dict:
    return {(x["asset"], x["horizon_minutes"]): x for x in report["scope_progress"]}


def test_uninitialized_host_reports_unknown_without_creating_tables(learning_store):
    report = _xmarket_progress(learning_store, _clock())
    assert report == {"status": "not_initialized", "shadow_only": True}
    with learning_store.connect() as conn:
        if learning_store.database.backend == "postgres":
            found = conn.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name='rival_xmarket_models'"
            ).fetchone()["n"]
        else:
            found = conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name='rival_xmarket_models'"
            ).fetchone()["n"]
    assert found == 0


def test_twelve_scope_report_does_not_fabricate_a_model_from_native_inputs(learning_store):
    now = _clock()
    XmarketInputs(learning_store).initialize()
    XmarketModelSet(learning_store).initialize()
    ShadowModelCompetition(learning_store).initialize()
    XmarketInputs(learning_store).record(
        [point(source, now, asset="BTC") for source in ("polymarket", "kalshi")]
    )
    report = _xmarket_progress(learning_store, now + timedelta(seconds=5))
    scopes = _scopes(report)
    assert len(scopes) == 12
    assert report["status"] == "waiting_for_fit"
    assert all(scopes[("BTC", horizon)]["status"] == "waiting_for_fit"
               for horizon in (5, 15, 60))
    assert all(scopes[("ETH", horizon)]["status"] == "waiting_for_inputs"
               for horizon in (5, 15, 60))
    assert all(row["stored_gate_passing_artifacts"] == 0
               and row["shadow_rows_24h"] == 0
               and row["latest_imported_at"] is None
               and row["latest_shadow_at"] is None
               and row["proves_live_edge"] is False
               for row in scopes.values())
    assert scopes[("BTC", 5)]["fresh_native_sources"] == 2
    assert scopes[("ETH", 5)]["missing_native_sources"] == ["kalshi", "polymarket"]
    assert "drive_file_id" not in json.dumps(report)
    assert "model_sha256" not in json.dumps(report)


def test_one_issued_model_does_not_hide_other_eleven_silent_scopes(learning_store):
    now = _clock()
    inputs = XmarketInputs(learning_store)
    inputs.initialize()
    inputs.record(
        [point(source, now, asset=asset)
         for asset in ("BTC", "ETH")
         for source in ("polymarket", "kalshi")]
    )
    models = XmarketModelSet(learning_store)
    models.initialize()
    assert models.import_document(
        "private-btc-file", json.dumps(valid_doc(venues=(), now=now)).encode()
    ) == 1
    assert models.import_document(
        "private-eth-file", json.dumps(
            valid_doc(venues=(), asset="ETH", horizon=15, now=now)
        ).encode()
    ) == 1
    competition = ShadowModelCompetition(learning_store)
    competition.initialize()
    with learning_store.connect() as conn:
        assert competition.issue(
            conn, samples=[], features=(.25,) * 10,
            now=now, venue="hyperliquid", contract="BTC", quote="USDC",
            asset="BTC", horizon=5, reference_opened_at=now - timedelta(minutes=5),
            target_opened_at=now, issued_at=now,
            incumbent_up=.6, baseline_up=.45,
            challengers=[{"family": "xmarket_v1", "probability": .7,
                          "training_rows": 1000, "model_sha256": "a" * 64,
                          "diagnostics": {"pm_spread": .02}}],
        ) == 1

    report = _xmarket_progress(learning_store, now + timedelta(seconds=5))
    scopes = _scopes(report)
    assert len(scopes) == 12
    assert report["status"] == "shadow_issuing"
    assert report["shadow_rows_24h"] == 1
    assert scopes[("BTC", 5)]["status"] == "shadow_issuing"
    assert scopes[("BTC", 5)]["stored_gate_passing_artifacts"] == 1
    assert scopes[("BTC", 5)]["shadow_rows_24h"] == 1
    assert scopes[("BTC", 5)]["latest_shadow_at"] is not None
    assert scopes[("BTC", 15)]["status"] == "waiting_for_fit"
    assert scopes[("BTC", 60)]["status"] == "waiting_for_fit"
    assert scopes[("ETH", 15)]["status"] == "imported_no_shadow_rows"
    assert scopes[("ETH", 15)]["stored_gate_passing_artifacts"] == 1
    assert scopes[("ETH", 15)]["latest_imported_at"] is not None
    assert scopes[("ETH", 15)]["shadow_rows_24h"] == 0
    assert scopes[("ETH", 5)]["status"] == "waiting_for_fit"
    assert all(scopes[("XRP", h)]["status"] == "waiting_for_inputs"
               for h in (5, 15, 60))
    assert all(row["proves_live_edge"] is False for row in scopes.values())
    assert "private-btc-file" not in json.dumps(report)
    assert "private-eth-file" not in json.dumps(report)
