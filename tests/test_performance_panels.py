"""Default mobile overview, scoped tabs and a visible XMarket lifecycle."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app.performance_panels import SECTIONS, build
from tests.test_performance_overview import _close, _make_home, _patch_live_reads
from tests.test_performance_three_embeds import NOW, _reports


def _xmarket():
    return {"status": "recorded", "asset": "BTC", "checked_at": NOW.isoformat(), "scopes": [
        {"horizon_minutes":5,"status":"no_qualifying_fit","usable_imports":0,
         "issued_24h":0,"settled_24h":0},
        {"horizon_minutes":15,"status":"shadowing","usable_imports":1,
         "issued_24h":26,"settled_24h":12},
        {"horizon_minutes":60,"status":"shadowing","usable_imports":1,
         "issued_24h":10,"settled_24h":0},
    ]}


def _build(section="overview", horizon=5, **changes):
    reports = {**_reports(), "xmarket": _xmarket(), **changes}
    return build("BTC", horizon, reports, section=section, now=NOW,
                 venues=("hyperliquid",), details_renderer=home_module.performance_embeds)


def _text(embeds):
    return "\n".join(str(e.to_dict()) for e in embeds)


def test_mobile_overview_is_compact_and_shows_xmarket_outside_the_selected_scope():
    embeds = _build()
    assert len(embeds) == 1 and len(embeds[0]) < 2200
    text = _text(embeds)
    assert "Main exchange model: behind baseline" in text
    assert "Polymarket · last 7 days" in text
    assert "5m** — no qualifying fit" in text
    assert "15m** — shadowing · 26 issued / 12 settled" in text
    assert "60m** — shadowing · 10 issued / 0 settled" in text
    assert "Qualified imports are on **15m/60m**" in text
    assert "```" not in text and "Model Lab" not in text
    assert "Getting more accurate?" not in text


def test_market_tabs_keep_baselines_and_data_separate():
    exchanges = _text(_build("exchanges"))
    poly = _text(_build("polymarket"))
    assert "0.251" in exchanges and "0.250" in exchanges
    assert "Polymarket" not in exchanges
    assert "0.233" in poly and "0.231" in poly
    assert "Up/down calls" not in poly and "main exchange model" not in poly.lower()
    assert "Kalshi" not in _text(_build("polymarket", polymarket={
        **_reports()["polymarket"], "kalshi": {"contracts": 12}}))


def test_models_show_a_new_xmarket_before_hourly_comparison_catches_up():
    text = _text(_build("models", horizon=15))
    assert "XMarket comparison pending" in text
    assert "Forecasts are recorded" in text and "26 issued / 12 settled" in text


def test_full_evidence_remains_available_and_keeps_discord_limits():
    embeds = _build("details")
    assert len(embeds) == 3
    assert "Scoreboard" in _text(embeds) and "XMarket activity" in _text(embeds)
    assert sum(map(len, embeds)) <= 5900
    assert all(len(f.value) <= 1024 for e in embeds for f in e.fields)


def test_baseline_comparison_never_claims_a_temporal_improvement_test():
    text = _text(_build("health"))
    assert "Baseline skill:" in text
    assert "has not passed the base-rate and coin-flip checks" in text
    assert "Improvement over time:" in text
    assert "Getting more accurate?" not in text


def test_overview_withholds_accuracy_when_graded_counts_contradict_scored_counts():
    exchange = _reports()["exchange"]
    exchange["last_7d"][5]["scored"] = 1
    text = _text(_build(exchange=exchange))
    assert "Exchange counts need checking" in text
    assert "Right **" not in text


def test_failed_or_stale_xmarket_reads_cannot_turn_into_no_models():
    failed = _text(_build(xmarket=RuntimeError("private data must not leak")))
    assert "lifecycle unavailable" in failed and "private data must not leak" not in failed
    stale = _text(_build(_stale={"xmarket":timedelta(minutes=7)}))
    assert "OLD COPY" in stale and "not live" in stale


@pytest.mark.asyncio
async def test_default_home_is_one_overview_and_skips_unrelated_slow_readers(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_xmarket_record", lambda *_: _xmarket())

    def unrelated(*_):
        raise AssertionError("overview must not query this detail")

    monkeypatch.setattr(home_module, "_performance_benefit", unrelated)
    monkeypatch.setattr(home_module, "_calibration_record", unrelated)
    monkeypatch.setattr(home_module, "_best_record", unrelated)
    try:
        assert home.performance_section == "overview"
        reports = await home._performance_reports()
        assert set(reports) == {"exchange", "polymarket", "trials", "xmarket", "cycles"}
        embeds = await home.render_embeds()
        assert len(embeds) == 1 and "Overview" in embeds[0].title
        home.research_factory = lambda _owner: object()
        home._buttons()
        rows = [c.row for c in home.children if isinstance(c, home_module.discord.ui.Button)]
        assert all(rows.count(row) <= 3 for row in set(rows))
        select = next(c for c in home.children if c.custom_id.endswith("performance-section"))
        assert {o.value for o in select.options} == set(SECTIONS)
        home.performance_section = "models"
        home._performance_refresh = True
        fresh = home._display_generation()
        assert fresh.performance_section == "models" and fresh._performance_refresh
        fresh.stop()
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_refresh_reads_new_lifecycle_and_keeps_the_selected_view(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    calls = []
    monkeypatch.setattr(home_module, "_xmarket_record", lambda *_: calls.append(1) or _xmarket())
    try:
        home.performance_section = "models"
        await home._performance_reports()
        await home._performance_reports()
        assert len(calls) == 1
        home.redraw = AsyncMock()
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        await home.act(interaction, "performance")
        assert home._performance_refresh and home.performance_section == "models"
        await home._performance_reports()
        assert len(calls) == 2
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_xmarket_is_rechecked_after_a_minute_and_missing_reads_are_not_cached(
        tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch)
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(home_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def read(*_):
        calls.append(1)
        return {"status": "unavailable", "partial": True} if len(calls) == 1 else _xmarket()

    monkeypatch.setattr(home_module, "_xmarket_record", read)
    home.performance_section = "models"
    try:
        await home._performance_reports()
        await home._performance_reports()
        assert len(calls) == 2
        await home._performance_reports()
        assert len(calls) == 2
        clock[0] += 61
        await home._performance_reports()
        assert len(calls) == 3
    finally:
        await _close(bot, home)
