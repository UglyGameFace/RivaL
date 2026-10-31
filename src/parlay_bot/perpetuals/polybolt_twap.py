"""Authenticated Polymarket PolyBolt TWAP stream, independent of CLOB books.

Official v2 reference channel: price.crypto.twap, 60-second Chainlink only.
Never substitute a perp candle, RTDS, ordinary price.crypto or stale snapshots.
CLOB API credentials are required. The digest is of transport-received data,
NOT a signed oracle attestation.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import random
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from websockets.asyncio.client import connect

from parlay_bot.perpetuals.polymarket_oracle import OracleReference

_LOG = logging.getLogger(__name__)
WS = "wss://ws-live-v2.polymarket.com/ws"
CHANNEL = "price.crypto.twap"
ASSETS = ("BTC", "ETH", "SOL", "XRP")
SYMBOLS = {asset.lower()+"usd": asset for asset in ASSETS}
MAX_AGE = timedelta(seconds=8)
MAX_SAMPLE_AGE = timedelta(minutes=3)


@dataclass(frozen=True)
class TWAPTick:
    asset: str
    observed_at: datetime
    received_at: datetime
    price: float
    receipt_sha256: str


def _ms(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("missing source timestamp")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid PolyBolt source timestamp")
    return datetime.fromtimestamp(value/1000, UTC)


class PolyBoltTWAP:
    """Read-only, bounded verified TWAP snapshots. No account/order actions."""

    def __init__(self, *, api_key: str | None, secret: str | None,
                 passphrase: str | None):
        self._creds = (api_key, secret, passphrase)
        self._ticks: dict[str, deque[TWAPTick]] = {
            asset: deque(maxlen=220) for asset in ASSETS
        }
        self._sequence: int | None = None
        self._authenticated = False
        self._subscribed = False
        self._healthy_since: datetime | None = None
        self._last_error = "not connected"

    @property
    def configured(self) -> bool:
        return all(isinstance(x, str) and bool(x.strip()) for x in self._creds)

    def reset(self) -> None:
        self._sequence = None
        self._subscribed = False
        self._authenticated = False
        self._healthy_since = None
        for ticks in self._ticks.values():
            ticks.clear()

    def status(self, *, now: datetime | None = None) -> dict:
        checked = now or datetime.now(UTC)
        return {
            "configured": self.configured,
            "connected": self._authenticated and self._subscribed,
            "channel": CHANNEL,
            "assets_fresh": [
                asset for asset in ASSETS
                if self._ticks[asset]
                and timedelta(0) <= checked-self._ticks[asset][-1].observed_at <= MAX_AGE
            ],
            "last_error": self._last_error,
            "model_approved": False,
        }

    def accept(self, message: object, *, received_at: datetime | None = None) -> int:
        """Apply server frames. A dropped/gapped stream clears old volatility."""
        received = received_at or datetime.now(UTC)
        if received.utcoffset() is None:
            raise ValueError("receipt clock must be aware")
        received = received.astimezone(UTC)
        if not isinstance(message, dict):
            raise TypeError("invalid PolyBolt frame")
        operation = message.get("op")
        if operation == "authed":
            self._authenticated = True
            self._last_error = None
            return 0
        if operation == "error":
            self._last_error = str(message.get("code") or "provider_error")[:55]
            self._subscribed = False
            return 0
        if operation == "subscribed":
            if message.get("channel") == CHANNEL and self._authenticated:
                self._subscribed = True
            return 0
        if message.get("channel") != CHANNEL or not self._authenticated:
            return 0
        seq = message.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ValueError("missing source sequence")
        if message.get("dropped", 0) or (
            self._sequence is not None and seq != self._sequence + 1
        ):
            # Never continue volatility after an unobserved gap.
            for ticks in self._ticks.values():
                ticks.clear()
            self._last_error = "PolyBolt dropped or out-of-order frames"
            self._sequence = seq
            return 0
        if self._sequence is None and seq != 1:
            self._last_error = "PolyBolt stream lacks initial sequence"
            return 0
        self._sequence = seq
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return 0
        symbol = payload.get("symbol")
        if symbol not in SYMBOLS or payload.get("source") != "chainlink":
            return 0
        asset = SYMBOLS[symbol]
        batch = payload.get("data") if message.get("snapshot") else [payload]
        if not isinstance(batch, list) or len(batch) > 150:
            return 0
        updated = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            try:
                stamp = _ms(item.get("timestamp"))
                raw_price = item.get("full_accuracy_value")
                if raw_price is None:
                    raw_price = item.get("value")
                price = float(raw_price)
                if (
                    not math.isfinite(price) or price <= 0
                    or received-stamp > MAX_SAMPLE_AGE
                    or stamp-received > timedelta(seconds=3)
                    or stamp < received-timedelta(minutes=2, seconds=10)
                ):
                    continue
                # Hash the exact source frame fields; not a vendor signature.
                evidence = json.dumps({
                    "channel": CHANNEL, "symbol": symbol, "source": "chainlink",
                    "seq": seq, "timestamp": item["timestamp"],
                    "value": str(raw_price),
                }, sort_keys=True, separators=(",", ":")).encode()
                tick = TWAPTick(
                    asset, stamp, received, price, hashlib.sha256(evidence).hexdigest(),
                )
                stream = self._ticks[asset]
                if stream and stamp <= stream[-1].observed_at:
                    continue
                stream.append(tick)
                updated += 1
            except (ValueError, TypeError, OverflowError):
                continue
        if updated:
            self._healthy_since = received
            self._last_error = None
        return updated

    def reference(self, notice, *, now: datetime | None = None) -> OracleReference | None:
        """Time-aligned original-price input only when 60s data is substantial."""
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            return None
        current = current.astimezone(UTC)
        if not self._authenticated or not self._subscribed:
            return None
        if notice.asset not in ASSETS or notice.horizon not in (5, 15, 60):
            return None
        if not notice.price_to_beat or not notice.resolution_source:
            return None
        source = notice.resolution_source.lower()
        rules = (notice.rules or "").lower()
        if (
            not ("chainlink" in source or "data.chain.link" in source)
            or "twap" not in rules
            or re.search(r"60[ -]?(?:second|sec|s)", rules) is None
        ):
            return None
        data = self._ticks[notice.asset]
        if len(data) < 8 or not 0 <= (current-data[-1].observed_at).total_seconds() <= 8:
            return None
        if abs((notice.quote_at-data[-1].observed_at).total_seconds()) > 20:
            return None
        # Use non-overlapping 10-second steps in the observed source TWAP,
        # rather than pretending 1-second, overlapping TWAP ticks independent.
        sampled: list[TWAPTick] = []
        for tick in data:
            if tick.observed_at > current or tick.observed_at < current-timedelta(seconds=120):
                continue
            if not sampled or (tick.observed_at-sampled[-1].observed_at).total_seconds() >= 10:
                sampled.append(tick)
        if len(sampled) < 7 or sampled[-1].observed_at-sampled[0].observed_at < timedelta(seconds=60):
            return None
        squared = [
            math.log(b.price/a.price)**2
            for a, b in itertools.pairwise(sampled)
        ]
        mean_step = sum(
            (b.observed_at-a.observed_at).total_seconds()
            for a, b in itertools.pairwise(sampled)
        )/len(squared)
        remaining_seconds = (notice.expires-current).total_seconds()
        if not 5 < remaining_seconds <= 3600:
            return None
        volatility_pct = 100 * math.sqrt(
            sum(squared)/len(squared) * remaining_seconds/mean_step
        )
        if not math.isfinite(volatility_pct) or not 0 < volatility_pct <= 20:
            return None
        latest = data[-1]
        return OracleReference(
            asset=notice.asset,
            resolution_source=notice.resolution_source,
            observed_at=latest.observed_at,
            price=latest.price,
            horizon_volatility_pct=volatility_pct,
            source_receipt_sha256=latest.receipt_sha256,
            method="chainlink-twap-60s",
        )

    async def run(self) -> None:
        """Single authenticated stream with explicit reauth and bounded backoff."""
        if not self.configured:
            self._last_error = "CLOB API credentials not configured"
            return
        backoff = 1.
        while True:
            self.reset()
            try:
                async with connect(
                    WS, max_size=65536, open_timeout=10,
                    ping_interval=None, close_timeout=4,
                ) as socket:
                    key, secret, passphrase = self._creds
                    await socket.send(json.dumps({
                        "op": "auth", "rid": "rival-auth",
                        "auth": {
                            "apiKey": key, "secret": secret,
                            "passphrase": passphrase,
                        },
                    }))
                    auth = json.loads(await asyncio.wait_for(socket.recv(), timeout=8))
                    if auth.get("op") != "authed":
                        self._last_error = "PolyBolt authentication rejected"
                        return  # Credentials/entitlement need owner action.
                    self.accept(auth)
                    await socket.send(json.dumps({
                        "op": "subscribe", "rid": "rival-twap",
                        "subscriptions": [
                            {"channel": CHANNEL, "filter": {
                                "symbol": asset.lower()+"usd",
                                "window_seconds": 60,
                            }} for asset in ASSETS
                        ],
                    }))
                    backoff = 1.
                    async for raw in socket:
                        self.accept(json.loads(raw))
                        # Auth required/errors are not endlessly retried.
                        if self._last_error in ("auth_invalid", "auth_required"):
                            return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # Do not log request frames/credentials or remote payloads.
                self._last_error = "PolyBolt transport interrupted"
                _LOG.warning("RivaL PolyBolt TWAP disconnected; read-only retry")
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff = min(backoff * 2, 30)
