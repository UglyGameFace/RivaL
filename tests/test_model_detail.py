"""Detailed Research: pick any model on trial and see its own page.

The page reads only records RivaL already keeps (the hourly promotion
check, early stop, the test-model ledger and market conditions), compares
each test call with the main model's call on the same candle, and never
judges from fewer than 20 calls.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from parlay_bot.discord_app import model_detail
from parlay_bot.discord_app.model_detail import model_embed, model_report, trial_models
from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.early_stop import EarlyStop
from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def store(tmp_path):
    hot = SQLiteHotStore(tmp_path / "detail.sqlite")
    market = PerpetualMarketStore(hot)
    market.initialize()
    ForwardAccuracyMonitor(market).initialize()
    ShadowModelCompetition(hot).initialize()
    EarlyStop(hot).initialize()
    with hot.connect() as conn:
        conn.executescript(FAMILY_PROMOTION_SCHEMA)
    yield hot
    hot.close()


def _promotion(store, venue, horizon, evidence, asset="BTC"):
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion (venue,contract,quote,asset,horizon_minutes,
                 evaluated_at,selected_family,status,reason,evidence_json)
               VALUES (?,?,?,?,?,?,NULL,'held','test',?)""",
            (venue, f"{asset}-{venue}", "USDT", asset, horizon, NOW.isoformat(),
             json.dumps(evidence)))


_SEQ = iter(range(10**6))


