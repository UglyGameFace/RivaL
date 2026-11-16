"""Gate, Deribit linear and Lighter public BBO through real proof consumers."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import (
    GATE_CONTRACT_SIZE,
    LIGHTER_PERP_MARKET_IDS,
    SOURCE_QUOTE_CURRENCIES,
    PublicBestQuoteClient,
    QuoteUnavailable,
    quote_identity,
    source_contract,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.perpetuals.profit_proof import cost_scenarios
from parlay_bot.perpetuals.stream_quotes import StreamingBookHub, parse_stream_message
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger
from parlay_bot.perpetuals.xmarket_quote_receipt import read_xmarket_quotes
from tests.test_additional_quote_sources import book as other_book
from tests.test_additional_quote_sources import seed

VENUES = ("gate", "deribit", "lighter")


def wire(venue, at, *, asset="BTC", bid=100., ask=100.1, quantity=10000.):
    """Full snapshots matching public endpoint frames captured 2026-11-16."""
    symbol = source_contract(venue, asset)
    ms = int(at.timestamp()*1000)
    if venue == "gate":
        return {"channel":"futures.book_ticker", "event":"update", "result":{
            "s":symbol, "t":ms, "u":123,
            "b":str(bid), "B":quantity, "a":str(ask), "A":quantity}}
    if venue == "deribit":
        return {"jsonrpc":"2.0", "method":"subscription", "params":{
            "channel":f"book.{symbol}.none.1.100ms", "data":{
                "instrument_name":symbol, "timestamp":ms, "change_id":123,
                "bids":[[bid,quantity]], "asks":[[ask,quantity]]}}}
    micros = int(at.timestamp()*1_000_000)
    return {"type":"update/ticker", "channel":f"ticker:{LIGHTER_PERP_MARKET_IDS[asset]}",
            "timestamp":ms, "last_updated_at":micros, "nonce":123, "ticker":{
                "s":asset, "last_updated_at":micros,
                "b":{"price":str(bid), "size":str(quantity)},
                "a":{"price":str(ask), "size":str(quantity)}}}


def book(venue, at, **kwargs):
    return parse_stream_message(venue, json.dumps(wire(venue, at, **kwargs)), received_at=at)


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("asset", ("BTC", "ETH", "SOL", "XRP"))
def test_native_full_snapshot_uses_exact_contract_clock_currency_and_coin_size(venue, asset):
    now = datetime.now(UTC)
    quote = book(venue, now, asset=asset, quantity=10.)
    assert quote_identity(quote) == (venue, source_contract(venue, asset),
                                     SOURCE_QUOTE_CURRENCIES[venue])
    expected = 10*GATE_CONTRACT_SIZE[asset] if venue == "gate" else 10.
    assert quote.bid_quantity == quote.ask_quantity == pytest.approx(expected)
    assert timedelta(0) <= now-quote.exchange_at < timedelta(milliseconds=1)


@pytest.mark.parametrize("venue", VENUES)
def test_unusable_native_books_never_replace_valid_quotes(venue):
    now = datetime.now(UTC)
    hub = StreamingBookHub(assets=("BTC",))
    for at, kwargs in ((now-timedelta(seconds=11), {}), (now+timedelta(seconds=1), {}),
                       (now, {"bid":101.}), (now, {"quantity":0.}),
                       (now, {"quantity":float("nan")})):
        assert not hub.accept(venue, json.dumps(wire(venue, at, **kwargs)), received_at=now)
    wrong = wire(venue, now)
    if venue == "gate":
        wrong["result"]["s"] = "BTC_USD"  # inverse contract
    elif venue == "deribit":
        wrong["params"]["channel"] = "book.BTC-PERPETUAL.none.1.100ms"
    else:
        wrong["channel"] = "ticker:2049"  # not the verified BTC perp
    assert not hub.accept(venue, json.dumps(wrong), received_at=now)
    valid = json.dumps(wire(venue, now))
    assert hub.accept(venue, valid, received_at=now)
    assert not hub.accept(venue, valid, received_at=now)
    assert hub.snapshot(venue, "BTC", now=now+timedelta(seconds=11)) is None


@pytest.mark.parametrize("venue", VENUES)
def test_native_entries_exits_restart_receipt_and_governance_are_source_exact(learning_store, venue):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    ShadowModelCompetition(learning_store).initialize()
    issued = (datetime.now(UTC)-timedelta(minutes=6)).replace(second=0,microsecond=0)
    issued -= timedelta(minutes=issued.minute % 5)
    seed(learning_store, venue, issued)
    ledger = StreamShadowLedger(learning_store)
    entry, exit_at = issued+timedelta(seconds=20), issued+timedelta(minutes=5,seconds=20)
    assert ledger.observe(other_book("okx", entry), now=entry)["opened"] == 0
    assert ledger.observe(book(venue, entry), now=entry)["opened"] == 1
    restarted = StreamShadowLedger(learning_store)
    assert restarted.observe(book(venue, entry), now=entry)["opened"] == 0
    assert restarted.observe(other_book("okx", exit_at), now=exit_at)["closed"] == 0
    assert restarted.observe(book(venue, exit_at, bid=99., ask=99.1), now=exit_at)["closed"] == 1
    progress = {"scope_progress":[{"asset":"BTC", "horizon_minutes":5,
                                    "stored_gate_passing_artifacts":1}]}
    receipt = read_xmarket_quotes(learning_store, exit_at+timedelta(minutes=2), progress)
    own = next(r for r in receipt["scopes"] if r["venue"] == venue)
    assert own["contract"] == source_contract(venue, "BTC")
    assert own["entry_rows"] == own["closed_exit_rows"] == 1
    assert all(r["entry_rows"] == 0 for r in receipt["scopes"] if r["venue"] != venue)
    report = restarted.report(asset="BTC", horizon_minutes=5, now=exit_at+timedelta(minutes=2))
    assert report["closed"] == 1
    for label,cost in cost_scenarios(venue):
        assert report["costs"][label]["mean_net_bps"] == pytest.approx((99./100.1-1)*10000-cost)
    with learning_store.connect() as conn:
        conn.execute("""UPDATE rival_model_shadow_forecasts SET family='lab_native_source',
                       settled_at=?,outcome_up=1,model_brier=.09,
                       incumbent_brier=.16,baseline_brier=.25""", (exit_at.isoformat(),))
        conn.execute("UPDATE rival_family_quote_shadow SET family='lab_native_source'")
    evaluation = FamilyPromotion(learning_store).evaluate(
        asset="BTC", venue=venue, contract=source_contract(venue, "BTC"),
        quote=SOURCE_QUOTE_CURRENCIES[venue], horizon=5, now=exit_at+timedelta(minutes=2))
    family = evaluation["families"]["lab_native_source"]
    assert family["closed_quotes"] == 1 and not family["qualified"]
    for label,cost in cost_scenarios(venue):
        assert family["cost_proof"][label]["challenger_net_bps"] == pytest.approx(
            (99./100.1-1)*10000-cost)


def test_native_contract_count_cannot_overstate_gate_coin_liquidity(learning_store):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    ShadowModelCompetition(learning_store).initialize()
    now = datetime.now(UTC)
    seed(learning_store, "gate", now-timedelta(seconds=20))
    # Ten Gate contracts are .001 BTC, not ten BTC. At this test price the
    # real quantity cannot support the fixed $25 paper notional.
    assert StreamShadowLedger(learning_store).observe(book("gate", now, quantity=10), now=now)["opened"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", VENUES)
async def test_native_signal_uses_stream_and_fails_closed_without_one(venue):
    client = PublicBestQuoteClient()
    client.stream_hub = StreamingBookHub(assets=("BTC",))
    try:
        with pytest.raises(QuoteUnavailable, match="native stream"):
            await client.get_venue("BTC", venue)
        now = datetime.now(UTC)
        assert client.stream_hub.accept(venue, json.dumps(wire(venue, now)), received_at=now)
        assert (await client.get_venue("BTC", venue)).venue == venue
    finally:
        await client.aclose()


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("incumbent", [.6,.5], ids=["position","cash"])
def test_native_fusion_quote_costs_follow_only_reference_source(learning_store, venue, incumbent):
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    research = MultiExchangeResearch(learning_store)
    research.initialize()
    issued = datetime.now(UTC)-timedelta(minutes=6)
    with learning_store.connect() as conn:
        conn.execute("""INSERT INTO perp_multi_forward
            (asset,horizon_minutes,target_opened_at,issued_at,model_version,
             probability_up,reference_probability_up,reference_venue,reference_contract,
             reference_quote,reference_opened_at,contributor_count,contributors_json)
            VALUES ('BTC',5,?,?,?,.7,?,?,?,?,?,3,'[]')""",
            (issued.isoformat(),issued.isoformat(),"ten-venue-fusion-shadow-v1",incumbent,venue,
             source_contract(venue,"BTC"),SOURCE_QUOTE_CURRENCIES[venue],
             (issued-timedelta(minutes=5)).isoformat()))
    entry, exit_at = issued+timedelta(seconds=20), issued+timedelta(minutes=5,seconds=20)
    assert research.observe(other_book("okx",entry),now=entry)["opened"] == 0
    assert research.observe(book(venue,entry),now=entry)["opened"] == 1
    assert research.observe(other_book("okx",exit_at),now=exit_at)["closed"] == 0
    assert research.observe(book(venue,exit_at),now=exit_at)["closed"] == 1
    with learning_store.connect() as conn:
        conn.execute("""UPDATE perp_multi_forward SET settled_at=?,outcome_up=1,
                        challenger_brier=.09,incumbent_brier=?""",
                     (exit_at.isoformat(),(incumbent-1)**2))
    report = research.report(asset="BTC",horizon_minutes=5,now=exit_at+timedelta(minutes=2))
    for label,cost in cost_scenarios(venue):
        expected = (100./100.1-1)*10000-cost
        assert report["after_cost"][label]["challenger_mean_net_bps"] == pytest.approx(expected)
        assert report["after_cost"][label]["incumbent_mean_net_bps"] == pytest.approx(
            expected if incumbent == .6 else 0)


def test_native_fee_floors_preserve_all_existing_stresses():
    assert dict(cost_scenarios("gate")) == {"conservative":21.,"severe":87.}
    for venue in ("deribit","lighter"):
        assert dict(cost_scenarios(venue)) == {"conservative":14.,"severe":80.}


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", VENUES)
async def test_native_worker_subscriptions_and_cancellation_use_one_bounded_public_socket(monkeypatch, venue):
    from parlay_bot.perpetuals import stream_quotes

    sent, connects = [], []
    processed = asyncio.Event()

    class Socket:
        delivered = False

        async def send(self, message):
            sent.append(json.loads(message))

        async def recv(self):
            if not self.delivered:
                self.delivered = True
                return json.dumps(wire(venue, datetime.now(UTC)))
            processed.set()  # first book has now passed the real accept path
            await asyncio.Future()

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            return False

    def connect(url, **kwargs):
        connects.append((url,kwargs))
        return Connection()

    monkeypatch.setattr(stream_quotes.websockets, "connect", connect)
    hub = StreamingBookHub(assets=("BTC","ETH"))
    task = asyncio.create_task(hub._run(venue))
    try:
        await asyncio.wait_for(processed.wait(), timeout=1)
        assert hub.snapshot(venue, "BTC") is not None
        assert len(connects) == 1
        assert connects[0][1]["max_queue"] == 32
        assert connects[0][1]["ping_interval"] == 20
        if venue == "gate":
            assert connects[0][0] == "wss://fx-ws.gateio.ws/v4/ws/usdt"
            assert sent[0]["channel"] == "futures.book_ticker"
            assert sent[0]["payload"] == ["BTC_USDT","ETH_USDT"]
        elif venue == "deribit":
            assert connects[0][0] == "wss://www.deribit.com/ws/api/v2"
            assert sent == [{"jsonrpc":"2.0", "id":1,"method":"public/subscribe",
                "params":{"channels":["book.BTC_USDC-PERPETUAL.none.1.100ms",
                                      "book.ETH_USDC-PERPETUAL.none.1.100ms"]}}]
        else:
            assert connects[0][0] == "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
            assert sent == [{"type":"subscribe","channel":"ticker/1"},
                            {"type":"subscribe","channel":"ticker/0"}]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    assert not hub.health()[venue]["connected"]
