"""Background Polymarket scans cannot flood the read-only Gamma/CLOB pool."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.polymarket_watch import (
    ASSETS,
    HORIZONS,
    PolymarketNotice,
    PolymarketReader,
)


@pytest.mark.asyncio
async def test_bulk_scan_bounded_without_losing_independent_contracts(monkeypatch):
    reader = PolymarketReader()
    admitted = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0
    received = []
    now = datetime.now(UTC)

    async def fake_market(asset, horizon, **kwargs):
        nonlocal active, maximum
        assert kwargs["now"] == now
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            admitted.set()
        try:
            await release.wait()
            received.append((asset, horizon))
            if asset == "ETH":
                # One provider failure must not cancel another exact market.
                raise TimeoutError("upstream market temporarily unavailable")
            return PolymarketNotice(
                asset=asset, horizon=horizon, issued=now,
                expires=now + timedelta(minutes=5),
                slug=f"{asset}-{horizon}",
                up_ask=0.50, down_ask=0.51, quote_at=now,
            )
        finally:
            active -= 1

    monkeypatch.setattr(reader, "market", fake_market)
    scan = asyncio.create_task(reader.active(now=now))
    await asyncio.wait_for(admitted.wait(), 1)
    assert active == 4
    assert maximum == 4
    assert len(received) == 0
    release.set()
    returned = await asyncio.wait_for(scan, 1)
    assert maximum == 4
    assert len(received) == len(ASSETS) * len(HORIZONS)
    assert len(returned) == (len(ASSETS) - 1) * len(HORIZONS)
    assert all(notice.asset != "ETH" for notice in returned)
