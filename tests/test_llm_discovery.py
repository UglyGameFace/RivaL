"""Authenticated Discloud VLAN service discovery security and integration."""
from __future__ import annotations

import importlib.util
import runpy
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
from parlay_bot.perpetuals import local_llm_discovery as client

server = runpy.run_path(str(ROOT / "services" / "rival-llm" / "discovery.py"))
TOKEN = "a" * 48


def test_signed_roundtrip_and_no_credential_leak():
    nonce = b"0123456789abcdef0123456789abcdef"
    packet = b"|".join((client.REQUEST, nonce, client._mac(TOKEN, b"request", nonce)))
    response = server["respond"](packet, TOKEN)
    assert response is not None
    assert TOKEN.encode() not in packet + response
    assert client.valid_reply(response, TOKEN, nonce)
    assert not client.valid_reply(response, "b" * 48, nonce)
    assert not client.valid_reply(response, TOKEN, b"f" * 32)
    assert server["respond"](packet, "b" * 48) is None
    assert server["respond"](b"RIVAL_LLM_DISCOVER_V1|fake|fake", TOKEN) is None
    assert server["respond"](b"x" * 161, TOKEN) is None


def test_udp_responder_requires_valid_mac_and_stops():
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    thread = threading.Thread(target=server["serve"], args=(TOKEN, stop, port),
                              daemon=True)
    thread.start()
    try:
        nonce = b"0123456789abcdef0123456789abcdef"
        request = b"|".join((client.REQUEST, nonce, client._mac(TOKEN, b"request", nonce)))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            for _ in range(4):
                try:
                    sock.sendto(request, ("127.0.0.1", port))
                    response, _ = sock.recvfrom(180)
                    break
                except socket.timeout:
                    continue
            else:
                pytest.fail("loopback responder did not acknowledge signed probe")
            assert client.valid_reply(response, TOKEN, nonce)
            sock.sendto(b"untrusted-packet", ("127.0.0.1", port))
            with pytest.raises(socket.timeout):
                sock.recvfrom(180)
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_missing_token_never_discovers(monkeypatch):
    monkeypatch.setattr(client, "_interface_networks", lambda: pytest.fail(
        "must not inspect interfaces without token"))
    assert client.discover_private_llm("") is None
    assert client.discover_private_llm("short") is None
