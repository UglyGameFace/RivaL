"""Authoritative read-only research market definitions.

Every horizon is the time until a future FIVE-minute candle opens.
This is NOT the resolution of a Polymarket Up/Down contract.
Never upgrade historical subscriber scopes without consent.
"""
from __future__ import annotations

ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)
ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum",
               "SOL": "Solana", "XRP": "XRP"}
LEGACY_ALERT_ASSETS = ("BTC", "ETH", "SOL")
LEGACY_ALERT_HORIZONS = (5, 15)


def asset_for_contract(contract: str) -> str | None:
    """Recognize real source contracts without fabricating venue support."""
    if contract.startswith("XBT"):
        return "BTC"
    return next((asset for asset in ASSETS if contract.startswith(asset)), None)
