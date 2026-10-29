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
