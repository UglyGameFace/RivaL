"""Bounded shared reads: one computation per key, including cold-cache bursts.

Only descriptive dashboards use this cache. Issuance, settlement and admission
continue to read their authoritative ledgers. Failed reads are never cached.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future

from parlay_bot.storage.query_scope import current_statement_timeout_ms

_CREATION_LOCK = threading.Lock()
MAX_KEYS = 128


class SharedReads:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self.pending: dict[tuple, Future] = {}
        self.hits = self.joined = self.computed = 0

    def read(self, key: tuple, ttl: float, loader):
        with self.lock:
            value = self.values.get(key)
            if value is not None and time.monotonic() - value[0] < ttl:
                self.hits += 1
                self.values.move_to_end(key)
                return copy.deepcopy(value[1])
            future = self.pending.get(key)
            leader = future is None
            if leader:
                future = Future()
                self.pending[key] = future
                self.computed += 1
            else:
                self.joined += 1
        if not leader:
            timeout = current_statement_timeout_ms()
            return copy.deepcopy(future.result(timeout=(timeout or 60_000) / 1000))
        try:
            result = loader()
            with self.lock:
                if result is not None:
                    self.values[key] = (time.monotonic(), copy.deepcopy(result))
                    self.values.move_to_end(key)
                    while len(self.values) > MAX_KEYS:
                        self.values.popitem(last=False)
            future.set_result(result)
            return copy.deepcopy(result)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def snapshot(self) -> dict:
        with self.lock:
            return {"cached_keys": len(self.values), "in_flight": len(self.pending),
                    "cache_hits": self.hits, "coalesced_reads": self.joined,
                    "computations": self.computed}


def shared_reads(store) -> SharedReads:
    with _CREATION_LOCK:
        cache = getattr(store, "_shared_reads", None)
        if cache is None:
            cache = SharedReads()
            store._shared_reads = cache
        return cache
