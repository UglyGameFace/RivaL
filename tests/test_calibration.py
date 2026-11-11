"""Can you trust its confidence? Calibration bands and market conditions.

The rows are the scoreboard's own "decided" rows: main model, last 7 days,
graded, valid scores, not 50/50. A band is judged only with 20+ calls and is
"off" only beyond sampling noise.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.home import _calibration_text
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def monitor(tmp_path):
    store = SQLiteHotStore(tmp_path / "cal.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    audit = ForwardAccuracyMonitor(market)
    audit.initialize()
    yield audit
    store.close()


_SEQ = iter(range(10**6))


def _call(audit, p, up, *, regime=None, horizon=5, model=MODEL, age=timedelta(hours=2),
          settled=True, asset="BTC"):
    i = next(_SEQ)
    issued = NOW - age
    ref = (issued - timedelta(minutes=5) + timedelta(seconds=i)).isoformat()
    outcome = int(up)
    with audit.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit (venue,contract,quote,asset,horizon_minutes,
                 model_version,reference_opened_at,reference_close,issued_at,target_opened_at,
                 probability_up,baseline_up,training_evaluated,settled_at,outcome_up,
                 realized_close,model_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("okx", f"{asset}-USDT", "USDT", asset, horizon, model, ref, 100.0,
             issued.isoformat(), issued.isoformat(), p, .5, 1,
             issued.isoformat() if settled else None, outcome if settled else None, 101.0,
             (p - outcome) ** 2, .25))
        if regime:
            conn.execute(
                """INSERT INTO perp_forward_regimes (venue,contract,quote,horizon_minutes,
                     model_version,reference_opened_at,issued_at,method_version,regime,
                     five_min_volatility_pct,feature_sha256,weights_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("okx", f"{asset}-USDT", "USDT", horizon, model, ref, issued.isoformat(),
                 "v1", regime, .1, "f" * 64, "w" * 64))


def test_bands_count_only_the_scoreboards_decided_rows(monitor):
    for _ in range(30):
        _call(monitor, .58, True, regime="quiet")      # said 58%, came true
    for _ in range(10):
        _call(monitor, .42, True, regime="quiet")      # said 58% DOWN, came up: wrong
    # Not counted: 50/50, unsettled, another model, too old, another coin/timeframe.
    _call(monitor, .5, True)
    _call(monitor, .7, True, settled=False)
    _call(monitor, .7, True, model="other-model")
    _call(monitor, .7, True, age=timedelta(days=8))
    _call(monitor, .7, True, asset="ETH")
    _call(monitor, .7, True, horizon=15)
    result = monitor.calibration(asset="BTC", horizon=5, now=NOW)
    band = result["bands"][1]
    assert (band["low"], band["high"], band["n"], band["came_true"]) == (.55, .60, 40, 30)
    assert band["said"] == pytest.approx(.58) and band["happened"] == pytest.approx(.75)
    assert sum(b["n"] for b in result["bands"]) == 40
    quiet = result["regimes"]["quiet"]
    assert (quiet["n"], quiet["came_true"]) == (40, 30)
    assert quiet["baseline_brier"] == pytest.approx(.25)


def test_a_call_with_no_recorded_condition_is_unknown_not_dropped(monitor):
    _call(monitor, .7, True)
    assert monitor.calibration(asset="BTC", horizon=5, now=NOW)["regimes"]["unknown"]["n"] == 1


def _record(bands, regimes=None):
    out = []
    for (low, high), (n, said, came) in zip(ForwardAccuracyMonitor.CALIBRATION_BANDS, bands):
        out.append({"low": low, "high": high, "n": n, "said": said if n else None,
                    "came_true": came, "happened": came / n if n else None})
    return {"calibration": {"bands": out, "regimes": regimes or {}}}


def test_honest_confidence_within_noise():
    text = _calibration_text(_record([(1000, .53, 520), (300, .57, 165), (5, .62, 3),
                                      (0, 0, 0)]))
    assert text.startswith("✅ **Its confidence is honest:**")
    assert "too few" in text and "1,000" in text


def test_overconfident_beyond_noise_says_to_treat_chances_as_smaller():
    text = _calibration_text(_record([(2000, .54, 1000), (800, .58, 400), (0, 0, 0),
                                      (0, 0, 0)]))
    assert text.startswith("⚠️ **Overconfident:** it says 55% on average but is right only 50%")


def test_underconfident_beyond_noise():
    text = _calibration_text(_record([(2000, .52, 1200), (0, 0, 0), (0, 0, 0), (0, 0, 0)]))
    assert text.startswith("**Underconfident:**")


def test_too_few_calls_is_never_a_verdict():
    text = _calibration_text(_record([(19, .53, 19), (3, .57, 0), (0, 0, 0), (0, 0, 0)]))
    assert text == "Too few finished calls in the last 7 days to check its confidence yet."


def test_failed_read_says_so():
    assert _calibration_text({"calibration": TimeoutError()}).startswith("Couldn't load")
    assert _calibration_text({}) == "Not available on this server."


def test_market_conditions_table_names_the_best_and_hides_small_samples():
    regimes = {
        "quiet": {"n": 700, "came_true": 380, "hit_rate": 380 / 700, "brier": .247,
                  "baseline_brier": .249},
        "turbulent": {"n": 12, "came_true": 9, "hit_rate": .75, "brier": .2,
                      "baseline_brier": .25},
    }
    text = _calibration_text(_record([(712, .53, 389), (0, 0, 0), (0, 0, 0), (0, 0, 0)],
                                     regimes))
    table = text.split("```")[3].strip().splitlines()
    assert table[1].split() == ["Quiet", "54.3%", "+2.0", "700"]
    assert table[2].split() == ["Turbulent", "few", "—", "12"]   # 12 calls: no verdict
    assert "Best in **quiet** markets" in text and "a clue, not proof" in text
    assert max(len(line) for line in table) <= 38
