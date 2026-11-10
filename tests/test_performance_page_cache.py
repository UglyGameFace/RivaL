"""Performance scorecard caches: exchange + Polymarket reads reuse slow aggregates.

Mirrors the improvement-check cache tests in test_performance_consistency.py.
"""
from __future__ import annotations

import threading
import time as _time
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import home as home_module


@pytest.fixture(autouse=True)
def _clear_scorecard_caches():
    home_module._EXCHANGE_CACHE.clear()
    home_module._EXCHANGE_INFLIGHT.clear()
    home_module._POLYMARKET_CACHE.clear()
    home_module._POLYMARKET_INFLIGHT.clear()
    yield
    home_module._EXCHANGE_CACHE.clear()
    home_module._EXCHANGE_INFLIGHT.clear()
    home_module._POLYMARKET_CACHE.clear()
    home_module._POLYMARKET_INFLIGHT.clear()


def test_exchange_result_is_reused_after_a_slow_first_run(monkeypatch):
    calls = []

    def slow(_market, asset):
        calls.append(asset)
        return {"last_7d": {5: {"decided": 7, "correct": 3}}}

    monkeypatch.setattr(home_module, "_performance_exchange", slow)
    market = SimpleNamespace()
    first = home_module._cached_exchange(market, "BTC")
    second = home_module._cached_exchange(market, "BTC")
    assert first == second
    assert calls == ["BTC"]
    home_module._cached_exchange(market, "ETH")
    assert len(calls) == 2


def test_polymarket_result_is_reused_after_a_slow_first_run():
    calls = []

    def slow(asset, horizon):
        calls.append((asset, horizon))
        return {"settled_forward_contracts": 300, "forward_brier": 0.2387}

    first = home_module._cached_polymarket_status(slow, "BTC", 5)
    second = home_module._cached_polymarket_status(slow, "BTC", 5)
    assert first == second
    assert calls == [("BTC", 5)]
    home_module._cached_polymarket_status(slow, "BTC", 15)
    assert len(calls) == 2


def test_polymarket_cache_key_is_stable_across_bound_method_accesses():
    calls = []

    class Learner:
        def status(self, asset, horizon):
            calls.append((asset, horizon))
            return {"settled_forward_contracts": 12}

    learner = Learner()
    # Each attribute access builds a fresh bound-method object; the cache
    # must still treat them as the same callable.
    first = home_module._cached_polymarket_status(learner.status, "BTC", 5)
    second = home_module._cached_polymarket_status(learner.status, "BTC", 5)
    assert first == second
    assert calls == [("BTC", 5)]
    assert home_module._polymarket_cache_owner(learner.status) is learner


def test_stale_exchange_entry_recomputes(monkeypatch):
    calls = []

    def slow(_market, asset):
        calls.append(asset)
        return {"last_7d": {5: {"decided": 7}}}

    monkeypatch.setattr(home_module, "_performance_exchange", slow)
    market = SimpleNamespace()
    home_module._cached_exchange(market, "BTC")
    key = (id(market), "BTC")
    stamped, owner, result = home_module._EXCHANGE_CACHE[key]
    home_module._EXCHANGE_CACHE[key] = (
        stamped - home_module.EXCHANGE_CACHE_SECONDS - 10, owner, result,
    )
    home_module._cached_exchange(market, "BTC")
    assert calls == ["BTC", "BTC"]


def test_stale_polymarket_entry_recomputes(monkeypatch):
    calls = []

    def slow(asset, horizon):
        calls.append((asset, horizon))
        return {"settled_forward_contracts": 1}

    monkeypatch.setattr(home_module, "POLYMARKET_CACHE_SECONDS", 0)
    home_module._cached_polymarket_status(slow, "BTC", 5)
    home_module._cached_polymarket_status(slow, "BTC", 5)
    assert calls == [("BTC", 5), ("BTC", 5)]


def test_concurrent_exchange_requests_share_one_query(monkeypatch):
    calls = []

    def slow(_market, asset):
        calls.append(asset)
        _time.sleep(0.3)
        return {"last_7d": {5: {"decided": 7}}}

    monkeypatch.setattr(home_module, "_performance_exchange", slow)
    market = SimpleNamespace()
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(home_module._cached_exchange(market, "SOL")))
        for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len(results) == 3


def test_concurrent_polymarket_requests_share_one_query():
    calls = []

    def slow(asset, horizon):
        calls.append((asset, horizon))
        _time.sleep(0.3)
        return {"settled_forward_contracts": 1}

    results = []
    threads = [threading.Thread(
        target=lambda: results.append(
            home_module._cached_polymarket_status(slow, "SOL", 15)))
        for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len(results) == 3


def test_warm_exchange_cache_refreshes_every_coin(monkeypatch):
    calls = []

    def slow(_market, asset):
        calls.append(asset)
        return {"last_7d": {5: {"decided": 7}}}

    monkeypatch.setattr(home_module, "_performance_exchange", slow)
    assert home_module.warm_exchange_cache(SimpleNamespace()) == 4
    assert sorted(calls) == ["BTC", "ETH", "SOL", "XRP"]
    assert len(home_module._EXCHANGE_CACHE) == 4


def test_warm_polymarket_cache_refreshes_every_coin_and_horizon():
    calls = []

    def slow(asset, horizon):
        calls.append((asset, horizon))
        return {"settled_forward_contracts": 1}

    assert home_module.warm_polymarket_cache(slow) == 12
    assert len(calls) == 12
    assert len(home_module._POLYMARKET_CACHE) == 12
    assert ("BTC", 5) in [(a, h) for (_, a, h) in home_module._POLYMARKET_CACHE]
    assert ("XRP", 60) in [(a, h) for (_, a, h) in home_module._POLYMARKET_CACHE]


def test_warmers_tolerate_a_failing_key(monkeypatch):
    def failing_exchange(_market, asset):
        if asset == "ETH":
            raise RuntimeError("boom")
        return {"last_7d": {5: {"decided": 7}}}

    monkeypatch.setattr(home_module, "_performance_exchange", failing_exchange)
    assert home_module.warm_exchange_cache(SimpleNamespace()) == 3

    def failing_polymarket(asset, horizon):
        if (asset, horizon) == ("SOL", 15):
            raise RuntimeError("boom")
        return {"settled_forward_contracts": 1}

    assert home_module.warm_polymarket_cache(failing_polymarket) == 11


@pytest.mark.asyncio
async def test_render_reuses_cached_exchange_and_polymarket(tmp_path, monkeypatch):
    from tests.test_performance_overview import _close, _fields, _make_home, _score

    bot, home = _make_home(tmp_path)
    try:
        exchange_calls = []
        poly_calls = []

        def counting_exchange(*_):
            exchange_calls.append(1)
            return _score()

        original_poly = home.polymarket_status

        def counting_poly(asset, horizon):
            poly_calls.append((asset, horizon))
            return original_poly(asset, horizon)

        monkeypatch.setattr(home_module, "_performance_exchange", counting_exchange)
        home.polymarket_status = counting_poly
        first = _fields(await home.render())
        second = _fields(await home.render())
        exchange_name = "📈 BTC exchange predictions (up or down?)"
        poly_name = "🎯 Polymarket / Kalshi predictions"
        assert "Couldn't load the scores" not in first[exchange_name]
        assert "Couldn't load these scores" not in first[poly_name]
        assert first[exchange_name] == second[exchange_name]
        assert first[poly_name] == second[poly_name]
        assert len(exchange_calls) == 1
        assert len(poly_calls) == 1
    finally:
        await _close(bot, home)
