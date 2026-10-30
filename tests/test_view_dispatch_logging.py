"""View-dispatch logging must describe discards without changing dispatch.

discord.py discards a tap on a detached item with a warning that carries
no message_id, so the trigger could never be proven. The wrapper logs the
message_id, custom_id, and lookup source for detached and unknown taps,
passes every tap through to the original dispatch exactly once, and never
breaks setup when the store is missing or unexpected.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from parlay_bot.discord_app.home import install_view_dispatch_logging


class FakeStore:
    def __init__(self, views):
        self._views = views
        self.dispatched = []

    def dispatch_view(self, component_type, custom_id, interaction):
        self.dispatched.append((component_type, custom_id, interaction))


def _client(store):
    return SimpleNamespace(_connection=SimpleNamespace(_view_store=store))


def _interaction(message_id):
    message = SimpleNamespace(id=message_id) if message_id is not None else None
    return SimpleNamespace(message=message)


def test_detached_item_logs_message_id_and_still_dispatches(caplog):
    item = SimpleNamespace(view=None, custom_id="rival:guided:crypto")
    store = FakeStore({123: {(2, "rival:guided:crypto"): item}})
    install_view_dispatch_logging(_client(store))
    with caplog.at_level(logging.WARNING):
        store.dispatch_view(2, "rival:guided:crypto", _interaction(123))
    assert store.dispatched == [(2, "rival:guided:crypto", store.dispatched[0][2])]
    assert "DISCARDED detached item" in caplog.text
    assert "message_id=123" in caplog.text
    assert "custom_id=rival:guided:crypto" in caplog.text
    assert "found_under=message" in caplog.text


def test_persistent_fallback_hit_is_quiet_but_dispatches(caplog):
    item = SimpleNamespace(view=object(), custom_id="rival:guided:crypto")
    store = FakeStore({None: {(2, "rival:guided:crypto"): item}})
    install_view_dispatch_logging(_client(store))
    with caplog.at_level(logging.WARNING):
        store.dispatch_view(2, "rival:guided:crypto", _interaction(999))
    assert len(store.dispatched) == 1
    assert "DISCARDED" not in caplog.text
    assert "unknown" not in caplog.text


def test_unknown_tap_is_logged_and_dispatches(caplog):
    store = FakeStore({})
    install_view_dispatch_logging(_client(store))
    with caplog.at_level(logging.INFO):
        store.dispatch_view(2, "rival:guided:crypto", _interaction(999))
    assert len(store.dispatched) == 1
    assert "view dispatch unknown" in caplog.text
    assert "message_id=999" in caplog.text


def test_double_install_dispatches_exactly_once(caplog):
    item = SimpleNamespace(view=None, custom_id="rival:guided:crypto")
    store = FakeStore({123: {(2, "rival:guided:crypto"): item}})
    client = _client(store)
    install_view_dispatch_logging(client)
    install_view_dispatch_logging(client)
    with caplog.at_level(logging.WARNING):
        store.dispatch_view(2, "rival:guided:crypto", _interaction(123))
    assert len(store.dispatched) == 1
    assert caplog.text.count("DISCARDED detached item") == 1


def test_missing_store_never_breaks_setup(caplog):
    install_view_dispatch_logging(SimpleNamespace())
    install_view_dispatch_logging(SimpleNamespace(_connection=SimpleNamespace()))
    assert "view dispatch logging unavailable" in caplog.text
