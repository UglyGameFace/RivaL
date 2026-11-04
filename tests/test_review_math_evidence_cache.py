"""Intelligence reviews reuse the costly 14-day score briefly, never across clocks."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from parlay_bot.perpetuals.analysis_evidence import _prospective_math_evidence

T0 = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)


def test_score_reused_for_ten_minutes_and_never_for_an_earlier_clock():
    calls = []

    class Auditor:
        def active_learning_evidence(self, *, asset, horizon_minutes, now):
            calls.append(now)
            return {"status": "insufficient", "targets": len(calls), "days": 1,
                    "brier": 0.25, "baseline_brier": 0.25}

    market = SimpleNamespace(research_forward_auditor=Auditor())
    first = _prospective_math_evidence(market, asset="BTC", clock=T0)
    again = _prospective_math_evidence(market, asset="BTC", clock=T0 + timedelta(minutes=4))
    assert first == again and len(calls) == 1  # reused within 10 minutes
    other = _prospective_math_evidence(market, asset="ETH", clock=T0 + timedelta(minutes=4))
    assert len(calls) == 2 and other["settled_targets"] == 2  # per asset
    earlier = _prospective_math_evidence(market, asset="BTC", clock=T0 - timedelta(minutes=1))
    assert len(calls) == 3 and earlier["settled_targets"] == 3  # never from a later clock
    later = _prospective_math_evidence(market, asset="BTC", clock=T0 + timedelta(minutes=11))
    assert len(calls) == 4 and later["settled_targets"] == 4  # expired: recomputed
