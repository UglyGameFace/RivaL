"""Host production learning receipt rejects skipped and fabricated health."""
from __future__ import annotations

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from parlay_bot.perpetuals import learning_health_receipt as host

RELAY = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "tools/discloud_learning_cycle_receipt.py"
))


def test_live_host_status_fresh_stale_missing_and_unavailable():
    now = datetime.now(UTC)
    valid = {
        "status": "recorded", "checked_at": now - timedelta(minutes=4),
        "issued": 8, "expired": 2,
    }
    result = host._status(valid, now)
    assert result == {"status": "fresh", "age_seconds": 240,
                      "issued": 8, "expired": 2}
    assert host._status(None, now) == {"status": "not_recorded"}
    assert host._status({"status": "unavailable"}, now) == {
        "status": "read_unavailable",
    }
    old = {**valid, "checked_at": now - timedelta(minutes=25)}
    assert host._status(old, now)["status"] == "stale_or_clock_invalid"
    future = {**valid, "checked_at": now + timedelta(seconds=2)}
    assert host._status(future, now)["status"] == "stale_or_clock_invalid"


def test_relay_must_observe_exact_fresh_status_and_successful_stages():
    valid = {
        "outcome": "verified", "forecast": {"status": "fresh"},
        "learning": {
            "status": "fresh", "math_ok": True, "model_review_ok": True,
        },
        "does_not_prove_prediction_edge": True,
    }
    marker = RELAY["MARKER"]
    read = RELAY["parse_receipt"](
        "unrelated output\n" + marker
        + __import__("json").dumps(valid) + "\n"
    )
    assert RELAY["accepted"](read)
    for changed in (
        {"outcome": "unverified"},
        {"forecast": {"status": "read_unavailable"}},
        {"learning": {"status": "not_recorded"}},
        {"learning": {"status": "fresh", "math_ok": False,
                      "model_review_ok": True}},
        {"does_not_prove_prediction_edge": False},
    ):
        assert not RELAY["accepted"]({**valid, **changed})
    import pytest
    with pytest.raises(ValueError, match="missing"):
        RELAY["parse_receipt"]("no structured receipt")
    with pytest.raises(ValueError, match="invalid cycle status"):
        RELAY["parse_receipt"](
            marker + __import__("json").dumps({
                **valid, "forecast": {"status": "unknown"},
            })
        )


def test_on_host_read_uses_production_reader_no_writes(monkeypatch):
    now = datetime.now(UTC)
    class Store:
        def __init__(self, _dsn):
            self.closed = False

        def close(self):
            self.closed = True

    store = Store("fake")
    monkeypatch.setattr(host, "Settings", lambda: SimpleNamespace(
        rival_storage_backend="postgres",
        rival_database_url=SimpleNamespace(get_secret_value=lambda: "fake"),
    ))
    monkeypatch.setattr(host, "PostgresHotStore", lambda _: store)
    watchdog = {"status": "fresh", "operational_status": "healthy"}
    monkeypatch.setattr(host, "read_xmarket_watchdog", lambda *_args: watchdog)
    quotes = {"status": "recorded", "read_only": True}
    monkeypatch.setattr(host, "read_xmarket_quotes", lambda *_args: quotes)
    monkeypatch.setattr(host, "_performance_cycles", lambda market: (
        {"forecast": {
            "status": "recorded", "checked_at": now,
            "issued": 10, "expired": 0,
        },
         "learning": {
             "status": "recorded", "checked_at": now,
             "issued": 8, "settled": 5,
             "math_ok": True, "model_review_ok": True,
         }}
    ))
    report = host.read_receipt()
    assert report["outcome"] == "verified"
    assert report["forecast"]["issued"] == 10
    assert report["learning"]["settled"] == 5
    assert report["xmarket_watchdog"] == watchdog
    assert report["xmarket_quote_progress"] == quotes
    assert store.closed


