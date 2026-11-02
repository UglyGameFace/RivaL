"""Test the actual LFM3 evidence boundary, not retrospective price guesses."""
import json

import pytest

from tools.discloud_llm_edge_audit import MARKER, MODEL, evaluate, parse_receipt


def source(direction="uncertain",count=1):
    return {"asset":"BTC","direction":direction,"count":count,"latest":"2026-11-01T23:41:27+00:00"}

def receipt(rows=None):
    return {"schema":"llm-edge-v1","model":MODEL,"provider":"local",
            "background_opt_in":False,"auto_ai_setting":False,"rows":[] if rows is None else rows,
            "cycle":{"math_issued":108,"math_settled":120}}

def test_one_saved_llm_reply_is_not_trading_edge():
    result=evaluate(receipt([source()]))
    assert result["saved_research_count"]==1
    assert result["reason"]=="all_outputs_abstained"
    assert result["llm_forward_brier"] is None
    assert not result["llm_after_cost_edge_verified"]
    assert not result["halt_historical_backfill"]

def test_directional_research_without_target_still_cannot_be_scored():
    result=evaluate(receipt([source("up",100),source("down",90)]))
    assert result["directional_research_count"]==190
    assert result["reason"]=="research_directions_have_no_immutable_future_target"
    assert result["paired_llm_forecast_count"] is None

def test_no_saved_research_and_database_failure_are_explicit():
    assert evaluate(receipt())["reason"]=="no_saved_llm_research"
    report=receipt()
    report["error"]="database_read_failed"
    assert evaluate(report)["status"]=="unverified"

@pytest.mark.parametrize("bad",[[source("buy")],[source(count=0)],
                          [{"asset":"ADA","direction":"up","count":1}],
                          [source(count=True)],[source()]*33])
def test_invalid_rows_never_make_success(bad):
    with pytest.raises(ValueError):
        evaluate(receipt(bad))

def test_single_authentic_model_receipt_required():
    line=MARKER+json.dumps(receipt([source()]))
    assert evaluate(parse_receipt("debug\n"+line))["saved_research_count"]==1
    with pytest.raises(ValueError):
        parse_receipt(line+"\n"+line)
    with pytest.raises(ValueError):
        parse_receipt("unrelated log")
    with pytest.raises(ValueError):
        parse_receipt(MARKER+json.dumps({**receipt(),"model":"wrong"}))

def test_enabling_local_background_flag_does_not_actually_enable_calls():
    data=receipt([source()])
    data["background_opt_in"]=True
    assert not evaluate(data)["background_ai_enabled"]
    data["provider"]="openrouter"
    assert not evaluate(data)["background_ai_enabled"]
    data["auto_ai_setting"]=True
    assert evaluate(data)["background_ai_enabled"]
