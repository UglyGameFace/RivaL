"""Bot Performance: verdict first, every model on trial, failure-isolated health."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from tests.test_guided_home import _client, _home


def _score(*, brier: float = .31, baseline: float = .24) -> dict:
    return {
        "last_7d": {5: {
            "decided": 7, "correct": 3, "hit_rate": 3 / 7,
            "issued": 8, "settled": 7, "scored": 6,
            "brier": brier, "baseline_brier": baseline,
        }},
    }


def _fields(embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


def _make_home(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    now = datetime.now(UTC)
    home.market = SimpleNamespace(
        feed_health=lambda: [{
            "asset": "BTC", "venue": "hyperliquid", "status": "ok",
            "last_success_at": now.isoformat(),
            "last_candle_close": (now-timedelta(minutes=5)).isoformat(),
        }],
        store=bot.store,
    )
    home.venues = ("hyperliquid",)
    home.polymarket_status = lambda *_: {
        "settled_forward_contracts": 300,
        "learned_contracts": 402,
        "score_window_days": 7,
        "forward_brier": .2387,
        "market_baseline_brier": .2222,
    }
    home.navigate("performance")
    return bot, home


async def _close(bot, home):
    home.stop()
    await bot.zip_resolver.aclose()
    await bot.public_quotes.aclose()
    bot.store.close()



def _patch_live_reads(monkeypatch, *, trials=None, **_ignored):
    """The page's readers, with quiet defaults; tests override what they check."""
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_: {
        "status": "insufficient", "targets": 198, "days": 2,
    })
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {"status": "recorded", "checked_at": now, "issued": 20, "expired": 0},
        "learning": {"status": "recorded", "checked_at": now, "math_ok": True,
                     "model_review_ok": True, "issued": 5, "settled": 4},
    })
    monkeypatch.setattr(home_module, "_trial_board_record", lambda *_: trials or {
        "rows": [], "markets": 0, "trials": 0, "passed": 0, "stops": 0})
    monkeypatch.setattr(home_module, "_calibration_record", lambda *_: {
        "bands": [], "regimes": {}, "window_days": 7})


SCORE = "🎯 Scoreboard (last 7 days)"
HEALTH = "⚙️ Running and learning"


def _trials_field(fields):
    return next(v for k, v in fields.items() if k.startswith("🧪 Models on trial"))


@pytest.mark.asyncio
async def test_page_is_verdict_first_then_scoreboard_trials_and_health(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)

    def heavy_training(*_):
        raise AssertionError("full Drive/model summary must not run on Performance")

    home.training_status = heavy_training
    _patch_live_reads(monkeypatch)
    try:
        panel = await home.render()
        fields = _fields(panel)
        assert list(fields) == [SCORE, "🎚️ Can you trust its confidence?",
                                "🧪 Models on trial here (0)", HEALTH]
        # Nothing on trial and no lab yet: no empty "best model" section.
        # The verdict is the first line; honesty disclosures stay.
        assert panel.description.startswith(
            "**No edge yet: Polymarket's odds are still more accurate.**")
        assert "predictions saved before the result was known" in panel.description
        assert "Does it make money? Not proven yet" in panel.description
        score = fields[SCORE]
        assert "**vs Polymarket odds:** behind · error 0.239 vs 0.222 · 300 contracts" in score
        # 7 decided is under 20: the rate is withheld, the counts stay.
        assert "too few to judge — 7 graded" in score and "(3 of 7)" in score
        # The fixture's 7 decided vs 6 scored is caught, never shown as fine.
        assert "These numbers don't add up" in score
        assert "vs a coin flip: behind (0.310 vs 0.240)" in score
        assert "7 of 8 predictions checked" in score and "main model only" in score
        health = fields[HEALTH]
        assert "✅ learning just now" in health and "✅ predictions just now" in health
        assert "✅ 1/1 exchanges live" in health
        assert "**Getting more accurate?** too early to tell" in health
        assert "No test models have scored results here yet." in _trials_field(fields)
        assert max(map(len, fields.values())) < 1024
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_slow_or_broken_reads_say_so_and_hide_nothing_else(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("learning read failure")))
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {"status": "recorded", "checked_at": now, "issued": 7, "expired": 0},
        "learning": {"status": "unavailable"},
    })
    monkeypatch.setattr(home_module, "_trial_board_record", lambda *_:
                        (_ for _ in ()).throw(TimeoutError("slow")))

    def bad_feeds():
        raise RuntimeError("feed read failure")

    home.market.feed_health = bad_feeds
    try:
        fields = _fields(await home.render())
        assert "vs a coin flip: behind" in fields[SCORE]
        assert "**vs Polymarket odds:** behind" in fields[SCORE]
        health = fields[HEALTH]
        assert "**Getting more accurate?** couldn't check right now" in health
        assert "learning loop: couldn't check" in health
        assert "feeds: couldn't check" in health
        assert "✅ predictions just now" in health
        assert "Couldn't load the trial table right now" in fields["🧪 Models on trial"]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_a_timed_out_improvement_check_says_it_is_still_calculating(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_:
                        (_ for _ in ()).throw(TimeoutError("independent score unavailable")))
    try:
        assert "still calculating" in _fields(await home.render())[HEALTH]
    finally:
        await _close(bot, home)


