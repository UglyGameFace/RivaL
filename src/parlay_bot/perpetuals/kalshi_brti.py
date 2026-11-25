"""Kalshi CF Benchmarks (BRTI) settlement-index stream, read-only.

Kalshi's KX<COIN>15M contracts settle on CF Benchmarks' Real Time Index:
the 60-second average at the close against the 60-second average at the
open. That index exists in exactly one live place -- Kalshi's
authenticated websocket channel ``cfbenchmarks_value`` (Kalshi API docs,
"CF Benchmarks Value Feed"), which streams each index level at ~1/sec
together with the venue-computed trailing 60-second average
(``avg_60s_data``). No code in this codebase subscribed to it, and the
series cannot be reconstructed after the fact, so the fair-value
pre-test's Kalshi side starts recording the day this collector runs.

Protocol (per the official docs): connect to
``wss://external-api-ws.kalshi.com/trade-api/ws/v2`` with the standard
Kalshi signed headers (KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE; the
signature is RSA-PSS/SHA-256 over ``<timestamp_ms>GET/trade-api/ws/v2``),
then ``{"cmd": "subscribe", "params": {"channels": ["cfbenchmarks_value"],
"index_ids": [...]}}``. Index IDs: BTC is ``BRTI``; ETH/SOL/XRP are
``<COIN>USD_RTI``. Each ``cfbenchmarks_value`` message carries ``msg.data``
-- the raw CF Benchmarks frame as a JSON string (``{"type": "value",
"id": ..., "time": <ms>, "value": "<decimal>"}``) -- plus the parsed
``avg_60s_data`` object whose ``value`` is the trailing 60-second average.

The signer is pure standard library (DER parsing + EMSA-PSS encoding +
``pow``): the bot's dependency set has no ``cryptography`` package, in
CI or in the production container, and a read-only index feed does not
justify a new native dependency.

Credentials are Kalshi API credentials (a key id plus the RSA private
key PEM). When they are not configured the client is inert: ``run()``
logs once that credentials are missing and returns, so the bot starts
cleanly and the Kalshi side of the fair-value sample simply does not
accrue until the credentials exist. No account/order actions anywhere:
this client can only listen.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import random
import secrets
import time
from datetime import UTC, datetime, timedelta

from websockets.asyncio.client import connect

from parlay_bot.perpetuals.settlement_index import IndexTick, frame_receipt

_LOG = logging.getLogger(__name__)
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
CHANNEL = "cfbenchmarks_value"
# A tick older than this at receipt, or further than this in the
# future, is not a live print (cf. polybolt_twap.MAX_SAMPLE_AGE).
MAX_TICK_AGE = timedelta(minutes=3)
MAX_TICK_FUTURE = timedelta(seconds=3)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
INDEX_IDS = {
    "BRTI": "BTC",
    "ETHUSD_RTI": "ETH",
    "SOLUSD_RTI": "SOL",
    "XRPUSD_RTI": "XRP",
}
SUBSCRIBE_INDEX_IDS = tuple(INDEX_IDS)


# ---------------------------------------------------------------------------
# RSA-PSS (SHA-256) signing with the standard library only.
# ---------------------------------------------------------------------------

_SHA256_DIGEST_INFO_LEN = 32


def _der_read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one DER TLV at offset; returns (tag, content, next_offset)."""
    if offset + 2 > len(data):
        raise ValueError("truncated DER")
    tag = data[offset]
    first = data[offset + 1]
    pos = offset + 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or pos + count > len(data):
            raise ValueError("invalid DER length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    else:
        length = first
    if pos + length > len(data):
        raise ValueError("truncated DER value")
    return tag, data[pos:pos + length], pos + length


def _der_sequence_items(content: bytes) -> list[tuple[int, bytes]]:
    items = []
    pos = 0
    while pos < len(content):
        tag, value, pos = _der_read(content, pos)
        items.append((tag, value))
    return items


def _parse_rsa_private_key(pem: str) -> tuple[int, int, int]:
    """(modulus, public exponent, private exponent) from a PEM key.

    Accepts PKCS#8 (``BEGIN PRIVATE KEY``) wrapping a PKCS#1 RSA key and
    a bare PKCS#1 (``BEGIN RSA PRIVATE KEY``) -- the two shapes Kalshi's
    key download and standard tooling produce.
    """
    if not isinstance(pem, str):
        raise TypeError("private key must be PEM text")
    body = "".join(
        line.strip() for line in pem.splitlines()
        if line.strip() and not line.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError as exc:
        raise ValueError("private key is not valid PEM base64") from exc
    tag, content, _ = _der_read(der, 0)
    if tag != 0x30:
        raise ValueError("private key is not a DER sequence")
    items = _der_sequence_items(content)
    if len(items) >= 3 and items[1][0] == 0x30 and items[2][0] == 0x04:
        # PKCS#8: version, algorithm identifier, private key octet string.
        tag, content, _ = _der_read(items[2][1], 0)
        if tag != 0x30:
            raise ValueError("PKCS#8 payload is not an RSA key sequence")
        items = _der_sequence_items(content)
    integers = [int.from_bytes(value, "big") for t, value in items if t == 0x02]
    if len(integers) < 4:
        raise ValueError("RSA private key fields missing")
    # PKCS#1 order: version, modulus, public exponent, private exponent.
    _, modulus, exponent, private = integers[0], integers[1], integers[2], integers[3]
    if modulus <= 0 or exponent <= 0 or private <= 0:
        raise ValueError("RSA private key fields invalid")
    return modulus, exponent, private


def _mgf1(seed: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def kalshi_sign(private_key_pem: str, message: str) -> str:
    """Base64 RSA-PSS/SHA-256 signature, Kalshi's documented scheme.

    PSS with MGF1-SHA-256 and a salt the length of the digest -- the
    exact parameterisation Kalshi's official examples use
    (``salt_length=PSS.DIGEST_LENGTH``).
    """
    modulus, _, private = _parse_rsa_private_key(private_key_pem)
    key_bytes = (modulus.bit_length() + 7) // 8
    digest = hashlib.sha256(message.encode()).digest()
    salt = secrets.token_bytes(_SHA256_DIGEST_INFO_LEN)
    m_prime = b"\x00" * 8 + digest + salt
    h_prime = hashlib.sha256(m_prime).digest()
    db_len = key_bytes - _SHA256_DIGEST_INFO_LEN - 1
    ps = b"\x00" * (db_len - _SHA256_DIGEST_INFO_LEN - 1)
    db = ps + b"\x01" + salt
    mask = _mgf1(h_prime, db_len)
    masked = bytearray(a ^ b for a, b in zip(db, mask))
    unused_bits = 8 * key_bytes - (modulus.bit_length() - 1)
    if unused_bits:
        masked[0] &= 0xFF >> unused_bits
    encoded = bytes(masked) + h_prime + b"\xbc"
    signature = pow(int.from_bytes(encoded, "big"), private, modulus)
    return base64.b64encode(signature.to_bytes(key_bytes, "big")).decode()


def kalshi_auth_headers(key_id: str, private_key_pem: str, *,
                        timestamp_ms: int | None = None) -> dict:
    """The three signed headers a Kalshi websocket connect requires."""
    stamp = str(timestamp_ms if timestamp_ms is not None
                else int(time.time() * 1000))
    signature = kalshi_sign(private_key_pem, f"{stamp}GET{WS_PATH}")
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": stamp,
        "KALSHI-ACCESS-SIGNATURE": signature,
    }


# ---------------------------------------------------------------------------
# Message parsing (pure).
# ---------------------------------------------------------------------------

def _ms(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _price(value) -> float | None:
    try:
        level = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return level if math.isfinite(level) and level > 0 else None


def parse_cfbenchmarks_message(message, *, received_at: datetime) -> IndexTick | None:
    """One ``cfbenchmarks_value`` frame -> an IndexTick, or None.

    Fail-closed like every other input in this codebase: a frame that is
    not a value update for a tracked index, whose raw data does not parse,
    whose index id disagrees with its payload, or whose value/timestamp
    is unusable is dropped whole, never repaired.
    """
    if received_at.utcoffset() is None:
        raise ValueError("receipt clock must be aware")
    received = received_at.astimezone(UTC)
    if not isinstance(message, dict) or message.get("type") != CHANNEL:
        return None
    msg = message.get("msg")
    if not isinstance(msg, dict):
        return None
    index_id = msg.get("index_id")
    asset = INDEX_IDS.get(index_id) if isinstance(index_id, str) else None
    if asset is None:
        return None
    raw = msg.get("data")
    if isinstance(raw, str):
        try:
            frame = json.loads(raw)
        except ValueError:
            return None
        frame_text = raw
    elif isinstance(raw, dict):
        frame = raw
        frame_text = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    else:
        return None
    if not isinstance(frame, dict):
        return None
    if frame.get("type") not in (None, "value"):
        return None
    if frame.get("id") not in (None, index_id):
        return None
    value = _price(frame.get("value"))
    source_at = _ms(frame.get("time")) or _ms(msg.get("received_at"))
    if value is None or source_at is None:
        return None
    # Freshness bound, mirroring the PolyBolt Chainlink parser
    # (MAX_SAMPLE_AGE / +3s): a delayed, replayed or clock-skewed frame
    # must not enter the settlement-index series as a live print -- a
    # future print would additionally poison the recorder's ordering
    # and suppress every real tick stamped before it.
    if received - source_at > MAX_TICK_AGE \
            or source_at - received > MAX_TICK_FUTURE:
        return None
    avg_60s = None
    avg_block = msg.get("avg_60s_data")
    if isinstance(avg_block, dict):
        avg_60s = _price(avg_block.get("value"))
    return IndexTick(
        venue="kalshi", asset=asset, source_at=source_at,
        received_at=received, value=value, avg_60s=avg_60s,
        receipt_sha256=frame_receipt(CHANNEL, {
            "index_id": index_id, "data": frame_text,
        }),
    )


class KalshiBRTI:
    """Read-only BRTI stream client. No account/order actions."""

    def __init__(self, *, key_id: str | None, private_key_pem: str | None,
                 tick_sink=None):
        self._key_id = key_id if isinstance(key_id, str) and key_id.strip() else None
        self._pem = (private_key_pem if isinstance(private_key_pem, str)
                     and private_key_pem.strip() else None)
        self._key_valid = False
        if self._pem is not None:
            try:
                _parse_rsa_private_key(self._pem)
                self._key_valid = True
            except (TypeError, ValueError):
                _LOG.warning(
                    "RivaL Kalshi BRTI private key is not a parseable RSA key; "
                    "the index collector stays inert (no key material logged)")
        self.tick_sink = tick_sink
        self._last_error = "not connected"
        self._valid_observations = 0
        self._reconnects = 0
        self._missing_logged = False

    @property
    def configured(self) -> bool:
        return self._key_id is not None and self._pem is not None and self._key_valid

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "channel": CHANNEL,
            "accepted_index_samples": self._valid_observations,
            "reconnects": self._reconnects,
            "last_error": self._last_error,
        }

    def accept(self, message, *, received_at: datetime | None = None) -> int:
        """Apply one server frame; returns 1 when it carried a usable tick."""
        tick = parse_cfbenchmarks_message(
            message, received_at=received_at or datetime.now(UTC))
        if tick is None:
            return 0
        self._valid_observations += 1
        self._last_error = None
        if self.tick_sink is not None:
            try:
                self.tick_sink(tick)
            except Exception:  # noqa: BLE001 - recorder is downstream
                _LOG.warning(
                    "RivaL Kalshi BRTI tick sink failed; stream continues",
                    exc_info=True,
                )
        return 1

    async def run(self) -> None:
        """Single authenticated stream with bounded backoff.

        Without credentials this logs the gap once and returns: the
        collector is inert by design, and a supervisor must not spin it.
        """
        if not self.configured:
            self._last_error = "Kalshi API credentials not configured"
            if not self._missing_logged:
                self._missing_logged = True
                _LOG.warning(
                    "RivaL Kalshi BRTI index collector inert: Kalshi API "
                    "credentials are not configured (KALSHI_API_KEY_ID / "
                    "KALSHI_API_PRIVATE_KEY); the fair-value Kalshi sample "
                    "does not accrue until they are set")
            return
        backoff = 1.0
        while True:
            self._reconnects += 1
            try:
                headers = kalshi_auth_headers(self._key_id, self._pem)
                async with connect(
                        WS_URL, additional_headers=headers, max_size=65536,
                        open_timeout=10, ping_interval=10, close_timeout=4,
                ) as socket:
                    await socket.send(json.dumps({
                        "id": 1, "cmd": "subscribe",
                        "params": {
                            "channels": [CHANNEL],
                            "index_ids": list(SUBSCRIBE_INDEX_IDS),
                        },
                    }))
                    _LOG.info(
                        "RivaL Kalshi BRTI stream connected; subscribed to %s "
                        "for %s (credentials not logged)",
                        CHANNEL, ",".join(SUBSCRIBE_INDEX_IDS))
                    backoff = 1.0
                    async for raw in socket:
                        try:
                            frame = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(frame, dict) and frame.get("type") == "error":
                            self._last_error = "Kalshi websocket error frame"
                            _LOG.warning(
                                "RivaL Kalshi BRTI provider error frame "
                                "(detail not logged)")
                            continue
                        self.accept(frame)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never log headers or key material
                self._last_error = "Kalshi BRTI transport interrupted"
                _LOG.warning("RivaL Kalshi BRTI stream disconnected; read-only retry")
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff = min(backoff * 2, 30)