def _call(store, *, family, p, main, up, horizon=5, venue="okx", regime=None):
    i = next(_SEQ)
    issued = NOW - timedelta(hours=1)
    ref = (issued - timedelta(seconds=i)).isoformat()
    y = int(up)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                 horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                 probability_up,incumbent_up,baseline_up,training_count,training_sha256,
                 settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, f"BTC-{venue}", "USDT", "BTC", horizon, family, ref, issued.isoformat(),
             issued.isoformat(), p, main, .5, 10, "t" * 64, issued.isoformat(), y,
             (p - y) ** 2, (main - y) ** 2, .25))
        if regime:
            conn.execute(
                """INSERT INTO perp_forward_regimes (venue,contract,quote,horizon_minutes,
                     model_version,reference_opened_at,issued_at,method_version,regime,
                     five_min_volatility_pct,feature_sha256,weights_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, f"BTC-{venue}", "USDT", horizon, MODEL, ref, issued.isoformat(),
                 "v1", regime, .1, "f" * 64, "w" * 64))


def _info(days, targets, gain, **extra):
    return {"days": days, "targets": targets, "forward_gain": gain, **extra}


def _seed(store):
    _promotion(store, "okx", 5, {
        "regime_v1": _info(12, 300, .004, cost_proof={
            "severe": {"paired_gain_bps": -.5, "windows": 200},
            "conservative": {"paired_gain_bps": 1.2, "windows": 200}}),
        "pooled_5m_aaaaaaaaaaaa": _info(3, 40, .01),
        "pooled_5m_bbbbbbbbbbbb": _info(12, 649, -.002),
        "liq_v1": _info(11, 581, -.06),
    })
    _promotion(store, "bybit", 5, {"regime_v1": _info(12, 100, -.002)})
    _promotion(store, "okx", 15, {"regime_v1": _info(9, 120, .001),
                                  "pooled_15m_cccccccccccc": _info(9, 200, .0)})
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_early_stop (venue,contract,quote,horizon_minutes,family,
                 stopped_at,rule_version,targets,days,model_brier,incumbent_brier,loss,
                 loss_lower) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("okx", "BTC-okx", "USDT", 5, "liq_v1", NOW.isoformat(), "t", 581, 11, .34,
             .26, .06, .05))
    # 40 trend-only calls at 58%: 30 right; the main model was right on 20.
    for k in range(40):
        main = .58 if k < 20 else (.42 if k < 30 else .58)   # wrong on k = 20..39
        _call(store, family="regime_v1", p=.58, main=main, up=k < 30,
              regime="quiet" if k < 30 else "turbulent")


def test_the_picker_lists_every_model_on_trial_for_the_coin(store):
    _seed(store)
    _promotion(store, "okx", 5, {"cross_v1": _info(1, 5, .0)}, asset="ETH")
    # One entry per model (6-year versions fold), longest-running first.
    assert trial_models(store, "BTC") == ["6-year", "Trend-only", "Liquidity"]
    assert trial_models(store, "ETH") == ["Cross-exch"]


def test_the_report_compares_like_with_like(store):
    _seed(store)
    report = model_report(store, "BTC", "Trend-only")
    five = report["horizons"][5]
    assert five["family"] == "regime_v1"
    assert (five["days"], five["results"]) == (12, 300)
    assert five["gain"] == pytest.approx((300 * .004 - 100 * .002) / 400)
    assert (five["ahead"], len(five["exchanges"])) == (1, 2)
    assert five["fees"] == {"severe": -.5, "conservative": 1.2}
    led = five["ledger"]
    assert (led["n"], led["right"], led["main_right"]) == (40, 30, 20)
    assert five["bands"][1]["n"] == 40               # every call was 58%
    assert five["conditions"]["quiet"]["n"] == 30
    assert set(report["horizons"]) == {5, 15}         # no 60-min trial
    # The most advanced 6-year version per timeframe, not the newest one.
    assert model_report(store, "BTC", "6-year")["horizons"][5]["family"] == (
        "pooled_5m_bbbbbbbbbbbb")
    assert model_report(store, "BTC", "Liquidity")["horizons"][5]["cut"] == 1
    assert model_report(store, "BTC", "Nope") is None


def test_the_page_answers_how_it_works_how_accurate_and_whats_left(store):
    _seed(store)
    embed = model_embed(model_report(store, "BTC", "Trend-only"), asset="BTC",
                        name="Trend-only")
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🧪 Trend-only · BTC · model detail"
    assert "only speaks when the market is trending" in embed.description
    assert "🟢 **Ahead so far**, not proven." in embed.description
    assert "5-min: day 12/21 · 300/150 results · ahead of the main model on 1 of 2" \
        in embed.description
    timeframes = fields["📏 Accuracy by timeframe"].split("```")[1].strip().splitlines()
    assert timeframes[1].split() == ["5m", "75.0%", "50.0%", "+2.5", "300", "12/21"]
    assert timeframes[3].split()[:2] == ["60m", "—"]
    exchanges = fields["🏦 By exchange (5-min)"].split("```")[1].strip().splitlines()
    assert exchanges[1].split() == ["okx", "300", "+4.0", "ahead"]
    assert exchanges[2].split() == ["bybit", "100", "-2.0", "behind"]
    # 58% calls came true 75% of the time over 40 calls: beyond noise (±13.7%).
    assert fields["🎚️ Is its confidence honest?"].startswith(
        "**Underconfident:** says 58% on average, right 75%.")
    calm = fields["🌦️ Calm vs wild markets"].split("```")[1].strip().splitlines()
    assert calm[1].split()[0] == "Quiet" and calm[2].split()[:2] == ["Turbulent", "few"]
    checklist = fields["🏁 What it needs to replace the main model"]
    assert "⬜ 21 days of live results (has 12)" in checklist
    assert "✅ 150 independent results (has 300)" in checklist
    assert "⬜ ahead after heavy trading costs (-0.5 bps per trade over 200 windows)" \
        in checklist
    assert "✅ never cut for losing" in checklist
    assert all(len(v) < 1024 for v in fields.values()) and len(embed) < 6000
    assert "can only take over by passing every check" in embed.footer.text


def test_a_cut_model_says_so_and_an_unknown_one_explains(store):
    _seed(store)
    liq = model_embed(model_report(store, "BTC", "Liquidity"), asset="BTC", name="Liquidity")
    assert "🛑 **Cut for losing** on every exchange it ran on." in liq.description
    missing = model_embed(None, asset="BTC", name="Old model")
    assert "no recent trial results" in missing.description


def test_every_listed_model_has_a_plain_description():
    from parlay_bot.discord_app.trial_board import _NAMES

    for name in {*_NAMES.values(), "6-year", "Boosted", "History"}:
        assert name in model_detail.HOW_IT_WORKS, name


class _Edit:
    def __init__(self):
        self.response = SimpleNamespace(defer=self._noop)
        self.shown = None
        self.view = None

    async def _noop(self, *a, **k):
        return None

    async def edit_original_response(self, *, embed, view):
        self.shown, self.view = embed, view


@pytest.mark.asyncio
async def test_detailed_research_opens_a_models_page_and_back(store):
    _seed(store)
    view = ResearchDashboard(owner_id=1, market=SimpleNamespace(store=store), ai=None,
                             venues=("okx",))
    view.screen, view.section = "accuracy", "better"
    view._rebuild()
    view._render_full = lambda: _plain()  # the card itself is tested elsewhere
    await view.render()
    picker = next(i for i in view.children if getattr(i, "custom_id", "") ==
                  "rival:research:model")
    assert [o.label for o in picker.options] == ["6-year", "Trend-only", "Liquidity"]
    assert picker.row == 4 and len({i.row for i in view.children}) <= 5
    # The exchange picker gave its row to the model picker on this card.
    assert not any(getattr(i, "custom_id", "") == "rival:research:venue"
                   for i in view.children)

    interaction = _Edit()
    picker._values = ["Trend-only"]
    await picker.callback(interaction)
    assert interaction.shown.title == "🧪 Trend-only · BTC · model detail"
    ids = {getattr(i, "custom_id", "") for i in interaction.view.children}
    assert {"rival:research:model:back", "rival:research:model"} <= ids
    back = next(i for i in interaction.view.children
                if getattr(i, "custom_id", "") == "rival:research:model:back")
    await back.callback(interaction)
    assert view.model is None and interaction.shown.title == "Results"


async def _plain():
    return discord.Embed(title="Results")
