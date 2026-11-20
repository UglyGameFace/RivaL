"""Streamed governance evaluation: chunking must never change a result.

FamilyPromotion.evaluate() used to fetchall() one family's whole window
(up to 24,001 rows) before scoring it; it now streams the same query in
FORECAST_FETCH_CHUNK chunks and scores rows as they arrive. These tests
pin the equivalence: identical return value and identical stored evidence
across chunk sizes, the LIMIT probe row still marks input incomplete when
it lands mid-stream, and the stream helper yields exactly the fetchall
rows in the query's order.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from parlay_bot.perpetuals import family_governance
from parlay_bot.perpetuals.family_governance import (
    FamilyPromotion,
    _stream_family_forecasts,
)
from tests.test_family_cost_promotion import _seed

NOW = datetime(2026, 11, 2, 16, tzinfo=UTC)


def _evaluate(store):
    return FamilyPromotion(store).evaluate(
        asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
        horizon=5, now=NOW)


def _stored_evidence(store):
    with store.connect() as conn:
        row = conn.execute(
            "SELECT evidence_json FROM rival_family_promotion").fetchone()
    return None if row is None else row["evidence_json"]


def test_chunk_size_never_changes_evaluation_output(learning_store, monkeypatch):
    family = _seed(learning_store, NOW)  # 168 settled rows, quoted subset
    baseline = _evaluate(learning_store)
    baseline_json = json.dumps(baseline, sort_keys=True)
    baseline_evidence = _stored_evidence(learning_store)
    assert baseline["families"][family]["input_rows"] == 168
    assert baseline["families"][family]["input_complete"]
    for chunk in (1, 3, 7, 2000):
        monkeypatch.setattr(family_governance, "FORECAST_FETCH_CHUNK", chunk)
        result = _evaluate(learning_store)
        assert json.dumps(result, sort_keys=True) == baseline_json
        assert _stored_evidence(learning_store) == baseline_evidence


def test_probe_row_mid_stream_still_marks_input_incomplete(
        learning_store, monkeypatch):
    family = _seed(learning_store, NOW)
    monkeypatch.setattr(family_governance, "MAX_FORECASTS_PER_FAMILY", 167)
    monkeypatch.setattr(family_governance, "FORECAST_FETCH_CHUNK", 7)
    result = _evaluate(learning_store)
    partial = result["families"][family]
    assert partial["input_rows"] == 167
    assert not partial["input_complete"]
    assert not partial["qualified"]
    assert result["status"] == "held"


def test_stream_yields_exactly_the_fetchall_rows_in_order(
        learning_store, monkeypatch):
    _seed(learning_store, NOW)
    monkeypatch.setattr(family_governance, "FORECAST_FETCH_CHUNK", 5)
    sql = """SELECT family,reference_opened_at,issued_at
             FROM rival_model_shadow_forecasts
             ORDER BY target_opened_at,issued_at,reference_opened_at"""

    def shape(rows):
        return [(row["family"], row["reference_opened_at"], row["issued_at"])
                for row in rows]

    with learning_store.connect() as conn:
        expected = shape(conn.execute(sql).fetchall())
    streamed = shape(_stream_family_forecasts(learning_store, sql, ()))
    assert len(expected) == 168
    assert streamed == expected