def test_host_forward_proof_reports_one_real_exact_source_without_mixing(monkeypatch):
    now = datetime(2026, 11, 3, 3, tzinfo=UTC)
    from parlay_bot.perpetuals.family_quotes import SUPPORTED_QUOTE_SOURCES

    assert SUPPORTED_QUOTE_SOURCES == {
        ("hyperliquid", "USDC"), ("binance", "USDT"),
    }
    def candidate(*, targets, days, coverage, complete):
        return {
            "valid": True, "targets": targets, "days": days,
            "eligible_quotes": 160,
            "closed_quotes": int(160 * coverage),
            "quote_coverage": coverage,
            "checks": {
                "future_outcomes": days >= 21 and targets >= 150,
                "beats_baselines": complete,
                "quote_coverage": coverage >= .8,
                "after_cost_paper": complete,
                "valid_records": True,
            },
        }

    def fake(_store, *, asset, venue, now):
        assert now == datetime(2026, 11, 3, 3, tzinfo=UTC)
        if venue == "hyperliquid" and asset == "BTC":
            return {"status": "recorded", "evaluations": [
                {"quote": "USDC", "horizon": 5, "fresh": True,
                 "candidate": candidate(targets=150, days=21, coverage=.75,
                                        complete=False), "paper_qualified": False},
            ]}
        if venue == "binance" and asset == "XRP":
            return {"status": "recorded", "evaluations": [
                {"quote": "USDT", "horizon": 15, "fresh": True,
                 "candidate": candidate(targets=165, days=22, coverage=.9,
                                        complete=True), "paper_qualified": True},
            ]}
        return {"status": "no_evaluations", "evaluations": []}

    monkeypatch.setattr(host, "proof_progress", fake)
    report = host._future_proof(object(), now)
    assert report["status"] == "recorded"
    assert report["evaluated_scopes"] == 2
    assert report["qualified_paper_scopes"] == 1
    assert report["best_exact_source"] == {
        "asset": "XRP", "venue": "binance", "quote": "USDT",
        "horizon_minutes": 15, "fresh": True, "targets": 165,
        "days": 22, "eligible_quotes": 160, "closed_quotes": 144,
        "quote_coverage": .9, "qualified_paper": True,
        "checks": {
            "future_outcomes": True, "beats_baselines": True,
            "quote_coverage": True, "after_cost_paper": True,
            "valid_records": True,
        },
    }
    assert report["does_not_prove_real_fills"]


def test_host_proof_fails_closed_on_missing_and_invalid_evaluations(monkeypatch):
    stamp = datetime(2026, 11, 3, 3, tzinfo=UTC)
    monkeypatch.setattr(host, "proof_progress", lambda *_args, **_kwargs: {
        "status": "no_evaluations", "evaluations": [],
    })
    empty = host._future_proof(object(), stamp)
    assert empty["status"] == "no_evaluations"
    assert empty["qualified_paper_scopes"] == 0
    monkeypatch.setattr(host, "proof_progress", lambda *_args, **_kwargs: {
        "status": "unavailable", "evaluations": [],
    })
    assert host._future_proof(object(), stamp)["status"] == "unavailable"
    monkeypatch.setattr(host, "proof_progress", lambda *_args, **_kwargs: {
        "status": "recorded", "evaluations": [{
            "quote": "USDC", "horizon": 5, "fresh": False,
            "candidate": {"valid": False}, "paper_qualified": False,
        }],
    })
    assert host._future_proof(object(), stamp)["qualified_paper_scopes"] == 0



