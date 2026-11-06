"""One checkpoint contest for book, pooled and reviewed price hypotheses.

The first snapshot 20% into a contract is immutable, including WAIT and
missing candidates. Later cheaper quotes or more favorable model outputs
cannot become another scored entry. Old score tables remain archival only.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.model_league import (
    FORMULA,
    JUMP,
    PRESSURE,
    Contest,
    checkpoint,
    settlement_schema,
)
from parlay_bot.perpetuals.price_formula import (
    chance_up,
    jump_chance,
    jump_parameters,
    per_second_sigma,
)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class FormulaTest:
    def __init__(self, store):
        self.store = store
        self.contest = Contest(store)
        self.latest: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._restored: set[str] = set()

    def initialize(self):
        self.contest.initialize()

    def consider(self, notice, *, reference: dict | None, learner,
                 now: datetime | None = None) -> dict | None:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        if not (notice.issued <= notice.quote_at <= stamp < notice.expires
                and stamp-notice.quote_at <= timedelta(seconds=25)):
            return None
        self.contest.initialize(now=stamp)
        with self._lock:
            if notice.slug in self.latest:
                return self.latest[notice.slug]
            restore = notice.slug not in self._restored
            self._restored.add(notice.slug)
            if len(self._restored) > 5000:
                self._restored = {notice.slug}
        if restore:
            first = self.contest.call(notice.slug, FORMULA)
            if first is not None:
                inputs = json.loads(first["evidence_json"])
                reading = {"slug": notice.slug, "chance_up": first["probability_up"],
                           "at": datetime.fromisoformat(first["issued_at"]),
                           "up_ask": inputs["up_ask"], "down_ask": inputs["down_ask"],
                           "strike_source": inputs["inputs"].get("strike_source", "unverified"),
                           "reference_label": inputs["inputs"].get("reference_label", "source-matched spot"),
                           "payoff": "TWAP moment approximation" if first["schema_key"].endswith("twap60") else "terminal digital"}
                with self._lock:
                    self.latest[notice.slug] = reading
                return reading
        if not checkpoint(notice, stamp):
            return None
        schema = settlement_schema(notice)
        if schema is None:
            return None
        forecasts, evidence, reading = {}, {}, None
        if reference is not None:
            native = "binance.com" in schema
            valid_reference = (
                reference.get("quote_currency") == ("USDT" if native else "USD")
                and timedelta(0) <= stamp-reference["at"] <= timedelta(seconds=15)
                and (not native or (reference.get("native_venue") == "binance_spot"
                                    and reference.get("opens_at") == int(notice.issued.timestamp())))
            )
            if not valid_reference:
                reference = None
        if reference is not None:
            price = reference["price"]
            published = notice.price_to_beat
            strike = published if published is not None else reference["open"]
            sigma = per_second_sigma(reference["closes"])
            left = (notice.expires-stamp).total_seconds()
            twap = 60 if schema.endswith("|twap60") else 0
            if strike is not None and sigma is not None:
                p = chance_up(price, strike, sigma, left, twap_seconds=twap)
                if p is not None:
                    evidence = {"sources": reference["sources"], "price": price,
                                "strike": strike, "strike_source": "published" if published is not None else reference.get("open_source", "three-spot-median-proxy"),
                                "quote_currency": reference["quote_currency"],
                                "reference_label": reference["reference_label"],
                                "sigma": sigma, "twap_seconds": twap,
                                "candle_sha256": _digest(reference["candles"]),
                                "last_closed_minute": reference["candles"][-1][0],
                                "candle_count": len(reference["candles"])}
                    forecasts[FORMULA] = (p, _digest({"model": FORMULA, "decay": .94}))
                    parameters = jump_parameters(reference["closes"])
                    if parameters is not None:
                        jp = jump_chance(price, strike, left, parameters, twap_seconds=twap)
                        if jp is not None:
                            forecasts[JUMP] = (jp, _digest({"model": JUMP, "threshold": 3, "min_returns": 119}))
                            evidence["jump_parameters"] = parameters
                    pressure = reference.get("microprice")
                    if pressure is not None:
                        pp = chance_up(pressure, strike, sigma, left, twap_seconds=twap)
                        if pp is not None:
                            forecasts[PRESSURE] = (pp, _digest({"model": PRESSURE}))
                            evidence["microprice"] = pressure
                    reading = {"slug": notice.slug, "chance_up": p, "at": stamp,
                               "up_ask": notice.up_ask, "down_ask": notice.down_ask,
                               "strike_source": evidence["strike_source"],
                               "reference_label": reference["reference_label"],
                               "payoff": "TWAP moment approximation" if twap else "terminal digital"}
        # All contestants use this book NOW. Never substitute a start-time
        # pooled price, a previous main-model call, or a later formula tick.
        forecasts.update(learner.contest_forecasts(notice))
        captured = self.contest.capture(notice, forecasts, evidence=evidence,
                                        now=now or datetime.now(UTC))
        if captured and reading is not None:
            with self._lock:
                self.latest[notice.slug] = reading
                self.latest = {k: v for k, v in self.latest.items()
                               if v["at"] > stamp-timedelta(hours=2)}
            return reading
        return None
