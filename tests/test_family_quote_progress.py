"""A bounded quote observer must progress past rows it cannot act on."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.family_quotes import MAX_ROWS, FamilyQuoteShadow
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.stream_shadow import StreamShadowLedger


def _book(at, *, quantity=10):
    return PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=102., ask=102.1,
        bid_quantity=quantity, ask_quantity=quantity,
        exchange_at=at, received_at=at,
    )


def _forecast(conn, family, *, issued, target, probability=.65):
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts
           (venue,contract,quote,asset,horizon_minutes,family,
            reference_opened_at,issued_at,target_opened_at,probability_up,
            incumbent_up,baseline_up,training_count,training_sha256)
           VALUES ('hyperliquid','BTC','USDC','BTC',15,?,?,?,?,?,.5,.5,200,?)""",
        (family, (target-timedelta(minutes=15)).isoformat(), issued.isoformat(),
         target.isoformat(), probability, "a"*64),
    )


def _observer(store):
    ShadowModelCompetition(store).initialize()
    observer = FamilyQuoteShadow(store)
    observer.initialize()
    return observer


@pytest.mark.parametrize("blocked", ["cash", "unsupported", "target_closed"])
def test_ineligible_early_families_cannot_hide_a_quoteable_xmarket(learning_store, blocked):
    now = datetime.now(UTC).replace(microsecond=0)
    observer = _observer(learning_store)
    target = now+timedelta(minutes=10)
    with learning_store.connect() as conn:
        for i in range(MAX_ROWS):
            _forecast(
                conn, ("unknown_" if blocked == "unsupported" else "lab_")+str(i),
                issued=now-timedelta(seconds=40),
                target=now-timedelta(minutes=6) if blocked == "target_closed" else target,
                probability=.5 if blocked == "cash" else .65,
            )
        _forecast(conn, "xmarket_v1", issued=now-timedelta(seconds=30), target=target)
    assert observer.observe(_book(now), now=now)["opened"] == 1
    with learning_store.connect() as conn:
        rows = conn.execute("SELECT family,entry_at FROM rival_family_quote_shadow").fetchall()
    assert [row["family"] for row in rows] == ["xmarket_v1"]
    assert rows[0]["entry_at"] == now.isoformat()


def test_previously_observed_batch_cannot_starve_new_forecasts(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    observer = _observer(learning_store)
    target = now+timedelta(minutes=10)
    with learning_store.connect() as conn:
        for i in range(MAX_ROWS):
            _forecast(conn, "lab_"+str(i), issued=now-timedelta(seconds=30), target=target)
    assert observer.observe(_book(now), now=now)["opened"] == MAX_ROWS
    with learning_store.connect() as conn:
        _forecast(conn, "xmarket_v1", issued=now+timedelta(seconds=1), target=target)
    later = now+timedelta(seconds=2)
    assert observer.observe(_book(later), now=later)["opened"] == 1
    assert observer.observe(_book(later), now=later)["opened"] == 0
    with learning_store.connect() as conn:
        row = conn.execute(
            "SELECT entry_at FROM rival_family_quote_shadow WHERE family='lab_0'"
        ).fetchone()
    assert row["entry_at"] == now.isoformat()


def test_live_stream_parent_reaches_xmarket_after_cash_batch(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    _observer(learning_store)
    ForwardAccuracyMonitor(PerpetualMarketStore(learning_store)).initialize()
    target = now+timedelta(minutes=10)
    with learning_store.connect() as conn:
        for i in range(MAX_ROWS):
            _forecast(conn, "lab_"+str(i), issued=now-timedelta(seconds=40),
                      target=target, probability=.5)
        _forecast(conn, "xmarket_v1", issued=now-timedelta(seconds=30), target=target)
    parent = StreamShadowLedger(learning_store)
    parent.observe(_book(now), now=now)
    parent.observe(_book(now), now=now)
    with learning_store.connect() as conn:
        rows = conn.execute(
            "SELECT family,training_sha256,entry_at FROM rival_family_quote_shadow"
        ).fetchall()
    assert [row["family"] for row in rows] == ["xmarket_v1"]
    assert rows[0]["training_sha256"] == "a"*64
    assert rows[0]["entry_at"] == now.isoformat()


def test_eligible_backlog_drains_in_bounded_batches_inside_original_window(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    observer = _observer(learning_store)
    target = now+timedelta(minutes=10)
    with learning_store.connect() as conn:
        for i in range(MAX_ROWS*2+1):
            family = "xmarket_v1" if i == MAX_ROWS*2 else "lab_"+str(i)
            _forecast(conn, family, issued=now-timedelta(seconds=10), target=target)
    opened = []
    for seconds in (0,30,60):
        at = now+timedelta(seconds=seconds)
        opened.append(observer.observe(_book(at), now=at)["opened"])
    assert opened == [MAX_ROWS, MAX_ROWS, 1]
    expired = now+timedelta(seconds=91)
    with learning_store.connect() as conn:
        _forecast(conn, "lab_too_late", issued=now, target=target)
    assert observer.observe(_book(expired), now=expired)["opened"] == 0


def test_insufficient_exit_depth_does_not_block_smaller_valid_position(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    observer = _observer(learning_store)
    with learning_store.connect() as conn:
        for i in range(MAX_ROWS+1):
            family = "lab_"+str(i) if i < MAX_ROWS else "xmarket_v1"
            close = now-timedelta(seconds=20 if i < MAX_ROWS else 10)
            quantity = .251 if i < MAX_ROWS else .249
            entry_price = 25./quantity
            conn.execute(
                """INSERT INTO rival_family_quote_shadow
                   (venue,contract,quote,asset,horizon_minutes,family,
                    reference_opened_at,issued_at,target_close_at,training_sha256,
                    challenger_up,incumbent_up,challenger_side,incumbent_side,
                    entry_at,entry_bid,entry_ask,quantity,state)
                   VALUES ('hyperliquid','BTC','USDC','BTC',15,?,?,?,?,?,
                           .65,.5,'LONG','CASH',?,?,?,?,'OPEN')""",
                (family, (close-timedelta(minutes=20)).isoformat(),
                 (close-timedelta(minutes=14)).isoformat(), close.isoformat(), "a"*64,
                 (close-timedelta(minutes=13)).isoformat(),
                 entry_price-.1, entry_price, quantity),
            )
    result = observer.observe(_book(now, quantity=.25), now=now)
    assert result["closed"] == 1
    with learning_store.connect() as conn:
        rows = conn.execute(
            "SELECT family,state,entry_at,exit_at FROM rival_family_quote_shadow"
        ).fetchall()
    assert [row["family"] for row in rows if row["state"] == "EXIT_QUOTED"] == ["xmarket_v1"]
    assert sum(row["state"] == "OPEN" for row in rows) == MAX_ROWS
    assert observer.expire(now=now+timedelta(minutes=2)) == MAX_ROWS
