"""Kalshi BRTI collector: protocol parsing, signing, inert-without-creds.

Message fixtures follow Kalshi's official ``cfbenchmarks_value``
documentation (the docs' own example frame). The RSA key below is a
throwaway key generated for these tests only -- it guards nothing.
"""
import base64
import hashlib
import json
from datetime import UTC, datetime

import pytest

from parlay_bot.perpetuals.kalshi_brti import (
    KalshiBRTI,
    _mgf1,
    _parse_rsa_private_key,
    kalshi_auth_headers,
    kalshi_sign,
    parse_cfbenchmarks_message,
)

TEST_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDdKenceDMGW7qD
zZpJemOT8gZLyh2btljTQfNK5GkHlWYJ6rF3M5L4wMppMSY5yQSvJx/W2wrWulET
tZuKoGokpNevZB0tIARBtRTGXt9olpxHD3DarfzBoXp0MnFULuoPnJjX6R+sGa0H
9Lm1I7t/9PZKsyHLsPfCRcV87DhITbFhMG94GxzsyODiqkNPtTPsBBnnn2Zv6CdU
PW9QR5WtIK/gmDMspe/030rkRQ1US/kh7pMcIIgPtzCjIO4hMC7HEnMczducJB/5
Io571fR5d7Jq6GoA0LINdES3NG3fQyDkGyhYEhIuUq2iFq5e8wjCWIj28F3rAQvk
HVsfXRs/AgMBAAECggEAYy8b2SryvyQ+FkQLsG454CyHVE/1TVLOrReDk4+x5pvC
oinloq63d+WGIPzQxtN2zVI/fizHxJrUJ4GnmtjsITjoHZlJWGZmYk1iXsBHcvEw
btHm67thgjj34MqKdpV/lLbG+PVspdLx/YJsEmdO/EiQd9m0C1R3GGIP+kE50Hpp
WLWT4k6qNneJjOCbYrntRzmzX1kny5SGtj31GUdXCpLzzfBjwC7ZmRsrw5n21K2e
Cd3SaVc9UykE2/YOD1N8QS40/33oTBuRQS4sWZ8PcOpI3BtES8wEHJTknNmA2qkV
C35ocbz89NGsDsiriLK7MmJdyPK4At9Ny3qmtBSPAQKBgQD/4J4N4eSEtgnVVGzr
RxhVrtRLkNjePnqTuPlrXx3koEH7kQXwJchUEi4AIRFNV0GQCxs8k+JUV7baaP7F
ZID3DqFShrN9SwDxjc0eRRjpZkhGw/KrPza7OEh1BC5N1EKkIhbTNNUItSNhQ+/H
aIcO1esFNcInIpiHbZZ2dkUqvwKBgQDdRQnhGHx2fudARA3NNVhqPp/FD+5n47Ij
7U847rqZfXUXKcsxrg959/S+nAa5Mphwz5PmSrM5zb0lhSVffFi2TJ1s8S+UWIrP
wDgZEhCtdV5RYz8C69w/zRaMVBvBSuQzB7hl5QtQPH7kifC0cwAwZ47qp1DR3KXd
rJq3RjavgQKBgQDGcuKiSav98WE2/r9qyjzpVyp3YOORmkte/nsS9zYr9s612QgN
9H+mFGNw3jSZEy9aFAMPvHIwj8bVJdFSLlyHP+WGJ8pVRjJ5JU0MSPLFMfqkR5yR
i/lawpAPkSqIWjNFMNsa+0Kt+i5yZUo2Ea+NpgMci1EFISPDBQ9I2PlT6QKBgCFb
fxnILztN9l7h45LEMAgUWNHEjRRsZzazV/YlR0xMdZKAYnmi3AJvNx5lNz5EOmcH
ZCybpllYguNdJz016cnR0wnRcnLSxlp7h/l/l7QOtaFfbOhr1lBoArY1F9oUCEMJ
LuttlD8MX7Tkdr09eRHTCuClSLHaqca5drmahDQBAoGAYk1rIlx6+2RAzmgFNKU6
Rwhpyc2A18qHiWJm6wtzvkcRw+987KIElV+pHjXuW3rQ5lmYjysEG7ZM6uHHI+xJ
r9FKXVx6E7eJKdMsScle9UtHI32ZaGUlb5cJCZdV8gS/dVwIcb57To2V0pjk+tsq
IyGOE5vFNEGJrGr24KmtGzE=
-----END PRIVATE KEY-----"""

RECEIVED = datetime(2026, 11, 20, 12, 0, 1, tzinfo=UTC)


def _docs_message(**overrides):
    """The example frame from Kalshi's cfbenchmarks_value documentation."""
    message = {
        "type": "cfbenchmarks_value",
        "sending_ts_ms": 1669149841234,
        "sid": 1,
        "seq": 42,
        "msg": {
            "index_id": "BRTI",
            "received_at": 1710000000123,
            "data": json.dumps({
                "type": "value", "id": "BRTI",
                "time": 1710000000123, "value": "68000.12",
            }),
            "avg_60s_data": {
                "value": "68000.12000000", "window_size": 3,
                "window_start_ts_ms": 1709999940123,
                "window_end_ts_exclusive": 1710000000123,
            },
        },
    }
    message.update(overrides)
    return message


def test_parse_docs_example_frame():
    tick = parse_cfbenchmarks_message(_docs_message(), received_at=RECEIVED)
    assert tick is not None
    assert tick.venue == "kalshi"
    assert tick.asset == "BTC"
    assert tick.value == pytest.approx(68000.12)
    assert tick.avg_60s == pytest.approx(68000.12)
    assert tick.source_at == datetime.fromtimestamp(1710000000123 / 1000, UTC)
    assert tick.received_at == RECEIVED
    assert len(tick.receipt_sha256) == 64


