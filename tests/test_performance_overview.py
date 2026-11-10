"""Performance scorecard: honest forward evidence and failure-isolated health."""
from __future__ import annotations

import re
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


@pytest.mark.asyncio
async def test_scorecard_is_short_source_specific_and_skips_heavy_training(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)

    def heavy_training(*_):
        raise AssertionError("full Drive/model summary must not run on Performance")

    home.training_status = heavy_training
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_: {
        "status": "insufficient", "targets": 198, "days": 2,
    })
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_: {
        "lineage_proven": 10, "historically_seeded": 0,
    })
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {
            "status": "recorded", "checked_at": now,
            "issued": 20, "expired": 0,
        },
        "learning": {
            "status": "recorded", "checked_at": now,
            "math_ok": True, "model_review_ok": True,
            "issued": 5, "settled": 4,
        },
    })
    try:
        panel = await home.render()
        fields = _fields(panel)
        assert list(fields) == [
            "🎯 Polymarket / Kalshi predictions",
            "📈 BTC exchange predictions (up or down?)",
            "🧠 Is it learning?",
            "📡 Is everything running?",
        ]
        assert "Does it make money?** Not proven yet" in panel.description
        assert "Better than a coin flip?** No, not yet" in fields["📈 BTC exchange predictions (up or down?)"]
        assert "(Error score: RivaL 0.310, coin flip 0.240; lower is better." in (
            fields["📈 BTC exchange predictions (up or down?)"]
        )
        assert "current model only" in fields["📈 BTC exchange predictions (up or down?)"]
        # The preamble is one short coverage line now: the timing
        # paragraph, the unscored breakdown, and the waiting line are
        # cut, so the 1024-char budget goes to the trial rows.
        assert "**Checked:** 7 of 8 predictions · every exchange it collects, the last 7 days, current model only" in (
            fields["📈 BTC exchange predictions (up or down?)"]
        )
        assert "couldn't be scored" not in fields["📈 BTC exchange predictions (up or down?)"]
        assert "minute or two" not in fields["📈 BTC exchange predictions (up or down?)"]
        assert "Covers:" not in fields["📈 BTC exchange predictions (up or down?)"]
        assert "No results running late" not in fields["📈 BTC exchange predictions (up or down?)"]
        assert "Better than Polymarket's own odds?** No, not yet" in fields["🎯 Polymarket / Kalshi predictions"]
        assert "(Error score: RivaL" in fields["🎯 Polymarket / Kalshi predictions"]
        assert "Past results studied:** 402 (history, not live predictions)" in fields["🎯 Polymarket / Kalshi predictions"]
        assert "Getting more accurate?** Too early to tell" in fields["🧠 Is it learning?"]
        assert "Built from the 6-year history?** 0 of the last 10 predictions" in fields["🧠 Is it learning?"]
        assert "Learning loop:** ✅ running" in fields["🧠 Is it learning?"]
        assert "Live market feeds:** ✅ 1 of 1 exchanges up to date" in fields["📡 Is everything running?"]
        assert "Prediction cycle:" in fields["📡 Is everything running?"]
        assert max(map(len, fields.values())) < 1024
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_slow_or_broken_learning_cannot_hide_exchange_or_cycle(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("learning read failure")))
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("lineage read failure")))
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {"status": "recorded", "checked_at": now, "issued": 7, "expired": 0},
        "learning": {"status": "unavailable"},
    })

    def bad_feeds():
        raise RuntimeError("feed read failure")

    home.market.feed_health = bad_feeds
    try:
        fields = _fields(await home.render())
        assert "Better than a coin flip?** No, not yet" in fields["📈 BTC exchange predictions (up or down?)"]
        assert "Getting more accurate?** Couldn't check right now" in fields["🧠 Is it learning?"]
        assert "Learning loop:** couldn't check right now" in fields["🧠 Is it learning?"]
        assert "Live market feeds:** couldn't check right now" in fields["📡 Is everything running?"]
        assert "made 7 predictions" in fields["📡 Is everything running?"]
        assert "Better than Polymarket's own odds?** No, not yet" in fields["🎯 Polymarket / Kalshi predictions"]
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


