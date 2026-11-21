"""Round-4 audit quick fixes: each one pinned by a behavioural test."""
from __future__ import annotations

from parlay_bot.discord_app import auto_hunt
from parlay_bot.discord_app.status_card import STARTUP_GRACE, STREAM_STARTUP_GRACE, plain


def test_auto_hunt_alerts_are_off_unless_explicitly_enabled():
    service = auto_hunt.AutoHuntService(bot=None, market=None, quotes=None, store=None)
    assert service.alerts_enabled is False


def test_stream_grace_and_startup_noise_window_are_separate_settings():
    assert STREAM_STARTUP_GRACE.total_seconds() == 300
    assert STARTUP_GRACE.total_seconds() == 600


def test_plain_keeps_the_exchange_and_never_invents_a_cause():
    line = plain("RivaL streaming forward proof failed venue=okx asset=BTC")
    assert line == "After-fee check failed (okx/BTC)"
    assert "no live price" not in line
    assert plain("RivaL perp collection FAILED venue=gate asset=XRP reason=timeout") \
        .startswith("gate/XRP")
