"""Authenticated, account-VLAN peer discovery when Discloud hostname aliases are absent.

UDP requests and responses are HMAC-SHA256 authenticated with the existing
RIVAL_LLM_SHARED_TOKEN. Discovery does not expose the token or model data and
cannot invoke any inference endpoint. The HTTP gateway still requires bearer auth.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import socket
import threading

PORT = 29785
REQUEST = b"RIVAL_LLM_DISCOVER_V1"
RESPONSE = b"RIVAL_LLM_REPLY_V1"
_LOG = logging.getLogger("rival_llm")


def _mac(token: str, direction: bytes, nonce: bytes) -> bytes:
    return hmac.new(token.encode("utf-8"), direction + b"|" + nonce,
                    hashlib.sha256).hexdigest().encode("ascii")


def respond(payload: bytes, token: str) -> bytes | None:
    if len(token) < 32 or len(payload) > 160:
        return None
    fields = payload.split(b"|")
    if len(fields) != 3 or fields[0] != REQUEST:
        return None
    nonce, supplied = fields[1:]
    if len(nonce) != 32 or any(ch not in b"0123456789abcdef" for ch in nonce):
        return None
    if not hmac.compare_digest(supplied, _mac(token, b"request", nonce)):
        return None
    return b"|".join((RESPONSE, nonce, _mac(token, b"reply", nonce)))


def serve(token: str, stopped: threading.Event, port: int = PORT) -> None:
    """Small fixed-capacity responder. No DB, filesystem, or inference access."""
    if len(token) < 32:
        raise ValueError("discovery requires private gateway token")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.settimeout(0.5)
        _LOG.info("private_peer_discovery_ready port=%d", port)
        while not stopped.is_set():
            try:
                packet, address = sock.recvfrom(161)
            except socket.timeout:
                continue
            except OSError:
                if not stopped.is_set():
                    _LOG.warning("private_peer_discovery_receive_failed")
                break
            reply = respond(packet, token)
            if reply is not None:
                try:
                    sock.sendto(reply, address)
                except OSError:
                    _LOG.warning("private_peer_discovery_reply_failed")
