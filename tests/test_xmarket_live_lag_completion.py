"""Complete native PM inputs before genuine future XMarket shadow issuance."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.features import persisted_candles
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.xmarket_inputs import XmarketInputs
from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
from parlay_bot.research.xmarket_features import live_vector, sampled_bars
from tests.test_volcal_v1_shadow import _seed_cycle_candles
from tests.xmarket_fixtures import NOW, point, valid_doc


def _notice(opened, *, asset="BTC"):
    row = point("polymarket", opened + timedelta(minutes=1), asset=asset)
    return SimpleNamespace(horizon=15, issued=opened, expires=opened + timedelta(minutes=15),
        asset=asset, slug=row["slug"], condition_id=row["contract_id"],
        up_token_id=row["token_id"], quote_at=opened + timedelta(minutes=1),
        up_bid=.52, up_ask=.54)


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "XRP"])
async def test_completed_contract_lags_recover_after_rollover_grace_period(
        learning_store, asset):
    inputs = XmarketInputs(learning_store)
    opened = NOW.replace(minute=0, second=0, microsecond=0)
    clock = opened + timedelta(minutes=3)
    targets = [opened - timedelta(minutes=n) for n in (0, 15, 60)]
    original = [point("polymarket", t - timedelta(minutes=4), asset=asset) for t in targets]
    inputs.record(original)
    original_bytes = {p["at"]: json.dumps(p, sort_keys=True) for p in original}
    requests = []

    def serve(request):
        start = int(request.url.params["startTs"])
        requests.append(start)
        at = (opened + timedelta(minutes=1) if start == int(opened.timestamp())
              else datetime.fromtimestamp(start, UTC) + timedelta(minutes=15))
        return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .61}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        assert await inputs.capture_polymarket(_notice(opened, asset=asset), client, now=clock) == 4
        # Live polling continues, but missing-lag requests are bounded to one
        # attempt per required bar. Existing recovered prices stay immutable.
        assert await inputs.capture_polymarket(
            _notice(opened, asset=asset), client, now=clock + timedelta(minutes=1)) == 0
    assert len(requests) == 5  # two live requests and three distinct old contracts
    rows = inputs.recent(asset, at=opened, now=clock)
    bars = sampled_bars(rows, asset=asset, as_of=clock)["polymarket"]
    assert all(bars[t]["price"] == .61 for t in targets)
    assert all(bars[t]["observed_at"] == clock.isoformat() for t in targets)
    earlier = sampled_bars(rows, asset=asset, as_of=clock - timedelta(seconds=1))
    assert not earlier["polymarket"]
    with learning_store.connect() as conn:
        for old_at, expected in original_bytes.items():
            row = conn.execute("SELECT sample_json FROM rival_xmarket_observations "
                "WHERE asset=? AND source='polymarket' AND at=?", (asset, old_at)).fetchone()
            assert json.dumps(json.loads(row["sample_json"]), sort_keys=True) == expected


@pytest.mark.asyncio
async def test_each_lag_failure_is_isolated_and_missing_exact_price_is_not_filled(
        learning_store, caplog):
    inputs = XmarketInputs(learning_store)
    opened = NOW.replace(minute=0, second=0, microsecond=0)
    clock = opened + timedelta(minutes=3)
    targets = [opened - timedelta(minutes=n) for n in (0, 15, 60)]
    inputs.record([point("polymarket", t - timedelta(minutes=4)) for t in targets])
    requests = []

    def serve(request):
        start = int(request.url.params["startTs"])
        requests.append(start)
        end = datetime.fromtimestamp(start, UTC) + timedelta(minutes=15)
        if end == opened:
            return httpx.Response(503)  # old rollover request fails, live capture already saved
        if end == targets[1]:
            return httpx.Response(200, json={"history": []})  # provider has no exact 15m price
        at = opened + timedelta(minutes=1) if start == int(opened.timestamp()) else end
        return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .61}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        assert await inputs.capture_polymarket(_notice(opened), client, now=clock) == 2
        assert await inputs.capture_polymarket(
            _notice(opened), client, now=clock + timedelta(minutes=1)) == 0
    assert len(requests) == 5
    assert "PM 0m lag recovery unavailable" in caplog.text
    bars = sampled_bars(inputs.recent("BTC", at=opened, now=clock), asset="BTC")
    assert targets[0] not in bars["polymarket"]
    assert targets[1] not in bars["polymarket"]
    assert bars["polymarket"][targets[2]]["price"] == .61
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_xmarket_observations "
            "WHERE at=?", ((opened + timedelta(minutes=1)).isoformat(),)).fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_next_bar_has_its_own_recovery_budget_without_duplicate_live_request(learning_store):
    inputs = XmarketInputs(learning_store)
    opened = NOW.replace(minute=0, second=0, microsecond=0)
    clock = opened + timedelta(minutes=3)
    # Each next-bar target has an observed native identity. A rolling
    # five-minute cooldown must not skip that new target when polling drifts.
    for minutes in (15, 60):
        inputs.record([point("polymarket", opened - timedelta(minutes=minutes + 4) +
                             timedelta(minutes=offset)) for offset in (0, 5)])
    requests = []

    def serve(request):
        start = int(request.url.params["startTs"])
        requests.append(start)
        if start == int(opened.timestamp()):
            # Sparse current source: no second request to the same current contract.
            at = opened + timedelta(minutes=1)
            return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .61}]})
        return httpx.Response(200, json={"history": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        await inputs.capture_polymarket(_notice(opened), client, now=clock)
        await inputs.capture_polymarket(_notice(opened), client, now=clock + timedelta(minutes=1))
        await inputs.capture_polymarket(_notice(opened), client, now=opened + timedelta(minutes=5))
    assert len(requests) == 8  # three current reads, three rollover lags, two next-bar lags
    assert requests.count(int(opened.timestamp())) == 3


@pytest.mark.asyncio
async def test_live_recovery_stamps_each_response_after_fetch(learning_store, monkeypatch):
    from parlay_bot.perpetuals import xmarket_inputs as module

    inputs = XmarketInputs(learning_store)
    opened = NOW.replace(minute=0, second=0, microsecond=0)
    clock = opened + timedelta(minutes=3)
    targets = [opened - timedelta(minutes=n) for n in (0, 15, 60)]
    inputs.record([point("polymarket", t - timedelta(minutes=4)) for t in targets])
    stamps = iter(clock + timedelta(seconds=n) for n in (0, 2, 4, 6, 8))

    class ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(stamps)

    monkeypatch.setattr(module, "datetime", ReceiptClock)

    def serve(request):
        start = int(request.url.params["startTs"])
        at = opened + timedelta(minutes=1) if start == int(opened.timestamp()) else (
            datetime.fromtimestamp(start, UTC) + timedelta(minutes=15))
        return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .61}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        assert await inputs.capture_polymarket(_notice(opened), client) == 4
    bars = sampled_bars(inputs.recent("BTC", at=opened, now=clock + timedelta(seconds=9)),
                        asset="BTC")["polymarket"]
    assert [bars[t]["observed_at"] for t in targets] == [
        (clock + timedelta(seconds=n)).isoformat() for n in (4, 6, 8)]
    assert not sampled_bars(list(bars.values()), asset="BTC", as_of=clock)["polymarket"]


@pytest.mark.asyncio
@pytest.mark.parametrize("horizon", [15, 60])
async def test_recovered_inputs_issue_immutable_shadows_through_real_cycle(
        learning_store, monkeypatch, horizon):
    boundary = NOW.replace(minute=0, second=0, microsecond=0)
    clock = boundary + timedelta(minutes=3)
    market = PerpetualMarketStore(learning_store)
    monitor = ForwardAccuracyMonitor(market)
    _seed_cycle_candles(market, now=clock)
    # Production artifacts pin both Bitget and OKX; exercise the full 18-input
    # vector rather than reducing the contract or mocking the forecast path.
    with learning_store.connect() as conn:
        for venue in ("bitget", "okx"):
            conn.execute("INSERT INTO perp_market_candles SELECT ?,contract,quote,"
                "interval_minutes,opened_at,closed_at,first_observed_at,open_price,"
                "high_price,low_price,close_price,volume_base,trades "
                "FROM perp_market_candles WHERE venue='hyperliquid'", (venue,))
    models = XmarketModelSet(learning_store)
    models.import_document("verified-fit", json.dumps(valid_doc(horizon=horizon, now=clock)).encode())
    inputs = XmarketInputs(learning_store)
    inputs.record([point("kalshi", boundary)] + [
        point("polymarket", boundary - timedelta(minutes=n + 4)) for n in (0, 15, 60)])
    def vector(at):
        candles = {v: persisted_candles(market.recent_candles(venue=v, contract="BTC",
            quote="USDC", interval_minutes=5, count=500, as_of=at))
            for v in ("hyperliquid", "bitget", "okx")}
        return live_vector(candles["hyperliquid"], asset="BTC",
            samples=inputs.recent("BTC", at=boundary, now=at),
            venue_candles=candles, venues=("bitget", "okx"), now=at)

    assert vector(clock) is None

    def serve(request):
        opened = datetime.fromtimestamp(int(request.url.params["startTs"]), UTC)
        at = boundary + timedelta(minutes=1) if opened == boundary else opened + timedelta(minutes=15)
        return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .61}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        assert await inputs.capture_polymarket(_notice(boundary), client, now=clock) == 4
    assert vector(clock - timedelta(seconds=1)) is None  # never retroactive
    assert len(vector(clock)[0]) == 18
    # Collection cannot create a forecast. Only the ordinary forward cycle can.
    monitor.initialize()
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
            "WHERE family='xmarket_v1'").fetchone()["n"] == 0
    monkeypatch.setattr(forward_audit, "ISSUE_XMARKET_V1_CHALLENGERS", True)
    monitor.run_once(now=clock)
    with learning_store.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM rival_model_shadow_forecasts "
            "WHERE family='xmarket_v1'").fetchall()]
        assert len(rows) == 3
        assert {r["venue"] for r in rows} == {"hyperliquid", "bitget", "okx"}
        assert all(r["issued_at"] == clock.isoformat() for r in rows)
        assert all(r["horizon_minutes"] == horizon and r["settled_at"] is None for r in rows)
    monitor.run_once(now=clock + timedelta(seconds=1))
    with learning_store.connect() as conn:
        unchanged = [dict(r) for r in conn.execute("SELECT * FROM rival_model_shadow_forecasts "
            "WHERE family='xmarket_v1'").fetchall()]
        assert unchanged == rows