@pytest.mark.asyncio
async def test_timed_out_improvement_still_shows_training_lineage(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_:
                        (_ for _ in ()).throw(TimeoutError("independent score unavailable")))
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_: {
        "lineage_proven": 100, "historically_seeded": 0,
    })
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "learning": {"status": "recorded", "checked_at": now,
                     "math_ok": True, "model_review_ok": True, "issued": 108, "settled": 85},
        "forecast": {"status": "recorded", "checked_at": now,
                     "issued": 108, "expired": 0},
    })
    try:
        field = _fields(await home.render())["🧠 Is it learning?"]
        assert "Couldn't check right now" in field or "Still calculating" in field
        assert "0 of the last 100 predictions" in field
        assert "all coins and timeframes" in field
    finally:
        await _close(bot, home)


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


def _patch_live_reads(monkeypatch, *, pooled=None, regime=None, live=None,
                      live_fast=None):
    """Performance readers shaped like Felix's 2026-11-09 screenshot: the
    exchange field is already full (pooled + regime_v1 rows present) before
    the live rows are appended."""
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_: {
        "status": "insufficient", "targets": 198, "days": 2,
    })
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_: {
        "lineage_proven": 10, "historically_seeded": 0,
    })
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {"status": "recorded", "checked_at": now, "issued": 20, "expired": 0},
        "learning": {"status": "recorded", "checked_at": now, "math_ok": True,
                     "model_review_ok": True, "issued": 5, "settled": 4},
    })
    if pooled is not None:
        monkeypatch.setattr(home_module, "_pooled_record", lambda *_: pooled)
    if regime is not None:
        monkeypatch.setattr(home_module, "_regime_record", lambda *_: regime)
    if live is not None:
        monkeypatch.setattr(home_module, "_regime_live_record", lambda *_: live)
    if live_fast is not None:
        monkeypatch.setattr(home_module, "_regime_live_fast_record", lambda *_: live_fast)


@pytest.mark.asyncio
async def test_live_rows_get_own_fields_and_trial_rows_fit_in_exchange_field(
        tmp_path, monkeypatch):
    """2026-11-09 regression: the live rows were appended last to the
    1024-char exchange field, so the base row was cut mid-header and the
    fast row was cut entirely. Both rows must now appear in full, in their
    own fields, and nowhere inside the exchange field. The pooled and
    regime_v1 rows render in the same one-line format inside the exchange
    field, which is no longer full now that the preamble is one short line."""
    bot, home = _make_home(tmp_path)
    live = _live_record(issued=12, decided=9, hits=6, windows=11)
    fast = _live_record(issued=12, decided=9, hits=5, windows=11, brier=.248,
                        gain_v1=.003, updates=93, drift=.021)
    _patch_live_reads(
        monkeypatch,
        pooled={"issued": 12761, "graded": 12761, "decided": 12761, "hits": 6698,
                "windows": 1527, "model_brier": .249, "incumbent_brier": .251,
                "baseline_brier": .250},
        regime={"issued": 935, "graded": 935, "decided": 935, "hits": 511,
                "windows": 133, "model_brier": .253, "incumbent_brier": .253,
                "baseline_brier": .250, "selection_effect": .001, "coverage": .06},
        live=live, live_fast=fast,
    )
    try:
        fields = _fields(await home.render())
        assert list(fields) == [
            "🎯 Polymarket / Kalshi predictions",
            "📈 BTC exchange predictions (up or down?)",
            "🔬 Trend learner",
            "🔬 Fast trend learner",
            "🧠 Is it learning?",
            "📡 Is everything running?",
        ]
        exchange = fields["📈 BTC exchange predictions (up or down?)"]
        assert "live-learning" not in exchange
        assert "trend learner" not in exchange
        assert "**6-year history model:**" in exchange
        assert "**trend-only test:**" in exchange
        assert len(exchange) < 1024  # preamble cut freed the budget
        from parlay_bot.discord_app.answer_cards import rename_text
        base_text = rename_text(
            home_module._regime_live_text(live, "trend learner").strip())
        fast_text = rename_text(home_module._regime_live_text(
            fast, "fast trend learner").strip())
        assert fields["🔬 Trend learner"] == base_text
        assert fields["🔬 Fast trend learner"] == fast_text
        assert "gain +0.002" in base_text
        assert "\n" not in base_text  # headline only, no detail line
        assert "\n" not in fast_text
        # The cut metrics live in Detailed Research's Trial detail
        # field now, not on this page.
        assert "trending labels learned" not in base_text
        assert "weight drift" not in base_text
        assert "vs guess" not in base_text
        assert max(map(len, fields.values())) <= 1024
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_no_live_field_before_first_issuance(tmp_path, monkeypatch):
    """_regime_live_text returns "" before first issuance; no empty embed
    field may be added (Discord rejects empty field values)."""
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch, live={"issued": 0}, live_fast={"issued": 0})
    try:
        fields = _fields(await home.render())
        assert list(fields) == [
            "🎯 Polymarket / Kalshi predictions",
            "📈 BTC exchange predictions (up or down?)",
            "🧠 Is it learning?",
            "📡 Is everything running?",
        ]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_only_issued_family_gets_a_field(tmp_path, monkeypatch):
    """One family issued (short form: nothing decided yet), the other has
    not: only the issued family's field appears, with the short-form text."""
    bot, home = _make_home(tmp_path)
    live = {"issued": 3, "graded": 0, "decided": 0, "hits": 0, "windows": 0,
            "model_brier": None, "incumbent_brier": None, "baseline_brier": None}
    _patch_live_reads(monkeypatch, live=live, live_fast={"issued": 0})
    try:
        fields = _fields(await home.render())
        assert list(fields) == [
            "🎯 Polymarket / Kalshi predictions",
            "📈 BTC exchange predictions (up or down?)",
            "🔬 Trend learner",
            "🧠 Is it learning?",
            "📡 Is everything running?",
        ]
        assert fields["🔬 Trend learner"] == (
            "**trend learner:** "
            "3 predictions made, 0 graded so far · 0/150 targets")
    finally:
        await _close(bot, home)


