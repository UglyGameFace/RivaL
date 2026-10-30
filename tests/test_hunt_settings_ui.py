"""Mobile Discord menus and persistent owner preferences for Auto-Hunt."""
from __future__ import annotations

import os

import discord
import pytest

from parlay_bot.discord_app.auto_hunt import AutoHuntStore
from parlay_bot.discord_app.hunt_settings import HuntSettingsView
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def test_settings_view_is_small_and_exposes_real_controls(tmp_path):
    hot = SQLiteHotStore(tmp_path / "settings-ui.sqlite")
    hot.initialize()
    storage = AutoHuntStore(hot)
    storage.initialize()
    try:
        prefs = storage.preferences("101")
        view = HuntSettingsView(101, None, prefs)
        selects = [x for x in view.children if isinstance(x, discord.ui.Select)]
        buttons = [x for x in view.children if isinstance(x, discord.ui.Button)]
        assert len(selects) == 3
        assert len(buttons) == 4
        assert len(view.children) <= 25
        assert all(x.row <= 4 for x in view.children)
        assert any(x.label == "XRP" for x in selects[0].options)
        assert any(x.value == "up" for x in selects[2].options)
        assert "not a probability" in view.render()
        shifts = HuntSettingsView(101, None, prefs, page="shifts")
        assert "Live Price Shifts: OFF" in [x.label for x in shifts.children]
        assert "New Model Reversals: OFF" in [x.label for x in shifts.children]
    finally:
        hot.close()


def test_preferences_do_not_opt_in_other_alert_tiers_by_default(tmp_path):
    hot = SQLiteHotStore(tmp_path / "scope.sqlite")
    hot.initialize()
    storage = AutoHuntStore(hot)
    storage.initialize()
    try:
        prefs = storage.preferences("102")
        assert prefs["live_shifts"] is False
        assert prefs["direction_changes"] is False
        assert storage.subscribers() == []
        storage.set_preference("102", "assets", ("XRP",))
        storage.set_preference("102", "live_shifts", 1)
        assert storage.subscribers() == []  # only a verified DM probe may subscribe
        assert storage.preferences("102")["assets"] == ("XRP",)
    finally:
        hot.close()


def test_postgresql_stores_and_restores_settings_additively():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("CI Postgres 16 not connected")
    hot = PostgresHotStore(dsn)
    store = AutoHuntStore(hot)
    store.initialize()
    owner = "rival-live-preferences-postgres-ci"
    try:
        store.set_preference(owner, "assets", ("XRP", "ETH"))
        store.set_preference(owner, "side", "down")
        store.set_preference(owner, "max_ask_pct", 50)
        store.set_preference(owner, "live_shifts", 1)
        restarted = AutoHuntStore(hot)
        restarted.initialize()
        value = restarted.preferences(owner)
        assert value["assets"] == ("ETH", "XRP")
        assert value["side"] == "down"
        assert value["max_ask_pct"] == 50
        assert value["live_shifts"] is True
        assert all(x['user_id'] != owner for x in restarted.subscribers())
    finally:
        with hot.connect() as conn:
            conn.execute(
                "DELETE FROM rival_hunt_preferences WHERE user_id=?", (owner,),
            )
        hot.close()
