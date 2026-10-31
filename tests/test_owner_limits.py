from __future__ import annotations

import pytest

from parlay_bot.providers.owner_limits import OwnerLimit, OwnerLimitStore
from parlay_bot.storage.hot import SQLiteHotStore


def test_owner_limit_persists_after_reopen(tmp_path):
    path = str(tmp_path / "limits.sqlite")
    store = SQLiteHotStore(path)
    limits = OwnerLimitStore(store)
    limits.save(OwnerLimit(provider="oddspapi", plan="free", maximum_units=500, reserve_units=50))
    store.close()
    again = SQLiteHotStore(path)
    loaded = OwnerLimitStore(again).get("oddspapi")
    assert loaded == OwnerLimit(provider="oddspapi", plan="free", maximum_units=500, reserve_units=50)
    OwnerLimitStore(again).save(
        OwnerLimit(provider="oddspapi", plan="paid", maximum_units=10000, reserve_units=100)
    )
    assert OwnerLimitStore(again).get("oddspapi").maximum_units == 10000
    again.close()


@pytest.mark.parametrize("kwargs", [
    {"provider": "unknown", "plan": "free", "maximum_units": 100, "reserve_units": 10},
    {"provider": "oddspapi", "plan": "free", "maximum_units": -1, "reserve_units": 0},
    {"provider": "oddspapi", "plan": "free", "maximum_units": 100, "reserve_units": 101},
    {"provider": "oddspapi", "plan": "bad plan", "maximum_units": 100, "reserve_units": 0},
])
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        OwnerLimit(**kwargs)


def test_confirmed_quota_change_audits_and_rejects_stale_other_menu(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "confirmed.sqlite"))
    limits = OwnerLimitStore(store)
    initial = OwnerLimit("sportsgameodds", "free", 500, 25)
    next_limit = OwnerLimit("sportsgameodds", "paid", 800, 100)
    try:
        assert limits.get("sportsgameodds") is None
        limits.save_if_unchanged(initial, expected=None)
        assert limits.get("sportsgameodds") == initial
        with pytest.raises(ValueError, match="changed in another menu"):
            limits.save_if_unchanged(next_limit, expected=None)
        limits.save_if_unchanged(next_limit, expected=initial)
        assert limits.get("sportsgameodds") == next_limit
        with pytest.raises(ValueError, match="changed in another menu"):
            limits.save_if_unchanged(initial, expected=initial)
        history = limits.history("sportsgameodds")
        assert len(history) == 2
        assert '"maximum_units": 800' in history[0]["new_json"]
        assert '"maximum_units": 500' in history[0]["previous_json"]
        assert history[1]["previous_json"] is None
    finally:
        store.close()


def test_programmatic_owner_upsert_keeps_append_only_history(tmp_path):
    store = SQLiteHotStore(str(tmp_path / "history.sqlite"))
    limits = OwnerLimitStore(store)
    try:
        limits.save(OwnerLimit("oddspapi", "free", 100, 10))
        limits.save(OwnerLimit("oddspapi", "paid", 200, 20))
        assert len(limits.history("oddspapi")) == 2
        assert limits.history("sportsgameodds") == []
        with pytest.raises(ValueError):
            limits.history("random")
    finally:
        store.close()
