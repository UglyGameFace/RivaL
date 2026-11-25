"""Display reads must never convert a FAILED read into a false zero.

Defect sweep 2026-11-24, finding D-1: the Models board and the model
detail page collapsed optional_rows() failures (None) with ``or []`` and
then presented the empty result as fact — "0 qualified of 0" on the board
and "✅ never cut for losing" on the detail page — during exactly the
database-distress episodes those screens exist to diagnose. A dropped
table stands in for any failed read here (optional_rows returns None for
both, by design).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from parlay_bot.discord_app import home, model_detail, trial_board
from parlay_bot.perpetuals.early_stop import EarlyStop
from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def store(tmp_path):
    hot = SQLiteHotStore(tmp_path / "honesty.sqlite")
    market = PerpetualMarketStore(hot)
    market.initialize()
    ForwardAccuracyMonitor(market).initialize()
    EarlyStop(hot).initialize()
    with hot.connect() as conn:
        conn.executescript(FAMILY_PROMOTION_SCHEMA)
    yield hot
    hot.close()


def _drop(store, table):
    with store.connect() as conn:
        conn.execute(f"DROP TABLE {table}")


def _promotion(store, venue, horizon, evidence, asset="BTC"):
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion (venue,contract,quote,asset,horizon_minutes,
                 evaluated_at,selected_family,status,reason,evidence_json)
               VALUES (?,?,?,?,?,?,NULL,'held','test',?)""",
            (venue, f"{asset}-{venue}", "USDT", asset, horizon, NOW.isoformat(),
             json.dumps(evidence)))


# --- the Models board -------------------------------------------------------

def test_board_marks_a_failed_promotion_read_unavailable(store):
    _drop(store, "rival_family_promotion")
    board = trial_board.read(store, asset="BTC", horizon=5, retired=frozenset())
    assert board["unavailable"] is True


def test_board_marks_a_failed_stop_read_unavailable(store):
    _drop(store, "rival_family_early_stop")
    board = trial_board.read(store, asset="BTC", horizon=5, retired=frozenset())
    assert board["unavailable"] is True


def test_board_empty_tables_are_available_and_zero(store):
    board = trial_board.read(store, asset="BTC", horizon=5, retired=frozenset())
    assert board["unavailable"] is False
    assert board["trials"] == 0


def test_home_board_section_says_couldnt_load_not_zero():
    section = home._trials_sections({"trials": {"unavailable": True}})
    assert section["count"] is None
    assert "Couldn't load" in section["leader"]


# --- the model detail page --------------------------------------------------

def test_model_report_is_unavailable_when_the_promotion_read_fails(store):
    _drop(store, "rival_family_promotion")
    report = model_detail.model_report(store, "BTC", "regime_v1", now=NOW)
    assert report is not None and report["unavailable"] is True
    embed = model_detail.model_embed(report, asset="BTC", name="regime_v1")
    assert "Couldn't load" in (embed.description or "")


def test_model_page_never_claims_never_cut_when_the_stop_read_fails(store):
    _promotion(store, "okx", 5, {
        "regime_v1": {"days": 12, "targets": 300, "forward_gain": .004}})
    _drop(store, "rival_family_early_stop")
    report = model_detail.model_report(store, "BTC", "Trend-only", now=NOW)
    assert report is not None and not report.get("unavailable")
    horizon = report["horizons"][5]
    assert horizon["cut"] == 0
    assert horizon["cut_unknown"] is True
    checklist = model_detail._checklist(report)
    assert "cut status unknown" in checklist
    assert "never cut" not in checklist


def test_model_page_still_claims_never_cut_when_the_stop_read_succeeds(store):
    _promotion(store, "okx", 5, {
        "regime_v1": {"days": 12, "targets": 300, "forward_gain": .004}})
    report = model_detail.model_report(store, "BTC", "Trend-only", now=NOW)
    horizon = report["horizons"][5]
    assert horizon["cut"] == 0
    assert horizon["cut_unknown"] is False
    assert "never cut for losing" in model_detail._checklist(report)
