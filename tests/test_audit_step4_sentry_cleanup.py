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

from parlay_bot.perpetuals import runtime_health
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore

REGRESSION = "regression:phemex:ETH:liq_v1"
REGRESSION_DETAIL = ("paired Brier 0.2600 vs incumbent 0.2000, n=120; "
                     "promotion must stay gated")


def test_regression_alert_is_a_warning_and_real_faults_stay_errors(
        tmp_path, monkeypatch, caplog):
    """Item 1: publish() routes regression:* OPEN transitions to warning +
    report_model_quality, and every other OPEN transition still logs error."""
    reported: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime_health, "report_model_quality",
                        lambda key, detail: reported.append((key, detail)))
    store = SQLiteHotStore(tmp_path / "step4.sqlite")
    monitor = RuntimeHealthMonitor(store)
    try:
        with caplog.at_level(logging.WARNING,
                             logger="parlay_bot.perpetuals.runtime_health"):
            events = monitor.publish(
                {REGRESSION: REGRESSION_DETAIL,
                 "feed:hyperliquid:BTC": "never attempted"},
                now=monitor.started_at)
        assert events == [("OPEN", "feed:hyperliquid:BTC", "never attempted"),
                          ("OPEN", REGRESSION, REGRESSION_DETAIL)]
        assert reported == [(REGRESSION, REGRESSION_DETAIL)]
        by_message = {r.message: r.levelname for r in caplog.records}
        assert by_message[f"RivaL HEALTH ALERT {REGRESSION}: {REGRESSION_DETAIL}"] == "WARNING"
        assert by_message["RivaL HEALTH ALERT feed:hyperliquid:BTC: never attempted"] == "ERROR"
    finally:
        store.close()


def test_regression_alert_is_not_an_error_anywhere_in_the_log(
        tmp_path, monkeypatch, caplog):
    """Item 1: no ERROR record at all for a regression-only publish, so the
    logging integration cannot also create an error-level Sentry event."""
    monkeypatch.setattr(runtime_health, "report_model_quality",
                        lambda key, detail: None)
    store = SQLiteHotStore(tmp_path / "step4-only.sqlite")
    monitor = RuntimeHealthMonitor(store)
    try:
        with caplog.at_level(logging.WARNING,
                             logger="parlay_bot.perpetuals.runtime_health"):
            monitor.publish({REGRESSION: REGRESSION_DETAIL},
                            now=monitor.started_at)
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []
        assert len(caplog.records) == 1
    finally:
        store.close()