def test_host_receipt_exposes_xmarket_own_cost_gates_not_top_family(monkeypatch):
    """Source-specific XMarket proof must not inherit another model's scores."""
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    other = {
        "valid": True, "targets": 500, "days": 23,
        "eligible_quotes": 400, "closed_quotes": 398,
        "quote_coverage": .995,
        "checks": {"future_outcomes": True, "beats_baselines": True,
                   "quote_coverage": True, "after_cost_paper": True,
                   "valid_records": True},
    }
    xmarket = {
        "name": "xmarket_v1", "valid": True, "qualified_paper": False,
        "targets": 40, "days": 1, "eligible_quotes": 27,
        "closed_quotes": 0, "quote_coverage": 0.0,
        "missing_quote_windows": 27,
        "checks": {"future_outcomes": False, "beats_baselines": False,
                   "quote_coverage": False, "after_cost_paper": False,
                   "valid_records": True},
    }

    def fake(_store, *, asset, venue, now):
        if asset == "BTC" and venue == "hyperliquid":
            return {"status": "recorded", "evaluations": [{
                "quote": "USDC", "horizon": 15, "fresh": True,
                "candidate": other, "paper_qualified": False,
                "xmarket_v1": xmarket,
            }]}
        if asset == "SOL" and venue == "binance":
            return {"status": "recorded", "evaluations": [{
                "quote": "USDT", "horizon": 60, "fresh": False,
                "candidate": other, "paper_qualified": False,
                "xmarket_v1": {"name": "xmarket_v1", "valid": False,
                               "qualified_paper": False},
            }]}
        return {"status": "no_evaluations", "evaluations": []}

    monkeypatch.setattr(host, "proof_progress", fake)
    report = host._future_proof(object(), now)
    assert report["best_exact_source"]["targets"] == 500
    x = report["xmarket_v1"]
    assert x["status"] == "recorded"
    assert x["evaluated_scopes"] == 2
    assert x["fresh_scopes"] == 1
    assert x["qualified_paper_scopes"] == 0
    valid, invalid = x["scopes"]
    assert (valid["asset"], valid["venue"], valid["horizon_minutes"]) == (
        "BTC", "hyperliquid", 15,
    )
    assert valid["eligible_quotes"] == 27
    assert valid["closed_quotes"] == 0
    assert valid["missing_quote_windows"] == 27
    assert not valid["checks"]["quote_coverage"]
    assert not valid["qualified_paper"]
    assert invalid["status"] == "invalid_saved_evidence"
    assert invalid["fresh"] is False
    assert "targets" not in invalid
    assert x["does_not_prove_real_fills"] is True
    # No raw contract, weights, source price or private evidence JSON.
    assert "evidence_json" not in json.dumps(x)
    assert "model_sha256" not in json.dumps(x)


def test_xmarket_source_cost_receipt_never_claims_missing_evidence(monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    monkeypatch.setattr(host, "proof_progress", lambda *_args, **_kw: {
        "status": "no_evaluations", "evaluations": [],
    })
    empty = host._future_proof(object(), now)
    assert empty["xmarket_v1"]["status"] == "no_evaluations"
    assert empty["xmarket_v1"]["qualified_paper_scopes"] == 0
    monkeypatch.setattr(host, "proof_progress", lambda *_args, **_kw: {
        "status": "unavailable", "evaluations": [],
    })
    unknown = host._future_proof(object(), now)
    assert unknown["xmarket_v1"]["status"] == "unavailable"
    assert unknown["xmarket_v1"]["scopes"] == []


def test_daily_forward_proof_receipt_runs_without_manual_deployment():
    """Only authenticated read-only host proof; no dummy commits to monitor."""
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/live-learning-cycle-receipt.yml"
    ).read_text(encoding="utf-8")
    assert 'cron: "17 10 * * *"' in workflow
    assert '"$GITHUB_EVENT_NAME" == "workflow_run"' in workflow
    assert "Verify native Discloud source parity" in workflow
    assert "Deploy tested RivaL app" not in workflow
    assert "if: steps.gate.outputs.check == 'true'" in workflow
    assert "python tools/discloud_learning_cycle_receipt.py" in workflow


