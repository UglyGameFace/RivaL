"""Real OKX/Bitget wire contracts through existing source-matched ledgers."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.perpetuals import stream_quotes
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.learning_health_receipt import _runtime_diagnostics
from parlay_bot.perpetuals.live_quotes import (
    ASSETS,
    KUCOIN_CONTRACT_SIZE,
    OKX_CONTRACT_SIZE,
    SOURCE_QUOTE_CURRENCIES,
    PublicBestQuoteClient,
    quote_identity,
    source_contract,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.perpetuals.profit_proof import cost_scenarios
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.perpetuals.stream_quotes import (
    KUCOIN_FUTURES_WS,
    StreamingBookHub,
    fetch_kucoin_bullet_token,
    parse_stream_message,
)
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger
from parlay_bot.perpetuals.xmarket_quote_receipt import read_xmarket_quotes


def wire(venue, at, *, asset="BTC", bid=100., ask=100.1, quantity=None):
    """Public v2 frames verified on the real endpoints on November 15.

    KuCoin tickerV2 frames match the live futures ticker shape, verified
    against GET /api/v1/ticker on 2026-11-17 (nanosecond clock, contract
    sizes, best bid/ask fields). The default quantity is one base coin of
    top-of-book depth (KuCoin BTC contracts are 0.001 coins, so 1000 of
    them), which clears the fixed $25 paper notional at the test price.
    """
    if quantity is None:
        quantity = 1000. if venue == "kucoin" else 100.
    symbol = source_contract(venue, asset)
    if venue == "kucoin":
        return {"id": "1", "type": "message",
                "topic": f"/contractMarket/tickerV2:{symbol}",
                "subject": "ticker",
                "data": {"sequence": 1, "symbol": symbol, "side": "buy",
                         "size": 1, "tradeId": "1", "price": str(ask),
                         "bestBidPrice": str(bid), "bestBidSize": str(quantity),
                         "bestAskPrice": str(ask), "bestAskSize": str(quantity),
                         "ts": int(at.timestamp()*1_000_000_000)}}
    stamp = str(int(at.timestamp()*1000))
    if venue == "okx":
        return {"arg": {"channel": "books5", "instId": symbol}, "data": [{
            "instId": symbol, "ts": stamp,
            "bids": [[str(bid), str(quantity), "0", "1"]],
            "asks": [[str(ask), str(quantity), "0", "1"]],
        }]}
    return {"action": "snapshot", "arg": {"instType": "USDT-FUTURES",
            "channel": "ticker", "instId": symbol}, "data": [{
                "instId": symbol, "symbol": symbol, "ts": stamp,
                "bidPr": str(bid), "bidSz": str(quantity),
                "askPr": str(ask), "askSz": str(quantity),
            }], "ts": int(stamp)}


def book(venue, at, **kwargs):
    return parse_stream_message(venue, json.dumps(wire(venue, at, **kwargs)), received_at=at)


@pytest.mark.parametrize("venue", ["okx", "bitget", "kucoin"])
@pytest.mark.parametrize("asset", tuple(ASSETS))
def test_real_wire_identity_and_base_quantity(venue, asset):
    now = datetime.now(UTC)
    quote = book(venue, now, asset=asset, quantity=10)
    assert quote_identity(quote) == (venue, source_contract(venue, asset), "USDT")
    if venue == "okx":
        expected = 10*OKX_CONTRACT_SIZE[asset]
    elif venue == "kucoin":
        expected = 10*KUCOIN_CONTRACT_SIZE[asset]
    else:
        expected = 10
    assert quote.bid_quantity == quote.ask_quantity == pytest.approx(expected)
    assert quote.exchange_at <= quote.received_at
    if venue == "kucoin":
        # KuCoin control frames are JSON objects, never a bare "pong".
        assert parse_stream_message(venue, json.dumps({"type": "pong"}),
                                    received_at=now) is None
        assert parse_stream_message(venue, json.dumps({"type": "welcome"}),
                                    received_at=now) is None
    else:
        assert parse_stream_message(venue, "pong", received_at=now) is None


@pytest.mark.parametrize("venue", ["okx", "bitget", "kucoin"])
def test_stale_future_crossed_wrong_instrument_and_empty_wire_never_enter_hub(venue):
    now = datetime.now(UTC)
    hub = StreamingBookHub(assets=("BTC",))
    for at, kwargs in ((now-timedelta(seconds=11), {}), (now+timedelta(seconds=1), {}),
                       (now, {"bid":101.,"ask":100.}), (now, {"quantity":0})):
        assert not hub.accept(venue, json.dumps(wire(venue, at, **kwargs)), received_at=now)
    wrong = wire(venue, now)
    if venue == "kucoin":
        # Topic still XBTUSDTM but the row claims ETHUSDTM: mismatch.
        wrong["data"]["symbol"] = "ETHUSDTM"
    else:
        wrong["data"][0]["instId"] = "ETHUSDT"
    assert not hub.accept(venue, json.dumps(wrong), received_at=now)
    empty = wire(venue, now)
    empty["data"] = []
    assert not hub.accept(venue, json.dumps(empty), received_at=now)
    if venue == "bitget":
        spot = wire(venue, now)
        spot["arg"]["instType"] = "SPOT"
        assert not hub.accept(venue, json.dumps(spot), received_at=now)
    assert hub.snapshot(venue, "BTC", now=now) is None
    raw = json.dumps(wire(venue, now))
    assert hub.accept(venue, raw, received_at=now)
    assert not hub.accept(venue, raw, received_at=now)
    assert hub.snapshot(venue, "BTC", now=now+timedelta(seconds=11)) is None


def seed(store, venue, issued):
    contract = source_contract(venue, "BTC")
    currency = SOURCE_QUOTE_CURRENCIES[venue]
    with store.connect() as conn:
        conn.execute("""INSERT INTO perp_forward_audit
            (venue,contract,quote,asset,horizon_minutes,model_version,
             reference_opened_at,reference_close,issued_at,target_opened_at,
             probability_up,baseline_up,training_evaluated)
            VALUES (?,?,?,'BTC',5,?,?,100.,?,?,.7,.5,200)""",
            (venue,contract,currency,MODEL,(issued-timedelta(minutes=5)).isoformat(),
             issued.isoformat(),issued.isoformat()))
        conn.execute("""INSERT INTO rival_model_shadow_forecasts
            (venue,contract,quote,asset,horizon_minutes,family,reference_opened_at,
             issued_at,target_opened_at,probability_up,incumbent_up,baseline_up,
             training_count,training_sha256)
            VALUES (?,?,?,'BTC',5,'xmarket_v1',?,?,?,.7,.6,.5,200,?)""",
            (venue,contract,currency,(issued-timedelta(minutes=5)).isoformat(),
             issued.isoformat(),issued.isoformat(),"a"*64))


@pytest.mark.parametrize("venue", ["okx", "bitget", "kucoin"])
def test_stream_and_xmarket_quote_lifecycle_is_source_exact_and_restart_safe(learning_store, venue):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    ShadowModelCompetition(learning_store).initialize()
    issued = datetime.now(UTC)-timedelta(minutes=6)
    entry = issued+timedelta(seconds=20)
    exit_at = issued+timedelta(minutes=5, seconds=20)
    other = "okx" if venue == "bitget" else "bitget"
    seed(learning_store, venue, issued)
    ledger = StreamShadowLedger(learning_store)
    assert ledger.observe(book(other, entry), now=entry)["opened"] == 0
    assert ledger.observe(book(venue, entry), now=entry)["opened"] == 1
    assert ledger.observe(book(venue, entry), now=entry)["opened"] == 0
    assert ledger.observe(book(other, exit_at), now=exit_at)["closed"] == 0
    # A genuine adverse later book produces a loss, never a candle fill.
    assert ledger.observe(book(venue, exit_at, bid=99., ask=99.1), now=exit_at)["closed"] == 1
    report = StreamShadowLedger(learning_store).report(
        asset="BTC", horizon_minutes=5, now=exit_at+timedelta(minutes=2))
    assert report["eligible_windows"] == report["entries"] == report["closed"] == 1
    assert report["costs"]["conservative"]["mean_net_bps"] < 0
    premium_cost = dict(cost_scenarios(venue))["conservative"]
    assert report["costs"]["conservative"]["assumed_extra_cost_bps"] == premium_cost
    assert report["costs"]["conservative"]["mean_net_bps"] == pytest.approx(
        (99./100.1-1)*10000-premium_cost)
    assert not report["qualified_shadow"] and not report["real_fills_verified"]
    progress = {"scope_progress":[{"asset":"BTC","horizon_minutes":5,
                                    "stored_gate_passing_artifacts":1}]}
    receipt = read_xmarket_quotes(learning_store, exit_at+timedelta(seconds=1), progress)
    current = next(r for r in receipt["scopes"] if r["venue"] == venue)
    assert current["contract"] == source_contract(venue, "BTC")
    assert current["entry_rows"] == current["closed_exit_rows"] == 1
    assert all(r["entry_rows"] == 0 for r in receipt["scopes"] if r["venue"] != venue)


def test_okx_contract_count_cannot_be_mistaken_for_coin_liquidity(learning_store):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    ShadowModelCompetition(learning_store).initialize()
    now = datetime.now(UTC)
    seed(learning_store, "okx", now-timedelta(seconds=20))
    # 10 contracts are only 0.1 BTC; insufficient for the fixed 25-USD
    # paper notional at this test price. The former raw size of 10 would pass.
    result = StreamShadowLedger(learning_store).observe(book("okx", now, quantity=10), now=now)
    assert result["opened"] == 0
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rival_family_quote_shadow").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_venue_signal_reuses_stream_without_rest():
    now = datetime.now(UTC)
    hub = StreamingBookHub(assets=("BTC",))
    for venue in ("okx", "bitget", "kucoin"):
        assert hub.accept(venue, json.dumps(wire(venue, now)), received_at=now)
    def forbidden(_request):
        raise AssertionError("fresh source book must not trigger REST")
    clients = {v:httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
               for v in ("okx","bitget","kucoin")}
    quotes = PublicBestQuoteClient(venue_clients=clients)
    quotes.stream_hub = hub
    try:
        for venue in clients:
            assert (await quotes.get_venue("BTC", venue)).venue == venue
    finally:
        await quotes.aclose()
        for client in clients.values():
            await client.aclose()


def test_running_stream_health_is_persisted_and_read_without_collecting_or_inference(learning_store):
    now = datetime.now(UTC)
    hub = StreamingBookHub(assets=("BTC",))
    for venue in ("okx","bitget","kucoin"):
        assert hub.accept(venue, json.dumps(wire(venue, now)), received_at=now)
    monitor = RuntimeHealthMonitor(learning_store)
    monitor.publish({}, now=now, quote_streams=hub.health(now=now))
    receipt = _runtime_diagnostics(learning_store, now)
    evidence = receipt["components"]["quote_streams"]["evidence"]
    assert evidence["does_not_prove_prediction_edge"]
    for venue in ("okx","bitget","kucoin"):
        assert evidence["sources"][venue]["updates"] == 1
        assert evidence["sources"][venue]["fresh_assets"] == ["BTC"]
    assert evidence["sources"]["binance"]["updates"] == 0


@pytest.mark.asyncio
async def test_one_okx_socket_supplies_quotes_and_existing_liquidity(monkeypatch):
    hub = StreamingBookHub(assets=("BTC",))
    hub.recorder = SimpleNamespace(ingest=lambda *_:None)
    names = []
    async def stalled(venue, *_args):
        names.append(venue)
        await asyncio.Future()
    monkeypatch.setattr(hub, "_run", stalled)
    monkeypatch.setattr(hub, "_run_liquidity", stalled)
    hub.start()
    await asyncio.sleep(0)
    try:
        assert names.count("okx") == 1
        assert names.count("bitget") == 1
        assert names.count("kucoin") == 1
        assert "okx-orderflow" not in names
        assert len(names) == 11  # eight native books plus three existing flow feeds
    finally:
        await asyncio.wait_for(hub.aclose(), timeout=1)


@pytest.mark.parametrize("venue", ["okx", "bitget", "kucoin"])
@pytest.mark.parametrize("incumbent", [.6, .5], ids=["position", "cash"])
def test_fusion_quote_uses_only_its_chosen_reference_source(learning_store, venue, incumbent):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    research = MultiExchangeResearch(learning_store)
    research.initialize()
    issued = datetime.now(UTC)-timedelta(minutes=6)
    target = issued
    with learning_store.connect() as conn:
        conn.execute("""INSERT INTO perp_multi_forward
            (asset,horizon_minutes,target_opened_at,issued_at,model_version,
             probability_up,reference_probability_up,reference_venue,reference_contract,
             reference_quote,reference_opened_at,contributor_count,contributors_json)
            VALUES ('BTC',5,?,?,?,.7,?,?,?,'USDT',?,3,'[]')""",
            (target.isoformat(),issued.isoformat(),"ten-venue-fusion-shadow-v1",incumbent,venue,
             source_contract(venue,"BTC"),(issued-timedelta(minutes=5)).isoformat()))
    entry = issued+timedelta(seconds=20)
    other = "okx" if venue == "bitget" else "bitget"
    assert research.observe(book(other,entry),now=entry) == {"opened":0,"closed":0}
    assert research.observe(book(venue,entry),now=entry)["opened"] == 1
    exit_at = issued+timedelta(minutes=5,seconds=20)
    assert research.observe(book(other,exit_at),now=exit_at)["closed"] == 0
    assert research.observe(book(venue,exit_at),now=exit_at)["closed"] == 1
    with learning_store.connect() as conn:
        conn.execute("""UPDATE perp_multi_forward SET settled_at=?,outcome_up=1,
                        challenger_brier=.09,incumbent_brier=?""",
                     (exit_at.isoformat(),(incumbent-1)**2))
    report = research.report(asset="BTC",horizon_minutes=5,
                             now=exit_at+timedelta(minutes=2))
    for label,cost in cost_scenarios(venue):
        expected = (100./100.1-1)*10000-cost
        assert report["after_cost"][label]["challenger_mean_net_bps"] == pytest.approx(expected)
        assert report["after_cost"][label]["incumbent_mean_net_bps"] == pytest.approx(
            expected if incumbent == .6 else 0)


def test_source_fees_add_to_existing_stress_without_relaxing_other_venues():
    assert dict(cost_scenarios("binance")) == {"conservative":14.,"severe":80.}
    assert dict(cost_scenarios("hyperliquid")) == {"conservative":14.,"severe":80.}
    assert dict(cost_scenarios("okx")) == {"conservative":16.,"severe":82.}
    assert dict(cost_scenarios("bitget")) == {"conservative":18.,"severe":84.}
    # KuCoin's 6 bps taker is below the 8 bps floor: no premium.
    assert dict(cost_scenarios("kucoin")) == {"conservative":14.,"severe":80.}


@pytest.mark.parametrize("venue", ["okx", "bitget", "kucoin"])
def test_family_qualification_applies_same_venue_fee_floor(learning_store, venue):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    ShadowModelCompetition(learning_store).initialize()
    issued = (datetime.now(UTC)-timedelta(minutes=6)).replace(second=0,microsecond=0)
    issued -= timedelta(minutes=issued.minute % 5)
    seed(learning_store,venue,issued)
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_model_shadow_forecasts SET family='lab_fee_source'")
    ledger = StreamShadowLedger(learning_store)
    entry = issued+timedelta(seconds=20)
    exit_at = issued+timedelta(minutes=5,seconds=20)
    ledger.observe(book(venue,entry),now=entry)
    ledger.observe(book(venue,exit_at),now=exit_at)
    with learning_store.connect() as conn:
        conn.execute("""UPDATE rival_model_shadow_forecasts SET settled_at=?,outcome_up=1,
                       model_brier=.09,incumbent_brier=.16,baseline_brier=.25""",
                     (exit_at.isoformat(),))
    report = FamilyPromotion(learning_store).evaluate(
        asset="BTC",venue=venue,contract=source_contract(venue,"BTC"),quote="USDT",
        horizon=5,now=exit_at+timedelta(minutes=2))
    family = report["families"]["lab_fee_source"]
    assert family["closed_quotes"] == 1
    assert not family["qualified"]
    for label,cost in cost_scenarios(venue):
        assert family["cost_proof"][label]["challenger_net_bps"] == pytest.approx(
            (100./100.1-1)*10000-cost)


def _kucoin_bullet_payload(token="tok-1", endpoint=KUCOIN_FUTURES_WS):
    return {"code": "200000", "data": {"token": token,
            "instanceServers": [{"endpoint": endpoint, "protocol": "websocket",
                                 "encrypt": True, "pingInterval": 50000,
                                 "pingTimeout": 10000}]}}


@pytest.mark.asyncio
async def test_kucoin_bullet_token_returns_token_and_endpoint():
    def handler(request):
        assert request.url.host == "api-futures.kucoin.com"
        assert request.url.path == "/api/v1/bullet-public"
        return httpx.Response(200, json=_kucoin_bullet_payload())
    token, endpoint = await fetch_kucoin_bullet_token(
        transport=httpx.MockTransport(handler))
    assert token == "tok-1"
    assert endpoint == KUCOIN_FUTURES_WS


@pytest.mark.asyncio
async def test_kucoin_bullet_token_rejects_bad_payload():
    def handler(request):
        return httpx.Response(200, json={"code": "400302", "msg": "restricted"})
    with pytest.raises(ValueError, match="bullet-public"):
        await fetch_kucoin_bullet_token(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_kucoin_stream_refreshes_bullet_token_on_reconnect(monkeypatch):
    hub = StreamingBookHub(assets=("BTC",))
    tokens = iter(["token-one", "token-two"])

    async def fake_bullet(*, transport=None):
        return next(tokens), KUCOIN_FUTURES_WS
    monkeypatch.setattr(stream_quotes, "fetch_kucoin_bullet_token", fake_bullet)
    seen_urls, sent = [], []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, message):
            sent.append(json.loads(message))

        async def recv(self):
            await asyncio.sleep(3600)

    def connect(url, **kwargs):
        seen_urls.append(url)
        if len(seen_urls) == 1:
            raise OSError("handshake refused")
        return Socket()
    monkeypatch.setattr(stream_quotes.websockets, "connect", connect)
    task = asyncio.create_task(hub._run("kucoin"))
    try:
        deadline = time.monotonic() + 15
        while (len(seen_urls) < 2 or not sent) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert len(seen_urls) == 2
        assert "token=token-one" in seen_urls[0]
        assert "token=token-two" in seen_urls[1]
        assert all("connectId=" in url for url in seen_urls)
        assert sent == [{"type": "subscribe",
                         "topic": "/contractMarket/tickerV2:XBTUSDTM",
                         "privateChannel": False, "response": True}]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_kucoin_push_only_silence_triggers_existing_reconnect(monkeypatch):
    # tickerV2 pushes only on a book change: a quiet market delivers
    # control frames and no books, so the existing 120s silence restart
    # must fire with no new silence rule invented.
    hub = StreamingBookHub(assets=("BTC",))
    monkeypatch.setattr(stream_quotes, "BOOK_SILENCE_SECONDS", 1)
    monkeypatch.setattr(stream_quotes, "RECEIVE_CHECK_SECONDS", 1)

    async def fake_bullet(*, transport=None):
        return "tok", KUCOIN_FUTURES_WS
    monkeypatch.setattr(stream_quotes, "fetch_kucoin_bullet_token", fake_bullet)
    pings = 0

    class PingSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, message):
            pass

        async def recv(self):
            nonlocal pings
            pings += 1
            return json.dumps({"type": "ping"})
    monkeypatch.setattr(stream_quotes.websockets, "connect",
                        lambda url, **kwargs: PingSocket())
    task = asyncio.create_task(hub._run("kucoin"))
    try:
        deadline = time.monotonic() + 15
        while hub._silence_restarts["kucoin"] == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert hub._silence_restarts["kucoin"] >= 1
        assert hub._reconnects["kucoin"] >= 1
        assert pings >= 1  # control frames arrived but no book was accepted
        assert hub._accepted["kucoin"] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
