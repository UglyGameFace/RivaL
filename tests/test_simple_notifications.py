"""End-to-end cancellation contract for the simplified notification center."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.alert_control import stop_all_notifications


@pytest.mark.asyncio
async def test_stop_all_revokes_every_independent_message_source():
    hunt = SimpleNamespace(stop=AsyncMock(return_value=True))
    target = SimpleNamespace(stop=AsyncMock(return_value=True))
    tracker = SimpleNamespace(stop=AsyncMock(return_value=True))
    message = await stop_all_notifications(
        42, auto_hunt=hunt, targets=target, tracker=tracker,
    )
    assert message == "Stopped all personal notifications and live tracking."
    for service in (hunt, target, tracker):
        service.stop.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_stop_all_runs_other_cancellations_when_one_fails():
    hunt = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("db outage")))
    target = SimpleNamespace(stop=AsyncMock(return_value=True))
    tracker = SimpleNamespace(stop=AsyncMock(return_value=True))
    message = await stop_all_notifications(
        42, auto_hunt=hunt, targets=target, tracker=tracker,
    )
    assert "partly confirmed" in message
    assert "optional alerts" in message
    target.stop.assert_awaited_once_with(42)
    tracker.stop.assert_awaited_once_with(42)
    assert "Stopped" in message


@pytest.mark.asyncio
async def test_stop_all_is_idempotent_and_never_claims_stopped_without_proof():
    all_off = SimpleNamespace(stop=AsyncMock(return_value=False))
    message = await stop_all_notifications(
        42, auto_hunt=all_off, targets=all_off, tracker=all_off,
    )
    assert message == "All personal notifications and live tracking are already off."
    assert all_off.stop.await_count == 3
    missing = await stop_all_notifications(
        42, auto_hunt=None, targets=None, tracker=None,
    )
    assert "Nothing was changed" in missing
