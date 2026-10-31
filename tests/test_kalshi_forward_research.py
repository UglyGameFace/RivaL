"""Kalshi learns only from its own first market quotes and official YES/NO receipts."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.kalshi_public import KalshiCryptoBrowser, KalshiMarket
from parlay_bot.perpetuals.kalshi_forward import KalshiForwardResearch
from parlay_bot.storage.hot import SQLiteHotStore


def market(i: int, at: datetime, *, asset="BTC", series="KXBTC15M"):
    return KalshiMarket(
        ticker=f"{series}-{i}", title=f"BTC test contract {i}",
        close_time=at+timedelta(minutes=15),
        yes_ask=.52 if i % 2 else .47,
        no_ask=.51 if i % 2 else .58,
        rules="BTC Crypto price settlement according to Kalshi's rule",
        series_ticker=series,
        quote_source="orderbook", yes_ask_size=10.0, no_ask_size=10.0,
    )


def receipt(mkt, result, settled):
    return {"market": {
        "ticker": mkt.ticker, "status": "settled",
        "result": result, "settlement_ts": settled.isoformat(),
    }}


def test_causal_kalshi_shadow_trains_only_on_past_settlements(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "kalshi.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(100):
        at = start+timedelta(minutes=i * 30)
        m = market(i, at)
        assert ledger.observe(asset="BTC", market=m, at=at)
        assert not ledger.observe(asset="BTC", market=m, at=at)
        assert ledger.settle(
            ticker=m.ticker, payload=receipt(m,"yes" if i % 2 else "no",
                                             at+timedelta(minutes=20)),
            checked_at=at+timedelta(minutes=21),
        )
    status = ledger.summary(asset="BTC")
    assert status["trained_labels"] == 100
    assert status["candidate_forward_resolved"] == 0
    assert status["approved_model"] is False
    # After 100 observed, already-settled and verified labels, a candidate
    # may be frozen for a NEW market but is still research only.
    at = start+timedelta(minutes=100*30)
    m = market(100, at)
    assert ledger.observe(asset="BTC", market=m, at=at)
    with ledger.store.connect() as db:
        frozen = db.execute(
            "SELECT candidate_yes,candidate_weights_sha256,"
            "candidate_training_labels FROM rival_kalshi_forward WHERE ticker=?",
            (m.ticker,),
        ).fetchone()
    assert 0 < frozen["candidate_yes"] < 1
    assert frozen["candidate_weights_sha256"]
    assert frozen["candidate_training_labels"] == 100
    assert ledger.settle(
        ticker=m.ticker, payload=receipt(m, "no", at+timedelta(minutes=20)),
        checked_at=at+timedelta(minutes=21),
    )
    result = ledger.summary(asset="BTC")
    assert result["candidate_forward_resolved"] == 1
    assert result["candidate_brier"] is not None
    assert result["kalshi_quote_baseline_brier"] is not None
    assert result["verified_betting_advantage"] is False
    assert result["approved_model"] is False
    with ledger.store.connect() as db:
        after = db.execute(
            "SELECT candidate_yes,candidate_weights_sha256,"
            "candidate_training_labels FROM rival_kalshi_forward WHERE ticker=?",
            (m.ticker,),
        ).fetchone()
    assert tuple(after) == tuple(frozen)
    assert not ledger.settle(
        ticker=m.ticker, payload=receipt(m, "no", at+timedelta(minutes=20)),
        checked_at=at+timedelta(minutes=21),
    )
    with pytest.raises(ValueError, match="changed"):
        ledger.settle(
            ticker=m.ticker, payload=receipt(m, "yes", at+timedelta(minutes=20)),
            checked_at=at+timedelta(minutes=22),
        )


def test_kalshi_series_models_never_transfer_training_labels(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "scope.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    first = market(1, start)
    ledger.observe(asset="BTC", market=first, at=start)
    ledger.settle(
        ticker=first.ticker,
        payload=receipt(first,"yes",start+timedelta(minutes=20)),
        checked_at=start+timedelta(minutes=21),
    )
    other = market(2,start+timedelta(minutes=30),series="KXBTC1H")
    assert ledger.observe(asset="BTC", market=other,
                          at=start+timedelta(minutes=30))
    with ledger.store.connect() as conn:
        counts = conn.execute(
            "SELECT series_ticker,updates FROM rival_kalshi_shadow_weights "
            "ORDER BY series_ticker"
        ).fetchall()
        proof = conn.execute(
            "SELECT candidate_yes FROM rival_kalshi_forward WHERE ticker=?",
            (other.ticker,),
        ).fetchone()
    assert [(x["series_ticker"],x["updates"]) for x in counts] == [
        ("KXBTC15M",1),("KXBTC1H",0),
    ]
    assert proof["candidate_yes"] is None


def test_incomplete_kalshi_source_or_settlement_fails_closed(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "reject.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    m = market(3,start)
    with pytest.raises(ValueError):
        ledger.observe(asset="BTC", market=KalshiMarket(
            ticker="KXBTC15M-3", title="invalid", close_time=m.close_time,
            yes_ask=.5, no_ask=.5, rules="statement", series_ticker=None,
        ),at=start)
    with pytest.raises(ValueError, match="BOTH real ask"):
        ledger.observe(asset="BTC", market=KalshiMarket(
            ticker=m.ticker,title=m.title,close_time=m.close_time,
            yes_ask=None,no_ask=.5,rules=m.rules,series_ticker=m.series_ticker,
            quote_source="orderbook", yes_ask_size=10.0, no_ask_size=10.0,
        ),at=start)
    assert ledger.observe(asset="BTC", market=m, at=start)
    pending = ledger.pending(now=start+timedelta(minutes=16))
    assert pending == (m.ticker,)
    for invalid in (
        {"market":{"ticker":m.ticker,"status":"open","result":"yes"}},
        {"market":{"ticker":m.ticker,"status":"settled","result":"void"}},
        {"market":{"ticker":m.ticker,"status":"settled","result":"yes"}},
        receipt(m,"yes",start+timedelta(minutes=10)),
    ):
        assert not ledger.settle(
            ticker=m.ticker,payload=invalid,checked_at=start+timedelta(minutes=30),
        )
    assert ledger.summary(asset="BTC")["trained_labels"] == 0


@pytest.mark.asyncio
async def test_kalshi_browser_records_official_market_without_user_messages(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "browse.sqlite"))
    def respond(req):
        if req.url.path.endswith("/series"):
            return httpx.Response(200,json={"series":[{
                "ticker":"KXBTC15M","title":"Bitcoin 15m",
                "category":"Crypto",
            }]})
        if req.url.path.endswith("/orderbook"):
            return httpx.Response(200,json={"orderbook_fp":{
                "yes_dollars":[["0.4900","10.00"]],
                "no_dollars":[["0.4800","10.00"]],
            }})
        if req.url.path.endswith("/markets"):
            return httpx.Response(200,json={"markets":[{
                "ticker":"KXBTC15M-ABC","title":"BTC above X",
                "status":"open",
                "close_time":(datetime.now(UTC)+timedelta(minutes=15)).isoformat(),
                "rules_primary":"Kalshi BTC settlement",
                "yes_ask_dollars":"0.52","no_ask_dollars":"0.51",
            }]})
        raise AssertionError("No settlement query before market close")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        browser = KalshiCryptoBrowser(client=client,forward=ledger)
        found = await browser.browse("BTC")
        assert found.markets[0].series_ticker == "KXBTC15M"
        if browser._study_tasks:
            import asyncio
            await asyncio.gather(*browser._study_tasks)
        assert ledger.summary(asset="BTC")["observed"] == 1
        await browser.aclose()


def test_unsettled_kalshi_market_does_not_starve_later_results(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "fairness.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(5):
        ledger.observe(asset="BTC", market=market(i, start), at=start)
    due = start+timedelta(minutes=16)
    first = ledger.pending(now=due, limit=2)
    assert first == ("KXBTC15M-0", "KXBTC15M-1")
    for ticker in first:
        assert ledger.mark_settlement_check(ticker=ticker, at=due)
    # Must rotate across a restart; do not spin on one never-settled ticker.
    reopened = KalshiForwardResearch(ledger.store)
    second = reopened.pending(now=due, limit=2)
    assert second == ("KXBTC15M-2", "KXBTC15M-3")
    for ticker in second:
        assert reopened.mark_settlement_check(ticker=ticker, at=due)
    assert reopened.pending(now=due, limit=2) == ("KXBTC15M-4",)
    assert reopened.pending(now=due+timedelta(minutes=4), limit=2) == (
        "KXBTC15M-4", "KXBTC15M-0",
    )
    assert not reopened.mark_settlement_check(ticker="UNKNOWN", at=due)


def test_finalized_status_with_real_settlement_receipt_can_train(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "finalized.sqlite"))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    m = market(1, start)
    ledger.observe(asset="BTC", market=m, at=start)
    event = receipt(m, "yes", start+timedelta(minutes=20))
    event["market"]["status"] = "finalized"
    assert ledger.settle(
        ticker=m.ticker, payload=event,
        checked_at=start+timedelta(minutes=21),
    )
    assert ledger.summary(asset="BTC")["trained_labels"] == 1


def test_kalshi_validation_must_be_per_exact_series_not_pooled(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "no-pooling.sqlite"))
    ledger.initialize()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    with ledger.store.connect() as db:
        for i in range(160):
            at = start+timedelta(days=i % 21, minutes=i)
            series = "KXBTC15M" if i % 2 else "KXBTC1H"
            db.execute(
                """INSERT INTO rival_kalshi_forward
                   (ticker,asset,series_ticker,close_at,issued_at,
                    first_quote_sha256,market_rules_sha256,
                    features_json,baseline_yes,yes_ask,no_ask,candidate_yes,
                    result,settlement_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{series}-{i}", "BTC",series,
                 (at+timedelta(minutes=15)).isoformat(),at.isoformat(),
                 "a"*64, "b"*64, "[0,0,0]", .5,.5,.5,.95,
                 "yes", (at+timedelta(minutes=20)).isoformat()),
            )
            db.execute(
                """INSERT INTO rival_kalshi_book_evidence
                   (ticker,observed_at,yes_ask_depth,no_ask_depth)
                   VALUES (?,?,?,?)""",
                (f"{series}-{i}", at.isoformat(), 10., 10.),
            )
    proof = ledger.summary(asset="BTC")
    assert proof["candidate_forward_resolved"] == 160
    assert proof["candidate_brier"] < proof["kalshi_quote_baseline_brier"]
    assert proof["candidate_research_evidence_qualified"] is False
    assert set(proof["by_series"]) == {"KXBTC15M","KXBTC1H"}
    assert all(
        not series["research_gate_passed"]
        for series in proof["by_series"].values()
    )
    assert not proof["approved_model"]


def test_legacy_summary_labels_never_count_as_verified_book_edge(tmp_path):
    ledger = KalshiForwardResearch(SQLiteHotStore(tmp_path / "legacy-scores.sqlite"))
    ledger.initialize()
    at = datetime(2026, 8, 1, tzinfo=UTC)
    with ledger.store.connect() as db:
        db.execute(
            """INSERT INTO rival_kalshi_forward
               (ticker,asset,series_ticker,close_at,issued_at,
                first_quote_sha256,market_rules_sha256,
                features_json,baseline_yes,yes_ask,no_ask,candidate_yes,
                result,settlement_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("KXBTC15M-LEGACY", "BTC", "KXBTC15M",
             (at + timedelta(minutes=15)).isoformat(), at.isoformat(),
             "a" * 64, "b" * 64, "[0,0,0]", .5, .47, .56, .90,
             "yes", (at + timedelta(minutes=20)).isoformat()),
        )
    score = ledger.summary(asset="BTC")
    assert score["observed"] == 1
    assert score["legacy_unverified_settlements"] == 1
    assert score["book_verified_observed"] == 0
    assert score["candidate_forward_resolved"] == 0
    assert score["candidate_research_evidence_qualified"] is False
    assert score["approved_model"] is False