def test_worst_case_headline_stays_under_140_chars():
    """Headline-only pin (2026-11-09): the longest label with the
    largest plausible numbers must stay under 140 chars, so a real
    row can never grow back into a paragraph. Bounds: decided ≤
    77,760 rows (30 days × 288 windows × 9 venues), windows ≤ 8,640,
    day ≤ 30. The old full form with every D7 extra measured 356."""
    extreme = _live_record(
        issued=77760, decided=77760, hits=77760, windows=8640,
        brier=.001, incumbent=.999, baseline=.999, gain_v1=1.0,
        updates=999999, drift=999.999,
    )
    extreme["selection_effect"] = 1.0
    extreme["paired_gain_last_7d"] = 1.0
    extreme["paired_gain_days_8_30"] = 1.0
    extreme["first_issued"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    for label in ("6-year history model", "trend-only test",
                  "trend learner", "fast trend learner"):
        text = home_module._trial_one_liner(label, extreme, datetime.now(UTC))
        assert "\n" not in text, label
        assert len(text) < 140, (label, len(text), text)
    longest = home_module._trial_one_liner(
        "fast trend learner", extreme, datetime.now(UTC))
    assert len(longest) < 140


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


_ONE_LINE_HEADLINE = re.compile(
    r"\*\*(?P<label>[^*]+):\*\* right (?P<hits>[\d,]+) of (?P<decided>[\d,]+) "
    r"\((?P<pct>\d+\.\d%|too few to judge — [\d,]+ graded)\) · Error score (?P<brier>\d\.\d{3}) vs main model "
    r"(?P<incumbent>\d\.\d{3}) · gain (?P<gain>[+-]\d\.\d{3}) · "
    r"day (?P<day>\d+)/21 · (?P<targets>[\d,]+)/150 targets"
)


@pytest.mark.asyncio
async def test_one_line_per_live_family_and_no_dead_or_zombie_families(
        tmp_path, monkeypatch):
    """Headline-only change: every live family renders as exactly one
    short headline line in the same format across the page; dead
    families (flow_v1/v2, regime_v2 — never issued, zero rows) never
    render; zombie families (archive_v1, gbm_v1 — no artifact
    imported) have no Performance-page row at all. The detail metrics
    the old detail line showed are NOT on this page anymore — they
    live in Detailed Research's Trial detail field."""
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    first_6d = (now - timedelta(days=6)).isoformat()
    first_2d = (now - timedelta(days=2)).isoformat()
    live = _live_record(issued=12, decided=9, hits=6, windows=11)
    live["first_issued"] = first_2d
    fast = _live_record(issued=12, decided=9, hits=5, windows=11, brier=.248,
                        gain_v1=.003, updates=93, drift=.021)
    fast["first_issued"] = first_2d
    pooled = {"issued": 12761, "graded": 12761, "decided": 12761, "hits": 6698,
              "windows": 1527, "model_brier": .249, "incumbent_brier": .251,
              "baseline_brier": .250, "first_issued": first_6d}
    regime = {"issued": 935, "graded": 935, "decided": 935, "hits": 511,
              "windows": 133, "model_brier": .253, "incumbent_brier": .253,
              "baseline_brier": .250, "selection_effect": .001, "coverage": .06,
              "first_issued": first_6d}
    _patch_live_reads(monkeypatch, pooled=pooled, regime=regime,
                      live=live, live_fast=fast)
    try:
        panel = await home.render()
        fields = _fields(panel)
        page = "\n".join([panel.description or "", *fields.values()])
        # One line per live family, same format, exactly once each.
        for label in ("6-year history model", "trend-only test",
                      "trend learner", "fast trend learner"):
            assert page.count(f"**{label}:**") == 1, label
            match = _ONE_LINE_HEADLINE.search(page)
            assert match is not None
        headlines = _ONE_LINE_HEADLINE.findall(page)
        assert len(headlines) == 4
        assert {h[0] for h in headlines} == {
            "6-year history model", "trend-only test",
            "trend learner", "fast trend learner",
        }
        # Audit Step 1: the live rows have 9 decided (< 20), so their
        # rate reads "too few to judge" instead of a % — same one-line
        # format, no rate on a tiny sample. The pooled/regime rows
        # have thousands decided, so theirs still show a %.
        live_rows = [h for h in headlines
                   if h[0] in ("trend learner", "fast trend learner")]
        assert all("too few to judge — 9 graded" in h[3] for h in live_rows)
        assert all("%" in h[3] for h in headlines if h not in live_rows)
        # Dead families never render; zombies have no row on this page.
        for dead in ("flow_v1", "flow_v2", "regime_v2", "regime-v2",
                     "Regime-v2"):
            assert dead not in page, dead
        for zombie in ("archive_v1", "gbm_v1"):
            assert zombie not in page, zombie
        # The cut metrics are gone from this page (they live in
        # Detailed Research's Trial detail field now).
        assert "vs guess" not in page
        assert "selection +" not in page
        assert "coverage" not in page
        assert "too early to judge" not in page
        assert "trending labels learned" not in page
        assert "weight drift" not in page
        assert "vs regime_v1" not in page
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_exchange_preamble_is_one_short_line_and_trial_rows_fit(
        tmp_path, monkeypatch):
    """Consolidation change 3: with Felix's screenshot shape (pooled +
    regime_v1 + live rows), the exchange field's preamble is one short
    coverage line — the timing paragraph, the unscored breakdown, and
    the waiting line are gone — and both trial rows fit without
    truncation. The honesty disclosures stay in the embed description."""
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    first_6d = (now - timedelta(days=6)).isoformat()
    live = _live_record(issued=12, decided=9, hits=6, windows=11)
    fast = _live_record(issued=12, decided=9, hits=5, windows=11, brier=.248,
                        gain_v1=.003, updates=93, drift=.021)
    _patch_live_reads(
        monkeypatch,
        pooled={"issued": 12761, "graded": 12761, "decided": 12761, "hits": 6698,
                "windows": 1527, "model_brier": .249, "incumbent_brier": .251,
                "baseline_brier": .250, "first_issued": first_6d},
        regime={"issued": 935, "graded": 935, "decided": 935, "hits": 511,
                "windows": 133, "model_brier": .253, "incumbent_brier": .253,
                "baseline_brier": .250, "selection_effect": .001, "coverage": .06,
                "first_issued": first_6d},
        live=live, live_fast=fast,
    )
    try:
        panel = await home.render()
        fields = _fields(panel)
        exchange = fields["📈 BTC exchange predictions (up or down?)"]
        lines = exchange.split("\n")
        assert lines[0].startswith("**Called the direction right:**")
        assert lines[1].startswith("**Better than a coin flip?**")
        assert lines[2] == (
            "**Checked:** 7 of 8 predictions · every exchange it collects, "
            "the last 7 days, current model only")
        assert "minute or two" not in exchange
        assert "couldn't be scored" not in exchange
        assert "Covers:" not in exchange
        assert "waiting for their candle" not in exchange
        assert "No results running late" not in exchange
        assert "**6-year history model:** right 6,698 of 12,761 (52.5%) · Error score 0.249 vs main model 0.251 · gain +0.002 · day 6/21 · 1,527/150 targets" in exchange
        assert "**trend-only test:** right 511 of 935 (54.7%) · Error score 0.253 vs main model 0.253 · gain +0.000 · day 6/21 · 133/150 targets" in exchange
        assert "vs guess" not in exchange
        assert "too early to judge" not in exchange
        assert len(exchange) < 1024
        assert "Does it make money?** Not proven yet" in panel.description
        assert "predictions saved *before* the result was known" in panel.description
    finally:
        await _close(bot, home)
