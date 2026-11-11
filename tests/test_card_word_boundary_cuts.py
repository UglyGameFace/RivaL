"""Card word-boundary cuts (2026-11-10): the remaining raw [:N] slices.

Audit Step 1 (PR #476) routed only home.py + research_dashboard.py through
``fit_embed_field_value``. The sites below still sliced raw and cut words
mid-word in live cards. Every site test feeds an over-budget value with a
word straddling the old cut point and asserts the fitted text never ends
mid-word. Each site test FAILS on the unmodified code (verified via
``git stash``): the unfitted text is captured by patching the module's
fitter to identity, and the test asserts the old ``[:budget]`` slice would
have cut mid-word, so the test cannot pass while the slice is still there.

The bot.py fields (rival_health command) and the signals.py title are not
reachable through a cheap public call, so they are covered by the fitter
contract tests at their budgets (1024, 170, 256) instead of a functional
test; the call sites are the identical one-line pattern tested
functionally at the other sites.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from parlay_bot.discord_app import embed_text
from parlay_bot.discord_app import opportunities as opp_module
from parlay_bot.discord_app import status_card as sc_module
from parlay_bot.discord_app.polymarket_watch import PolymarketReader
from parlay_bot.discord_app.status_card import StatusFacts, build_embed
from parlay_bot.perpetuals import polymarket_history as ph_module
from parlay_bot.perpetuals import polymarket_tracking as pt_module
from parlay_bot.perpetuals import proof_progress as pp_module
from parlay_bot.perpetuals.polymarket_history import fairness_text, results_text
from parlay_bot.perpetuals.polymarket_tracking import polymarket_tracking_text
from parlay_bot.perpetuals.proof_progress import proof_progress_text
from tests.test_performance_overview import (
    _close,
    _fields,
    _make_home,
    _patch_live_reads,
)


def _straddling_line(budget: int) -> str:
    """One long line with the word STRADDLING crossing the cut point.

    The head is ``budget - 5`` chars of complete ``"word "`` repeats (plus
    padding spaces), so STRADDLING spans [budget-5, budget+5): the old
    ``[:budget]`` slice ends inside it.
    """
    head_len = budget - 5
    repeats, rem = divmod(head_len, 5)
    head = "word " * repeats + " " * rem
    assert len(head) == head_len
    return head + "STRADDLING" + " tail" * 60


def _assert_no_mid_word_cut(original: str, result: str, budget: int) -> None:
    """The fitted text ends at a word or line boundary of the original."""
    assert len(result) <= budget
    core = result.removesuffix("…")
    assert original.startswith(core), "fitted text must be a prefix of the original"
    assert len(core) == len(original) or original[len(core)] in (" ", "\n"), (
        "fitted text ends mid-word"
    )


def _assert_discriminating(unfitted: str, budget: int) -> None:
    """The test input actually straddles the cut point.

    Guards against a test that would pass on the old code because the old
    slice happened to land on a space or a line boundary.
    """
    assert len(unfitted) > budget, "test input must exceed the budget"
    assert unfitted[budget] not in (" ", "\n"), "old slice must cut mid-word"
    assert unfitted[budget - 1] != "\n", "old slice must not end on a line boundary"


def _captured_unfitted(module, builder, *args, **kwargs):
    """Call ``builder`` with the module's fitter patched to identity.

    Returns the exact text the builder would have fitted, so the test can
    check the fitted result against it. On unmodified code the module has
    no fitter (``create=True``), the patch is a no-op, and the returned
    text is the already-sliced value, which fails ``_assert_discriminating``.
    """
    seen: list[str] = []

    def _identity(text: str, *, budget: int = 1024) -> str:
        seen.append(text)
        return text

    with patch.object(module, "fit_embed_field_value", _identity, create=True):
        builder(*args, **kwargs)
    assert seen, "builder did not call the fitter"
    return seen[0]


# --- The move itself -------------------------------------------------------


def test_fitter_lives_in_leaf_module_and_home_reexports_it():
    from parlay_bot.discord_app.embed_text import (
        DISCORD_FIELD_LIMIT,
        fit_embed_field_value,
    )
    from parlay_bot.discord_app.home import (
        DISCORD_FIELD_LIMIT as home_limit,
    )
    from parlay_bot.discord_app.home import (
        fit_embed_field_value as home_fitter,
    )

    assert DISCORD_FIELD_LIMIT == 1024
    assert home_limit is DISCORD_FIELD_LIMIT
    assert home_fitter is fit_embed_field_value
    assert embed_text.fit_embed_field_value is fit_embed_field_value


# --- Fitter contract at the budgets the unreachable sites use --------------


@pytest.mark.parametrize("budget", [256, 170, 1000, 1024, 2000])
def test_fit_word_boundary_at_every_site_budget(budget):
    text = _straddling_line(budget)
    result = embed_text.fit_embed_field_value(text, budget=budget)
    _assert_no_mid_word_cut(text, result, budget)
    assert result.endswith("…"), "a cut single line ends with an ellipsis"


# --- perpetuals text builders (sites 11-13) --------------------------------


def _history_with_horizons(count: int) -> dict:
    return {
        "horizons": {
            horizon: {
                "results": [True, False] * 50,
                "first": datetime(2026, 11, 1, tzinfo=UTC),
                "day_up": 3,
                "day_down": 2,
            }
            for horizon in (5, 15, 60, 120, 240, 360, 720, 1440)[:count]
        }
    }


def test_results_text_no_mid_word_cut():
    history = _history_with_horizons(8)
    unfitted = _captured_unfitted(ph_module, results_text, history)
    _assert_discriminating(unfitted, 1024)
    result = results_text(history)
    assert result != unfitted[:1024]
    _assert_no_mid_word_cut(unfitted, result, 1024)


def _fairness_history(count: int) -> dict:
    return {
        "buckets": [
            {
                "n": 25,
                "price_sum": 12.5,
                "up": 13,
                "label": f"{40 + i}-{50 + i}%",
            }
            for i in range(count)
        ]
    }


def test_fairness_text_no_mid_word_cut():
    history = _fairness_history(13)
    unfitted = _captured_unfitted(ph_module, fairness_text, history)
    _assert_discriminating(unfitted, 1024)
    result = fairness_text(history)
    assert result != unfitted[:1024]
    _assert_no_mid_word_cut(unfitted, result, 1024)


def _tracking_result(cohorts: int) -> dict:
    call = {
        "outcome": "UP",
        "side": "UP",
        "price": 0.55,
        "issued_at": datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        "probability_up": 0.6,
    }
    return {
        "cohorts": [
            {
                "horizon": horizon,
                "issued": 10,
                "target": 10,
                "settled": 10,
                "picked": 25,
                "wins": 13,
                "waits": 2,
                "return_per_dollar": 0.02,
                "priced": 25,
                "model_brier": 0.24,
                "market_brier": 0.23,
                "recent": [dict(call), dict(call), dict(call)],
            }
            for horizon in (5, 15, 60, 120)[:cohorts]
        ]
    }


def test_polymarket_tracking_text_no_mid_word_cut():
    result_in = _tracking_result(4)
    unfitted = _captured_unfitted(pt_module, polymarket_tracking_text, result_in)
    _assert_discriminating(unfitted, 1024)
    result = polymarket_tracking_text(result_in)
    assert result != unfitted[:1024]
    _assert_no_mid_word_cut(unfitted, result, 1024)


def _proof_with_evaluations(count: int) -> dict:
    checks = {
        "future_outcomes": True,
        "beats_baselines": True,
        "quote_coverage": True,
        "after_cost_paper": True,
        "valid_records": True,
    }
    return {
        "evaluations": [
            {
                "horizon": horizon,
                "venue": "hyperliquid-international-exchange",
                "contract": "BTC-USD",
                "candidate": {"valid": True, "name": "archive_v1", "checks": dict(checks)},
                "fresh": True,
            }
            for horizon in (5, 15, 60, 120, 240, 360, 720, 1440)[:count]
        ]
    }


def test_proof_progress_text_no_mid_word_cut():
    proof = _proof_with_evaluations(8)
    unfitted = _captured_unfitted(pp_module, proof_progress_text, proof)
    _assert_discriminating(unfitted, 1024)
    result = proof_progress_text(proof)
    assert result != unfitted[:1024]
    _assert_no_mid_word_cut(unfitted, result, 1024)


# --- status card heartbeat (site 4) -----------------------------------------


def test_status_card_heartbeat_no_mid_word_cut():
    now = datetime.now(UTC)
    facts = StatusFacts(
        now=now,
        boot_at=now - timedelta(hours=2),
        ready=True,
        heartbeat={
            "consecutive_problems": 1,
            "last_success_at": (now - timedelta(minutes=5)).isoformat(),
            "last_problem_at": (now - timedelta(minutes=3)).isoformat(),
            "last_problem": _straddling_line(1024),
        },
    )
    unfitted = _captured_unfitted(
        sc_module, lambda: build_embed(facts)[0].fields
    )
    _assert_discriminating(unfitted, 1024)
    embed, _, _ = build_embed(facts)
    fields = {field.name: field.value for field in embed.fields}
    assert "Outside uptime check" in fields
    assert fields["Outside uptime check"] != unfitted[:1024]
    _assert_no_mid_word_cut(unfitted, fields["Outside uptime check"], 1024)


# --- GuidedHome "What Happened" note (site 5) ------------------------------


@pytest.mark.asyncio
async def test_home_note_no_mid_word_cut(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    _patch_live_reads(monkeypatch, live={"issued": 0}, live_fast={"issued": 0})
    note = _straddling_line(1000)
    home.note = note
    try:
        embed = await home.render()
    finally:
        await _close(bot, home)
    fields = _fields(embed)
    assert "What Happened" in fields
    _assert_no_mid_word_cut(note, fields["What Happened"], 1000)
    assert fields["What Happened"] != note[:1000]


# --- Opportunities desk: Polymarket field + watch name (sites 6-7) ----------


def test_crypto_desk_embed_no_mid_word_cut(monkeypatch):
    now = datetime.now(UTC)
    over_budget_value = _straddling_line(1024)
    long_venue = " ".join(["venueword"] * 40)
    watch = {
        "direction": "DOWN",
        "horizon_minutes": 5,
        "venue": long_venue,
        "price": 50000.0,
        "price_observed_at": now,
        "direction_probability": 0.6,
        "issued": now - timedelta(minutes=5),
        "target": now,
        "model_version": None,
        "contract": "BTC-USD",
        "quote": "USD",
    }
    monkeypatch.setattr(
        opp_module, "polymarket_call_fields",
        lambda store, *, asset, now: [
            ("🎰 Polymarket • 5 min • call: UP", over_budget_value),
        ],
    )
    monkeypatch.setattr(opp_module, "recent_watches", lambda market, *, asset, now: [watch])
    monkeypatch.setattr(opp_module, "otg_snapshot", lambda watch, market: {})
    monkeypatch.setattr(opp_module, "live_lean", lambda *args, **kwargs: None)
    monkeypatch.setattr(opp_module, "otg_line", lambda otg: "OTG line")
    monkeypatch.setattr(opp_module, "lean_line", lambda lean: "lean line")
    monkeypatch.setattr(opp_module, "below_line_label", lambda otg: None)

    seen: list[tuple[str, int]] = []

    def _identity(text: str, *, budget: int = 1024) -> str:
        seen.append((text, budget))
        return text

    market = SimpleNamespace(store=object())
    with patch.object(opp_module, "fit_embed_field_value", _identity, create=True):
        opp_module.crypto_desk_embed(market, asset="BTC", now=now)
    by_budget = {budget: text for text, budget in seen}
    assert by_budget.get(1024) == over_budget_value
    assert 256 in by_budget, "watch field name must be fitted at budget 256"
    name_unfitted = by_budget[256]
    _assert_discriminating(name_unfitted, 256)
    _assert_discriminating(over_budget_value, 1024)

    embed = opp_module.crypto_desk_embed(market, asset="BTC", now=now)
    fields = {field.name: field.value for field in embed.fields}
    assert fields["🎰 Polymarket • 5 min • call: UP"] != over_budget_value[:1024]
    _assert_no_mid_word_cut(
        over_budget_value, fields["🎰 Polymarket • 5 min • call: UP"], 1024,
    )
    watch_name = next(name for name in fields if name.startswith("👀 Leans"))
    assert watch_name != name_unfitted[:256]
    _assert_no_mid_word_cut(name_unfitted, watch_name, 256)


# --- sports discovery (site 8) ----------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args):
        return _FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def initialize(self):
        return None

    def connect(self):
        return _FakeConn(self._rows)


def test_sports_discovery_embed_no_mid_word_cut():
    from parlay_bot.discord_app import sports_discovery as sd_module
    from parlay_bot.discord_app.sports_discovery import sports_discovery_embed

    now = datetime.now(UTC)
    rows = [
        {
            "sport_name": f"Sport {i} with a very long name",
            "tournament_name": f"Tournament {i} championship league division",
            "events": 123,
            "books": 5,
        }
        for i in range(12)
    ]
    store = _FakeStore(rows)
    unfitted = _captured_unfitted(
        sd_module,
        lambda: sports_discovery_embed(
            store, configured_leagues=(), collector_enabled=True, now=now,
        ).fields,
    )
    _assert_discriminating(unfitted, 1000)
    embed = sports_discovery_embed(
        store, configured_leagues=(), collector_enabled=True, now=now,
    )
    fields = {field.name: field.value for field in embed.fields}
    assert "Live markets found" in fields
    assert fields["Live markets found"] != unfitted[:1000]
    _assert_no_mid_word_cut(unfitted, fields["Live markets found"], 1000)


# --- data dashboard coverage (site 9) ---------------------------------------


def test_coverage_embed_no_mid_word_cut():
    from parlay_bot.discord_app import data_dashboard as dd_module
    from parlay_bot.discord_app.data_dashboard import coverage_embed

    now = datetime.now(UTC)
    assets = tuple(f"ASSET{i:02d}" for i in range(30))
    rows = [
        {"venue": "hyperliquid", "asset": asset, "status": f"custom_problem_{i:02d}"}
        for i, asset in enumerate(assets)
    ]
    kwargs = {
        "rows": rows,
        "venues": ("hyperliquid", "dydx"),
        "assets": assets,
        "selected_venue": "hyperliquid",
        "interval_seconds": 300,
        "now": now,
    }
    unfitted = _captured_unfitted(dd_module, lambda: coverage_embed(**kwargs).fields)
    _assert_discriminating(unfitted, 1000)
    embed = coverage_embed(**kwargs)
    fields = {field.name: field.value for field in embed.fields}
    assert "Why data is unavailable" in fields
    assert fields["Why data is unavailable"] != unfitted[:1000]
    _assert_no_mid_word_cut(unfitted, fields["Why data is unavailable"], 1000)


# --- Polymarket rules storage (site 14) --------------------------------------
#
# Trace: PolymarketNotice.rules has no embed-field render anywhere — the
# only display use of ``.rules`` is the Kalshi render in home.py:1543, and
# polybolt_twap / polymarket_oracle only lowercase-match it. So the
# word-boundary fit belongs at storage, budget 2000.


@pytest.mark.asyncio
async def test_polymarket_rules_storage_no_mid_word_cut(monkeypatch):
    reader = PolymarketReader()
    now = datetime.now(UTC)
    description = _straddling_line(2000)
    token_up = "1234567890123456789012345678901"
    token_down = "1234567890123456789012345678902"
    condition = "0x" + "ab" * 32
    event = {
        "markets": [
            {
                "clobTokenIds": json.dumps([token_up, token_down]),
                "conditionId": condition,
                "description": description,
                "resolutionSource": "Chainlink",
                "feesEnabled": False,
            }
        ]
    }
    window = SimpleNamespace(
        closes=now + timedelta(hours=1), accepting_orders=True, price_to_beat=0.5,
    )

    async def _fake_event(slug, *, now, refresh=False):
        return event

    class _FakeResponse:
        def __init__(self, token):
            self._token = token

        def raise_for_status(self):
            return None

        def json(self):
            ts = int(now.timestamp() * 1000)
            return {
                "timestamp": str(ts),
                "asset_id": self._token,
                "market": condition,
                "asks": [{"price": "0.55", "size": "100"}],
                "bids": [{"price": "0.45", "size": "100"}],
            }

    class _FakeClient:
        async def get(self, url, params=None):
            return _FakeResponse(params["token_id"])

    monkeypatch.setattr(reader, "_event", _fake_event)
    monkeypatch.setattr(
        PolymarketReader, "_window", staticmethod(lambda *args, **kwargs: window),
    )
    monkeypatch.setattr(reader, "_http", lambda: _FakeClient())

    notice = await reader._lookup(
        slug="btc-up-or-down", asset="BTC", horizon=5, now=now,
    )
    assert notice.rules is not None
    assert notice.rules != description[:2000]
    _assert_no_mid_word_cut(description, notice.rules, 2000)
