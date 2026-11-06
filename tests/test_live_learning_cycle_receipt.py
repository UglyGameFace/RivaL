"""Host production learning receipt rejects skipped and fabricated health."""
from __future__ import annotations

import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def test_daily_forward_proof_receipt_runs_without_manual_deployment():
    """Only authenticated read-only host proof; no dummy commits to monitor."""
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/live-learning-cycle-receipt.yml"
    ).read_text(encoding="utf-8")
    assert 'cron: "17 10 * * *"' in workflow
    assert '"$GITHUB_EVENT_NAME" == "workflow_run"' in workflow
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
