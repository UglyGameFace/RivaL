"""The Discord loop must not queue behind CPU-bound worker threads for seconds."""
from __future__ import annotations

import sys
from unittest.mock import patch

from parlay_bot.discord_app import bot


def test_bot_startup_shortens_the_gil_switch_interval():
    assert bot.GIL_SWITCH_INTERVAL <= 0.001
    settings = type("S", (), {"discord_token": type("T", (), {
        "get_secret_value": lambda self: "x"})()})()
    before = sys.getswitchinterval()
    try:
        with patch.object(bot, "RivalDiscordClient") as client:
            bot.run_discord_bot(settings)
        assert abs(sys.getswitchinterval() - bot.GIL_SWITCH_INTERVAL) < 1e-9
        client.return_value.run.assert_called_once()
    finally:
        sys.setswitchinterval(before)
