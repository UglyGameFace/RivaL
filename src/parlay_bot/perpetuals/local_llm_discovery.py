"""Bounded authenticated peer discovery across Discloud account VLAN interfaces.

Discloud's configured VLAN aliases may not be registered in Docker DNS.
Broadcast is restricted to the two attached local interfaces; responses must
have the correct HMAC and originate from a subnet directly attached to RivaL.
Never cache an IP permanently; rediscover after a connection failure/redeploy.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import time

PORT = 29785
REQUEST = b"RIVAL_LLM_DISCOVER_V1"
RESPONSE = b"RIVAL_LLM_REPLY_V1"


def _mac(token: str, direction: bytes, nonce: bytes) -> bytes:
    return hmac.new(token.encode("utf-8"), direction + b"|" + nonce,
                    hashlib.sha256).hexdigest().encode("ascii")


def _interface_networks() -> list[ipaddress.IPv4Network]:
    """Read only assigned interfaces, never enumerate other hosts or networks."""
    import fcntl
    import struct

    result = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for name in (b"eth0", b"eth1"):
            try:
                ip = socket.inet_ntoa(fcntl.ioctl(
                    control.fileno(), 0x8915, struct.pack("256s", name))[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(
                    control.fileno(), 0x891b, struct.pack("256s", name))[20:24])
                subnet = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
            except (OSError, ValueError):
                continue
            # Discloud's VLAN uses /22s; reject loops, point-to-point and
            # unbounded broadcast ranges even if host configuration changes.
            if 20 <= subnet.prefixlen <= 29 and not subnet.is_loopback:
                result.append(subnet)
    return list(dict.fromkeys(result))


def valid_reply(message: bytes, token: str, nonce: bytes) -> bool:
    parts = message.split(b"|")
    return (len(parts) == 3 and parts[0] == RESPONSE
            and hmac.compare_digest(parts[1], nonce)
            and hmac.compare_digest(parts[2], _mac(token, b"reply", nonce)))


def discover_private_llm(token: str) -> str | None:
    """Return the authenticated peer IP, or None after <= ~1.5s."""
    if not token or len(token) < 32:
        return None
    try:
        networks = _interface_networks()
    except (ImportError, OSError):
        return None
    if not networks:
        return None
    nonce = secrets.token_hex(16).encode("ascii")
    request = b"|".join((REQUEST, nonce, _mac(token, b"request", nonce)))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        for _ in range(3):
            for network in networks:
                try:
                    sock.sendto(request, (str(network.broadcast_address), PORT))
                except OSError:
                    continue
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    message, peer = sock.recvfrom(180)
                except TimeoutError:
                    break
                except OSError:
                    return None
                try:
                    source = ipaddress.ip_address(peer[0])
                except ValueError:
                    continue
                if (isinstance(source, ipaddress.IPv4Address)
                    and any(source in subnet for subnet in networks)
                    and valid_reply(message, token, nonce)):
                    return str(source)
    return None