def _promotion(store, *, venue, asset="BTC", horizon=5, evidence, at=None):
    from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA

    with store.connect() as conn:
        conn.executescript(FAMILY_PROMOTION_SCHEMA)
        conn.execute(
            """INSERT INTO rival_family_promotion (venue,contract,quote,asset,
                 horizon_minutes,evaluated_at,selected_family,status,reason,evidence_json)
               VALUES (?,?,?,?,?,?,NULL,'held','test',?)""",
            (venue, f"{asset}-{venue}", "USDT", asset, horizon,
             (at or datetime.now(UTC)).isoformat(), __import__("json").dumps(evidence)))


def _evidence(days, targets, gain, *, bps=None, qualified=False):
    info = {"days": days, "targets": targets, "forward_gain": gain, "qualified": qualified}
    if bps is not None:
        info["cost_proof"] = {"severe": {"paired_gain_bps": bps, "windows": targets}}
    return info


@pytest.mark.asyncio
async def test_every_model_on_trial_is_one_row_from_the_hourly_check(tmp_path, monkeypatch):
    """The table reads the promotion check itself (every family it scored),
    folds the exchanges into one row per model, and marks cut models."""
    from parlay_bot.perpetuals.early_stop import EarlyStop

    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_trial_board_record", _real_trial_board_record)
    for venue, gain in (("okx", .004), ("bybit", .002)):
        _promotion(bot.store, venue=venue, evidence={
            "regime_v1": _evidence(12, 300, gain, bps=-.5),
            "pooled_5m_1a10acfb6945": _evidence(12, 649, -.002),
            "cross_v1": _evidence(1, 9, .01),
            "liq_v1": _evidence(11, 581, -.06),
        })
    # Another market counts toward "all markets" but not this table.
    _promotion(bot.store, venue="okx", asset="ETH", evidence={"regime_v1": _evidence(3, 30, 0)})
    stop = EarlyStop(bot.store)
    stop.initialize()
    with bot.store.connect() as conn:
        for venue in ("okx", "bybit"):
            conn.execute(
                """INSERT INTO rival_family_early_stop (venue,contract,quote,horizon_minutes,
                     family,stopped_at,rule_version,targets,days,model_brier,incumbent_brier,
                     loss,loss_lower) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, f"BTC-{venue}", "USDT", 5, "liq_v1", datetime.now(UTC).isoformat(),
                 "test", 581, 11, .34, .26, .06, .05))
    try:
        fields = _fields(await home.render())
        name, value = next((k, v) for k, v in fields.items() if k.startswith("🧪"))
        assert name == "🧪 Models on trial here (4)"
        from tests.test_trial_board import _grouped

        rows = _grouped(value)
        assert [words[0] for _, words in rows] == [
            "Trend-only", "Cross-exch", "6-year", "Liquidity"]
        # No graded calls stored in this test: the Right column says so ("—").
        assert rows[0] == ("ahead", ["Trend-only", "12/21", "300", "—", "+3.0"])
        assert rows[1][0] == "early"     # 9 results is too few to judge
        assert rows[3][0] == "cut"       # cut on every exchange it ran on
        block = value.split("```")[1].strip().splitlines()
        assert max(len(line) for line in block) <= 35   # one line per model on a phone
        assert ("Closest to passing: **Trend-only** (ahead on 2 of 2 exchanges, behind after fees). "
                "Needs 9 more days before it can be trusted.") in value
        assert "All markets: 5 on trial · 0 passed · 2 cut early for losing" in value
    finally:
        await _close(bot, home)


def _real_trial_board_record(market, asset, horizon):
    from parlay_bot.discord_app import trial_board

    return trial_board.read(market.store, asset=asset, horizon=horizon)


@pytest.mark.asyncio
async def test_polymarket_contest_table_shows_only_when_scores_exist(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    try:
        home.contest_models = lambda *_: None        # league not computed yet
        assert "🎲 Polymarket contest" not in _fields(await home.render())
        home.contest_models = lambda *_: [
            {"model": "digital-gbm-ewma-v1", "label": "GBM + EWMA", "contracts": 412,
             "trades": 160, "improvement": -.0012, "net_per_contract": -.004,
             "significant": False},
            {"model": "digital-odds-lag-logistic-v1", "label": "Odds-lag learner",
             "contracts": 12, "trades": 0, "improvement": 0., "net_per_contract": 0.,
             "significant": False},
        ]
        contest = _fields(await home.render())["🎲 Polymarket contest"]
        from tests.test_trial_board import _grouped

        assert _grouped(contest) == [("early", ["Odds-lag", "12", "+0.0", "—"]),
                                     ("behind", ["Formula", "412", "-1.2", "-0.4¢"])]
        assert "300 contracts and a statistical pass" in contest
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_persisted_learning_heartbeat_is_read_without_model_registry(tmp_path):
    bot = _client(tmp_path)
    market = PerpetualMarketStore(bot.store)
    autonomous = AutonomousResearch(market, ai=None, venues=())
    try:
        assert await autonomous.cycle() == (0, 0, 0)
        heartbeat = home_module._latest_learning_cycle(market)
        assert heartbeat is not None
        assert heartbeat["status"] == "recorded"
        assert heartbeat["math_ok"]
        assert heartbeat["model_review_ok"]
        assert heartbeat["issued"] == 0
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()


class _MappingRowConnection:
    """Fake the actual psycopg dict_row contract, not sqlite.Row[0]."""

    def __init__(self, table, result):
        self.table, self.result = table, result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, *_args):
        assert self.table in sql
        return self

    def fetchone(self):
        return self.result


class _MappingRowStore:
    def __init__(self, learning, forecast):
        self.rows = {
            "perp_learning_cycle_health": learning,
            "perp_forward_cycle_diagnostics": forecast,
        }

    def connect(self):
        # The production readers open separate connections per query. Return
        # psycopg-style dictionaries with no integer keys.
        store = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql):
                for table, row in store.rows.items():
                    if table in sql:
                        if isinstance(row, Exception):
                            raise row
                        return _MappingRowConnection(table, row)
                raise AssertionError("unexpected table")
        return Connection()


def test_postgres_dictionary_rows_make_both_worker_receipts_visible():
    now = datetime.now(UTC)
    market = SimpleNamespace(store=_MappingRowStore(
        learning={
            "completed_at": now.isoformat(), "math_ok": 1,
            "model_review_ok": 1, "math_issued": 12, "math_settled": 9,
        },
        forecast={
            "checked_at": now.isoformat(), "issued_forecasts": 12,
            "target_expired": 2,
        },
    ))
    result = home_module._performance_cycles(market)
    assert result["learning"] == {
        "status": "recorded", "checked_at": now, "math_ok": True,
        "model_review_ok": True, "issued": 12, "settled": 9,
    }
    assert result["forecast"] == {
        "status": "recorded", "checked_at": now, "issued": 12, "expired": 2,
    }


def test_missing_forecast_table_does_not_hide_learning_receipt():
    now = datetime.now(UTC)
    market = SimpleNamespace(store=_MappingRowStore(
        learning={
            "completed_at": now.isoformat(), "math_ok": 0,
            "model_review_ok": 1, "math_issued": 0, "math_settled": 4,
        },
        forecast=RuntimeError("missing forecast diagnostics table"),
    ))
    result = home_module._performance_cycles(market)
    assert result["forecast"]["status"] == "unavailable"
    assert result["learning"]["status"] == "recorded"
    assert result["learning"]["math_ok"] is False


def test_empty_cycle_tables_are_absent_not_misreported_as_healthy():
    market = SimpleNamespace(store=_MappingRowStore(learning=None, forecast=None))
    result = home_module._performance_cycles(market)
    assert result == {"forecast": None, "learning": None}


def _live_record(*, issued, decided, hits, windows, brier=.249, incumbent=.251,
                 baseline=.250, gain_v1=.002, updates=47, drift=.012):
    """Full-form regime live-learning trial record, as trial_report returns."""
    return {
        "issued": issued, "graded": decided, "decided": decided, "hits": hits,
        "windows": windows, "model_brier": brier, "incumbent_brier": incumbent,
        "baseline_brier": baseline, "paired_gain_vs_regime_v1": gain_v1,
        "selection_effect": .001, "coverage": .06, "availability_ratio": .83,
        "updates": updates, "weight_drift_mean": drift,
        "paired_gain_last_7d": .003, "paired_gain_days_8_30": None,
    }


def test_trial_detail_line_carries_every_cut_metric_and_only_those():
    """Headline-only change: the metrics cut from the Performance page
    live in _trial_detail_line (rendered by Detailed Research's Trial
    detail field). A metric the record does not carry is absent; a
    family with no decided results has no detail line at all."""
    record = _live_record(
        issued=434, decided=434, hits=214, windows=56,
        brier=.256, incumbent=.252, baseline=.250, gain_v1=-.004,
        updates=2292, drift=.325,
    )
    record["selection_effect"] = -.001
    record["coverage"] = .02
    record["availability_ratio"] = .45
    record["paired_gain_last_7d"] = -.008
    record["paired_gain_days_8_30"] = None
    line = home_module._trial_detail_line("Live-learning fast", record)
    assert line == (
        "**Live-learning fast:** vs guess 0.250 · vs regime_v1 -0.004 · "
        "selection -0.001 (easier subset) · coverage 2% · availability 45% · "
        "2,292 trending labels learned · weight drift 0.325 · gain last 7d -0.008")
    assert "too early to judge" not in line
    # A metric the record does not carry is simply absent.
    del record["paired_gain_last_7d"]
    assert "gain last 7d" not in home_module._trial_detail_line(
        "Live-learning fast", record)
    # No decided results: no detail line.
    assert home_module._trial_detail_line(
        "Live-learning fast", {"issued": 3, "decided": 0}) == ""


