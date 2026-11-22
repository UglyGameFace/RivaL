"""Discord 2025-26 pilots (2026-11-22): Components V2 price card + state
modal normalisation.

Pilot 1: price_card_view renders the price-target card as a Components V2
LayoutView built FROM price_embed, gated by RIVAL_COMPONENTS_V2_CARDS
(default off). These tests assert both renderings carry the same core data
and that the flag defaults off.

Pilot 2: StateModal stays a free-text modal (a single select cannot hold
51 jurisdictions — Discord caps select options at 25), but the
normalisation it routes through (normalize_state via choose_state) now
accepts punctuation variants and unambiguous misspellings, while every
previously accepted input still resolves identically.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
import pytest

from parlay_bot.discord_app.price_targets import (
    PriceResult,
    components_v2_cards_enabled,
    price_card_view,
    price_embed,
    reset_components_v2_cards_cache,
)
from parlay_bot.jurisdiction.states import normalize_state


def _result(**overrides) -> PriceResult:
    now = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)
    base = {
        "asset": "BTC",
        "horizon_minutes": 15,
        "target_price": "105",
        "reference_price": 100.0,
        "required_direction": "UP",
        "status": "WATCH",
        "explanation": "Monitoring completed candles. "
                       "Target-hit probability is not validated.",
        "venue": "gate",
        "last_price": 101.5,
        "observed_at": now - timedelta(minutes=1),
        "started_at": now - timedelta(minutes=5),
        "deadline": now + timedelta(minutes=10),
        "hit_at": None,
    }
    base.update(overrides)
    return PriceResult(**base)


def _component_texts(payload, out: list[str] | None = None) -> list[str]:
    out = [] if out is None else out
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, str):
            out.append(content)
        for value in payload.values():
            _component_texts(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _component_texts(item, out)
    return out


@pytest.fixture(autouse=True)
def _reset_flag_cache():
    reset_components_v2_cards_cache()
    yield
    reset_components_v2_cards_cache()


def test_components_v2_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("RIVAL_COMPONENTS_V2_CARDS", raising=False)
    reset_components_v2_cards_cache()
    assert components_v2_cards_enabled() is False


def test_components_v2_flag_on_when_env_set(monkeypatch):
    monkeypatch.setenv("RIVAL_COMPONENTS_V2_CARDS", "true")
    reset_components_v2_cards_cache()
    assert components_v2_cards_enabled() is True


@pytest.mark.parametrize("status", ["WATCH", "WAIT", "EXPIRED"])
def test_v2_card_carries_same_core_fields_as_embed(status):
    result = _result(status=status)
    embed = price_embed(result)
    view = price_card_view(result)
    assert isinstance(view, discord.ui.LayoutView)

    payload = view.to_components()
    text = "\n".join(_component_texts(payload))
    assert embed.title in text
    assert embed.description in text
    for field in embed.fields:
        assert field.name in text
        assert field.value in text
    assert embed.footer.text in text
    # Accent colour matches the embed colour for the status.
    assert payload[0]["accent_color"] == embed.color.value


def test_v2_card_hit_includes_crossing_candle():
    now = datetime(2026, 11, 22, 17, 0, tzinfo=UTC)
    result = _result(status="HIT", hit_at=now - timedelta(minutes=2))
    embed = price_embed(result)
    text = "\n".join(_component_texts(price_card_view(result).to_components()))
    hit_field = [f for f in embed.fields if f.name == "Verified crossing candle"]
    assert hit_field and hit_field[0].value in text


def test_v2_card_without_last_price_matches_embed_fields():
    result = _result(last_price=None, observed_at=None)
    embed = price_embed(result)
    text = "\n".join(_component_texts(price_card_view(result).to_components()))
    assert "Last verified close" not in text
    for field in embed.fields:
        assert field.value in text


def test_embed_path_unchanged():
    embed = price_embed(_result())
    assert isinstance(embed, discord.Embed)
    assert embed.title == "BTC • 15m • 👀 WATCH"
    assert embed.fields[0].name == "Your target"


# --- Pilot 2: state normalisation ------------------------------------------


@pytest.mark.parametrize("raw,code", [
    # Previously accepted — must never regress.
    ("ct", "CT"), ("CT", "CT"), ("Connecticut", "CT"),
    ("CONNECTICUT", "CT"), ("  New   York ", "NY"),
    ("Washington D.C.", "DC"), ("DC", "DC"), ("d.c.", "DC"),
    ("washington dc", "DC"), ("District of Columbia", "DC"),
    # Punctuation variants (new).
    ("C.T.", "CT"), ("N.Y.", "NY"), ("Washington, DC", "DC"),
    # Lowercase / mixed-case names (already worked, pinned here).
    ("texas", "TX"), ("nEw hAmPsHiRe", "NH"),
    # Unambiguous misspellings (new typo tolerance).
    ("Conneticut", "CT"), ("conneticut", "CT"), ("New Yrok", "NY"),
    ("Pensylvania", "PA"), ("Californa", "CA"), ("Virgina", "VA"),
    ("Massachusets", "MA"),
])
def test_normalize_state_accepts(raw, code):
    assert normalize_state(raw) == code


@pytest.mark.parametrize("raw", [
    "Dakota",      # ambiguous: North vs South — must not guess
    "Carolina",    # ambiguous: North vs South — must not guess
    "York",        # fragment, not a state name
    "Ontario",     # not a U.S. jurisdiction
    "Atlantis",
    "XX",          # codes are exact-only, never fuzzy-matched
    "",
])
def test_normalize_state_still_rejects(raw):
    with pytest.raises(ValueError):
        normalize_state(raw)


def test_state_modal_still_free_text():
    # The modal keeps its TextInput (a select cannot list 51 jurisdictions);
    # the improvement lives in the normaliser it submits through.
    from parlay_bot.discord_app.views import StateModal

    assert isinstance(StateModal.state, discord.ui.TextInput)
