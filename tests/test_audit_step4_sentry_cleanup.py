"""Audit step 4: Sentry clean-up.

1. Model-quality alerts (a challenger losing to the incumbent by more than
   the promotion bar — the gate working as designed, e.g. RIVAL-F liq_v1
   phemex:ETH) leave as Sentry warnings under their own fingerprint, not
   as errors; real faults are unchanged.
2. No test events in prod Sentry: the pytest guards in init_sentry /
   error_report.install no longer let an exported DSN defeat them.
3. The RivaL Intelligence monitor is settled off-by-design; the visible
   state (status card says reviews are off) is covered by the existing
   test_status_card.py card_intelligence_off test, and the setup_hook gate
   now logs the off-by-design state once at startup.
"""
from __future__ import annotations

import logging

from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore

REGRESSION = "regression:phemex:ETH:liq_v1"
REGRESSION_DETAIL = ("paired Brier 0.2600 vs incumbent 0.2000, n=120; "
                     "promotion must stay gated")


def test_losing_test_model_raises_no_alert_and_real_faults_stay_errors(
        tmp_path, monkeypatch, caplog):
    """Owner decision (Nov 11): a losing test model is not an alert. It is
    an info line only (no Sentry event, no status-card warning); the
    incident is still recorded, and every other fault still logs error."""
    from parlay_bot import observability

    reported: list = []
    monkeypatch.setattr(observability, "report_model_quality",
                        lambda key, detail: reported.append((key, detail)))
    store = SQLiteHotStore(tmp_path / "step4.sqlite")
    monitor = RuntimeHealthMonitor(store)
    try:
        with caplog.at_level(logging.INFO,
                             logger="parlay_bot.perpetuals.runtime_health"):
            events = monitor.publish(
                {REGRESSION: REGRESSION_DETAIL,
                 "learning_missing": "no valid completed learning cycle",
                 "feed:hyperliquid:BTC": "never attempted"},
                now=monitor.started_at)
        assert events == [("OPEN", "feed:hyperliquid:BTC", "never attempted"),
                          ("OPEN", "learning_missing", "no valid completed learning cycle"),
                          ("OPEN", REGRESSION, REGRESSION_DETAIL)]
        assert reported == []
        by_message = {r.message: r.levelname for r in caplog.records}
        assert by_message[f"RivaL test model losing {REGRESSION}: {REGRESSION_DETAIL}"] == "INFO"
        assert not any(REGRESSION in m and lvl in ("WARNING", "ERROR")
                       for m, lvl in by_message.items())
        assert by_message[
            "RivaL HEALTH ALERT learning_missing: no valid completed learning cycle"] == "ERROR"
        assert by_message["RivaL HEALTH ALERT feed:hyperliquid:BTC: never attempted"] == "WARNING"
        assert any(i["issue_key"] == REGRESSION for i in monitor.snapshot()["issues"])
    finally:
        store.close()