def test_contest_receipt_reads_real_new_policy_rows_without_writing(learning_store):
    from parlay_bot.perpetuals.model_league import FORMULA
    from tests.test_model_league import SHA, START, notice, setup

    c = setup(learning_store)
    n = notice()
    assert c.capture(n, {FORMULA: (.8,SHA)}, now=n.quote_at)
    c.display_call(n.slug, None)
    report = host._contest_progress(learning_store, START+timedelta(hours=1))
    assert report['status'] == 'recorded'
    assert report['captured'] == report['with_forecasts'] == 1
    assert report['scopes'][0]['formula_forecasts'] == 1
    assert report['scopes'][0]['pinned_for_display'] == 1
    assert report['does_not_prove_prediction_edge'] is True
    with learning_store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM rival_poly_contest_rounds').fetchone()['n'] == 1
    assert host._contest_progress(object(), START)['status'] == 'unavailable'


def test_actual_learning_receipt_requires_changed_weights_and_new_labels(learning_store):
    """A healthy cycle heartbeat alone must not be mistaken for weight updates."""
    from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.perpetuals.model_lab import LEARNER_SCHEMA

    store = learning_store
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    now = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    with store.connect() as conn:
        conn.execute(LEARNER_SCHEMA)
        conn.execute(
            "INSERT INTO rival_lab_learners "
            "(venue,contract,quote,horizon_minutes,family,weights_json,cursor,updates,trained_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("hyperliquid", "BTC", "USDC", 5, "lab_5m_test",
             "[0.1,0.2]", None, 47, (now-timedelta(minutes=7)).isoformat()),
        )
        for minutes, updates, digest in ((30, 80, "a"), (10, 84, "b")):
            stamp = now - timedelta(minutes=minutes)
            conn.execute(
                """INSERT INTO rival_forward_model_lineage (
                     venue,contract,quote,horizon_minutes,model_version,
                     reference_opened_at,issued_at,weights_sha256,
                     feature_sha256,training_labels,historical_training_labels,
                     online_training_updates,historical_seed_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", 5, MODEL,
                 (stamp-timedelta(minutes=5)).isoformat(), stamp.isoformat(),
                 digest*64, "c"*64, updates, 0, updates, None),
            )
    result = host._actual_learning_progress(store, now)
    assert result["status"] == "weight_updates_observed"
    assert result["sampled_forecasts"] == 2
    assert result["streams_with_before_after"] == 1
    assert result["streams_with_verified_weight_updates"] == 1
    assert result["latest_online_training_updates"] == 84
    assert result["sampled_historically_seeded_forecasts"] == 0
    assert result["lab"]["streams_with_training_updates"] == 1
    assert result["proves_improvement"] is False
    assert result["proves_profitable_edge"] is False

    # Counter alone isn't enough. Persisted probabilities can stay unchanged.
    with store.connect() as conn:
        conn.execute(
            "UPDATE rival_forward_model_lineage SET weights_sha256=? "
            "WHERE online_training_updates=84",
            ("a"*64,),
        )
    assert host._actual_learning_progress(store, now)["status"] == (
        "no_recent_weight_change_proven")


def test_actual_learning_receipt_reports_absent_lineage_without_inventing_progress(
    learning_store,
):
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    result = host._actual_learning_progress(
        learning_store, datetime(2026, 11, 12, 16, tzinfo=UTC))
    assert result["status"] == "no_lineage"
    assert result["lab"]["status"] == "not_initialized"
    assert result["streams_with_verified_weight_updates"] == 0
    assert result["proves_improvement"] is False


def test_read_only_xmarket_diagnostic_marker_is_required_and_sanitized():
    value = {"status": "recorded", "scopes": [],
             "does_not_prove_prospective_predictions": True, "read_only": True}
    stdout = RELAY["XMARKET_MARKER"] + json.dumps(value)
    assert RELAY["parse_xmarket_probe"](stdout) == value
    for invalid in (
        {},
        {**value, "read_only": False},
        {**value, "does_not_prove_prospective_predictions": False},
        {**value, "status": "verified"},
    ):
        with pytest.raises(ValueError):
            RELAY["parse_xmarket_probe"](
                RELAY["XMARKET_MARKER"] + json.dumps(invalid))
    with pytest.raises(ValueError, match="missing XMarket diagnostic"):
        RELAY["parse_xmarket_probe"]("no diagnostic")

    assert "xmarket_shadow_diagnostic import main as shadow_probe" in RELAY["CMD"]
    assert "main();shadow_probe()" in RELAY["CMD"]



def test_xmarket_proof_handoff_marks_unevaluated_issued_scopes_unknown():
    """Five live fits do not imply five supported-source paper evaluations."""
    progress = {
        "status": "shadow_issuing",
        "scope_progress": [
            {"asset": asset, "horizon_minutes": horizon,
             "stored_gate_passing_artifacts": 1, "shadow_rows_24h": 12}
            for asset, horizon in (
                ("BTC", 15), ("BTC", 60), ("SOL", 15),
                ("XRP", 15), ("XRP", 60),
            )
        ] + [{
            "asset": "ETH", "horizon_minutes": 15,
            "stored_gate_passing_artifacts": 0, "shadow_rows_24h": 0,
        }],
    }
    proof = {
        "status": "recorded",
        "scopes": [
            {"asset": "SOL", "horizon_minutes": 15, "venue": "hyperliquid",
             "status": "recorded", "fresh": True, "qualified_paper": False,
             "checks": {"quote_coverage": False}},
            {"asset": "XRP", "horizon_minutes": 15, "venue": "hyperliquid",
             "status": "recorded", "fresh": True, "qualified_paper": False,
             "checks": {"quote_coverage": False}},
            {"asset": "XRP", "horizon_minutes": 60, "venue": "hyperliquid",
             "status": "recorded", "fresh": True, "qualified_paper": False,
             "checks": {"quote_coverage": False}},
        ],
    }
    result = host._xmarket_forward_handoff(progress, proof)
    assert result["fitted_scopes"] == 5
    assert result["issuing_scopes"] == 5
    assert result["issuing_scopes_with_fresh_source_evaluation"] == 3
    assert result["issuing_scopes_missing_supported_source_evaluation"] == 2
    btc = [row for row in result["scopes"] if row["asset"] == "BTC"]
    assert len(btc) == 2
    assert all(row["status"] == "no_supported_source_evaluation"
               and row["source_evaluations"] == 0
               and row["quote_gate_passing_sources"] == 0 for row in btc)
    assert all(row["qualified_paper_sources"] == 0 for row in result["scopes"])
    assert result["does_not_prove_edge"] is True
    assert "quote_coverage" not in json.dumps(btc)


def test_xmarket_proof_handoff_fails_closed_for_stale_invalid_and_missing():
    fitted = {
        "status": "shadow_issuing",
        "scope_progress": [
            {"asset": "BTC", "horizon_minutes": 15,
             "stored_gate_passing_artifacts": 1, "shadow_rows_24h": 3},
            {"asset": "BTC", "horizon_minutes": 60,
             "stored_gate_passing_artifacts": 1, "shadow_rows_24h": 0},
        ],
    }
    stale = {"status": "recorded", "scopes": [
        {"asset": "BTC", "horizon_minutes": 15,
         "fresh": False, "status": "recorded", "qualified_paper": True,
         "checks": {"quote_coverage": True}},
    ]}
    report = host._xmarket_forward_handoff(fitted, stale)
    assert report["issuing_scopes_with_fresh_source_evaluation"] == 0
    assert report["scopes"][0]["status"] == "stale_or_invalid_source_evaluation"
    assert report["scopes"][0]["qualified_paper_sources"] == 0
    assert report["scopes"][1]["status"] == "not_issuing"
    assert host._xmarket_forward_handoff(
        {"status": "read_unavailable"}, stale
    ) == {"status": "unavailable", "does_not_prove_edge": True}
    assert host._xmarket_forward_handoff(
        fitted, {"status": "unavailable"}
    ) == {"status": "unavailable", "does_not_prove_edge": True}
