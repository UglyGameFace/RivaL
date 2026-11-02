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