@pytest.mark.parametrize("index_id,asset", [
    ("ETHUSD_RTI", "ETH"), ("SOLUSD_RTI", "SOL"), ("XRPUSD_RTI", "XRP"),
])
def test_parse_maps_each_index_id(index_id, asset):
    message = _docs_message()
    message["msg"]["index_id"] = index_id
    message["msg"]["data"] = json.dumps({
        "type": "value", "id": index_id, "time": 1710000000123, "value": "100.5"})
    tick = parse_cfbenchmarks_message(message, received_at=RECEIVED)
    assert tick is not None and tick.asset == asset


@pytest.mark.parametrize("mutate", [
    lambda m: m.update({"type": "cfbenchmarks_value_indexlist"}),
    lambda m: m["msg"].update({"index_id": "DOGEUSD_RTI"}),
    lambda m: m["msg"].update({"data": "not json"}),
    lambda m: m["msg"].update({"data": json.dumps(
        {"type": "value", "id": "ETHUSD_RTI", "time": 1710000000123,
         "value": "1.0"})}),  # payload id disagrees with index_id
    lambda m: m["msg"].update({"data": json.dumps(
        {"type": "value", "id": "BRTI", "time": 1710000000123,
         "value": "-3"})}),  # unusable price
    lambda m: m.update({"msg": "garbage"}),
])
def test_parse_drops_unusable_frames(mutate):
    message = _docs_message()
    mutate(message)
    assert parse_cfbenchmarks_message(message, received_at=RECEIVED) is None


def test_parse_without_any_timestamp_is_dropped():
    message = _docs_message()
    message["msg"]["data"] = json.dumps(
        {"type": "value", "id": "BRTI", "value": "1.0"})
    message["msg"].pop("received_at")
    assert parse_cfbenchmarks_message(message, received_at=RECEIVED) is None


def test_parse_falls_back_to_kalshi_receipt_time():
    message = _docs_message()
    message["msg"]["data"] = json.dumps(
        {"type": "value", "id": "BRTI", "value": "1.0"})
    tick = parse_cfbenchmarks_message(message, received_at=RECEIVED)
    assert tick is not None
    assert tick.source_at == datetime.fromtimestamp(1710000000123 / 1000, UTC)


def test_parse_accepts_a_dict_payload_and_missing_average():
    message = _docs_message()
    message["msg"]["data"] = {
        "type": "value", "id": "BRTI", "time": 1710000000123, "value": "1.5"}
    message["msg"].pop("avg_60s_data")
    tick = parse_cfbenchmarks_message(message, received_at=RECEIVED)
    assert tick is not None and tick.avg_60s is None


def _pss_verify(modulus, exponent, message: str, signature_b64: str) -> bool:
    """EMSA-PSS verification, the mirror of the production signer."""
    signature = base64.b64decode(signature_b64)
    key_bytes = (modulus.bit_length() + 7) // 8
    if len(signature) != key_bytes:
        return False
    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus)
    em = encoded.to_bytes(key_bytes, "big")
    if em[-1] != 0xBC:
        return False
    h_prime = em[key_bytes - 33:key_bytes - 1]
    masked = bytearray(em[:key_bytes - 33])
    unused_bits = 8 * key_bytes - (modulus.bit_length() - 1)
    if unused_bits:
        masked[0] &= 0xFF >> unused_bits
    mask = _mgf1(h_prime, len(masked))
    db = bytes(a ^ b for a, b in zip(masked, mask))
    if unused_bits:
        db = bytes([db[0] & (0xFF >> unused_bits)]) + db[1:]
    split = len(db) - 33
    if db[:split] != b"\x00" * split or db[split] != 0x01:
        return False
    salt = db[split + 1:]
    digest = hashlib.sha256(message.encode()).digest()
    return hashlib.sha256(b"\x00" * 8 + digest + salt).digest() == h_prime


def test_signer_produces_verifiable_kalshi_signatures():
    modulus, exponent, _ = _parse_rsa_private_key(TEST_KEY_PEM)
    message = "1700000000000GET/trade-api/ws/v2"
    signature = kalshi_sign(TEST_KEY_PEM, message)
    assert _pss_verify(modulus, exponent, message, signature)
    assert not _pss_verify(modulus, exponent, message + "x", signature)


def test_auth_headers_carry_the_signed_timestamp():
    headers = kalshi_auth_headers("key-id-1", TEST_KEY_PEM, timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "key-id-1"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    modulus, exponent, _ = _parse_rsa_private_key(TEST_KEY_PEM)
    assert _pss_verify(modulus, exponent, "1700000000000GET/trade-api/ws/v2",
                       headers["KALSHI-ACCESS-SIGNATURE"])


def test_unparseable_key_is_not_configured():
    client = KalshiBRTI(key_id="k", private_key_pem="not a pem")
    assert not client.configured


async def test_client_without_credentials_is_inert_and_logs_once(caplog):
    client = KalshiBRTI(key_id=None, private_key_pem=None)
    assert not client.configured
    with caplog.at_level("WARNING"):
        await client.run()
        await client.run()
    assert client.status()["accepted_index_samples"] == 0
    warnings = [r for r in caplog.records if "credentials are not configured" in r.message]
    assert len(warnings) == 1


def test_configured_client_accepts_ticks_to_its_sink():
    seen = []
    client = KalshiBRTI(key_id="k", private_key_pem=TEST_KEY_PEM,
                        tick_sink=seen.append)
    assert client.configured
    assert client.accept(_docs_message(), received_at=RECEIVED) == 1
    assert len(seen) == 1 and seen[0].asset == "BTC"
    assert client.accept({"type": "subscribed"}, received_at=RECEIVED) == 0
    assert client.status()["accepted_index_samples"] == 1
