"""Five-minute prospective paths: issuance, admission, completeness, capacity.

A 5-minute path needs exactly one complete exact-source candle. The
settlement worker admits 5-minute paths only from capacity the 15/60-minute
queues did not use (leftover priority, MAX_SETTLE total per cycle), and
issuance admits 5-minute horizons through ``HORIZONS`` in
``ProspectivePathLedger.issue`` / ``empirical_paths`` and through
``horizon in (5, 15, 60)`` in ``forward_audit``. The settlement tests
below seed 5-minute rows directly; the issuance tests issue them through
``ProspectivePathLedger.issue`` instead.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.path_forward import (
    TARGET_VERSION,
    VERSION,
    ProspectivePathLedger,
    empirical_paths,
)


def _candles(*, count=400, start=None):
    stamp = start or datetime(2026, 10, 28, tzinfo=UTC)
    values = []
    for index in range(count):
        opened = stamp + timedelta(minutes=5 * index)
        price = 100 + 0.18 * math.sin(index / 3) + 0.04 * math.sin(index / 9)
        values.append(MarketCandle(
            venue="hyperliquid", contract="BTC", quote="USDC",
            interval_minutes=5, opened_at=opened,
            closed_at=opened + timedelta(minutes=5, milliseconds=-1),
            observed_at=opened + timedelta(minutes=5, seconds=1),
            open_price=price, high_price=price + .07, low_price=price - .07,
            close_price=price, volume_base=10.0, trades=8,
        ))
    return values


def _row(c):
    return (c.venue, c.contract, c.quote, 5, c.opened_at.isoformat(),
            c.closed_at.isoformat(), c.observed_at.isoformat(),
            c.open_price, c.high_price, c.low_price, c.close_price,
            c.volume_base, c.trades)


def _future_path(last, *, steps=3):
    return [replace(
        last, opened_at=last.opened_at + timedelta(minutes=5 * j),
        closed_at=last.opened_at + timedelta(minutes=5 * (j + 1), milliseconds=-1),
        observed_at=last.opened_at + timedelta(minutes=5 * (j + 1), seconds=2),
        high_price=last.close_price + .001, low_price=last.close_price - .001,
    ) for j in range(2, steps + 2)]


def _insert_5m_path(conn, *, source, issued, options, asset="BTC"):
    """Insert both sides of a 5-minute path plus companion close targets.

    Mirrors the rows ``ProspectivePathLedger.issue`` writes for 15/60m,
    with ``target = reference + 10m`` (one 5-minute step plus the skipped
    partially elapsed bar) and ``horizon_minutes=5``.
    """
    sample = options["long"]
    target = source.opened_at + timedelta(minutes=10)
    for side in ("long", "short"):
        item = options[side]
        conn.execute(
            """INSERT INTO rival_forward_paths
             (venue,contract,quote,asset,horizon_minutes,model_version,side,
              reference_opened_at,issued_at,target_opened_at,reference_close,
              sigma_horizon,tp1_probability,tp2_probability,tp1_baseline,
              tp2_baseline,signed_return_p10,signed_return_p50,
              signed_return_p90,training_count,cohort_count,training_sha256,state)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source.venue, source.contract, source.quote, asset, 5, VERSION, side,
             source.opened_at.isoformat(), issued.isoformat(), target.isoformat(),
             source.close_price, item["sigma"], item["tp1_probability"],
             item["tp2_probability"], item["tp1_baseline"], item["tp2_baseline"],
             item["p10"], item["p50"], item["p90"], item["training_count"],
             item["cohort_count"], item["training_sha256"], "pending"),
        )
        conn.execute(
            """INSERT INTO rival_forward_path_targets (
                 venue,contract,quote,asset,horizon_minutes,
                 model_version,side,reference_opened_at,issued_at,
                 target_opened_at,reference_close,sigma_horizon,
                 direction_probability,direction_baseline,
                 early_move_probability,early_move_baseline,
                 absolute_move_p50,absolute_move_baseline,
                 close_target_training_count,close_target_training_sha256,state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source.venue, source.contract, source.quote, asset, 5, TARGET_VERSION, side,
             source.opened_at.isoformat(), issued.isoformat(), target.isoformat(),
             source.close_price, item["sigma"], item["direction_probability"],
             item["direction_baseline"], item["early_move_probability"],
             item["early_move_baseline"], item["absolute_move_p50"],
             item["absolute_move_baseline"], item["close_target_training_count"],
             item["close_target_training_sha256"], "pending"),
        )
    assert sample is not None  # keep the shared-options call honest


def _pending_sides(conn, horizon):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM rival_forward_paths"
        " WHERE state='pending' AND horizon_minutes=?",
        (horizon,),
    ).fetchone()["n"]


def _settled_sides(conn, horizon):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM rival_forward_paths"
        " WHERE state='settled' AND horizon_minutes=?",
        (horizon,),
    ).fetchone()["n"]


def test_five_minute_paths_are_issued(learning_store):
    """Issuance admits 5m; 15/60m issuance behaviour is unchanged."""
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = empirical_paths(candles, horizon=5, now=issued)
    assert set(options) == {"long", "short"}
    with learning_store.connect() as conn:
        assert ledger.issue(conn, candles=candles, asset="BTC", horizon=5,
                            now=issued, issued_at=issued) == 2
        assert _pending_sides(conn, 5) == 2
        # Issuing again is a no-op (ON CONFLICT DO NOTHING), not a duplicate.
        assert ledger.issue(conn, candles=candles, asset="BTC", horizon=5,
                            now=issued, issued_at=issued) == 0
        assert _pending_sides(conn, 5) == 2
        # 15-minute issuance is unchanged by admitting 5.
        assert ledger.issue(conn, candles=candles, asset="BTC", horizon=15,
                            now=issued, issued_at=issued) == 2
        assert _pending_sides(conn, 15) == 2


def test_issued_five_minute_path_settles_end_to_end(learning_store):
    """A path issued through ledger.issue() settles on one complete candle."""
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    future = _future_path(last, steps=1)
    now = future[-1].observed_at + timedelta(minutes=1)
    with learning_store.connect() as conn:
        assert ledger.issue(conn, candles=candles, asset="BTC", horizon=5,
                            now=issued, issued_at=issued) == 2
        for bar in future:
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(bar),
            )
    assert ledger.settle(now=now) == {"settled": 2, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _pending_sides(conn, 5) == 0
        assert _settled_sides(conn, 5) == 2
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_forward_path_targets"
            " WHERE state='settled' AND horizon_minutes=5"
        ).fetchone()["n"] == 2
    assert len(ledger.target_report(asset="BTC", horizon=5, now=now)) == 2


def test_five_minute_path_settles_on_one_complete_candle(learning_store):
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = empirical_paths(candles, horizon=15, now=issued)
    future = _future_path(last, steps=1)
    now = future[-1].observed_at + timedelta(minutes=1)
    with learning_store.connect() as conn:
        _insert_5m_path(conn, source=last, issued=issued, options=options)
        for bar in future:
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(bar),
            )
    assert ledger.settle(now=now) == {"settled": 2, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _pending_sides(conn, 5) == 0
        assert _settled_sides(conn, 5) == 2
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_forward_path_targets"
            " WHERE state='settled' AND horizon_minutes=5"
        ).fetchone()["n"] == 2
    assert len(ledger.target_report(asset="BTC", horizon=5, now=now)) == 2


@pytest.mark.parametrize(
    "blocked", ["missing", "observed_before_issue", "future_receipt"],
)
def test_five_minute_path_stays_pending_without_one_complete_candle(
    learning_store, blocked,
):
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = empirical_paths(candles, horizon=15, now=issued)
    future = _future_path(last, steps=1)
    now = future[-1].observed_at + timedelta(minutes=1)
    with learning_store.connect() as conn:
        _insert_5m_path(conn, source=last, issued=issued, options=options)
        if blocked != "missing":
            observed = (
                issued - timedelta(seconds=1)
                if blocked == "observed_before_issue"
                else now + timedelta(minutes=1)
            )
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(replace(future[0], observed_at=observed)),
            )
    assert ledger.settle(now=now) == {"settled": 0, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _pending_sides(conn, 5) == 2
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rival_forward_path_targets"
            " WHERE state='pending' AND horizon_minutes=5"
        ).fetchone()["n"] == 2


def test_five_minute_paths_never_starve_longer_horizons(learning_store, monkeypatch):
    """5m fills only leftover capacity after 15/60m, even when newer.

    The 5m paths below target 5 minutes LATER than the 15m paths, so a
    naive ``IN (5, 15, 60)`` selection ordered by newest target would
    settle the 5m paths first and starve the 15m batch. Strict priority
    (15/60m first, 5m only from leftover slots) must settle all four
    15m paths in cycle 1 and all four 5m paths in cycle 2.
    """
    from parlay_bot.perpetuals import path_forward

    monkeypatch.setattr(path_forward, "MAX_SETTLE", 4)
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = empirical_paths(candles, horizon=15, now=issued)
    future15 = _future_path(last, steps=3)
    # After the newest 5m bar (opened last+25m) is first observed
    # (last+30m+2s) and its target is 5m old, so both horizons are due.
    now = last.opened_at + timedelta(minutes=31, seconds=2)
    with learning_store.connect() as conn:
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            source = replace(last, contract=asset)
            assert ledger.issue(conn, candles=[source], asset=asset, horizon=15,
                                now=issued, issued_at=issued, options=options) == 2
            for bar in future15:
                conn.execute(
                    "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    _row(replace(bar, contract=asset)),
                )
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            # Reference 15 minutes later: this 5m target (ref + 10m) is
            # newer than the 15m targets above (ref + 20m).
            reference = replace(
                last, contract=asset,
                opened_at=last.opened_at + timedelta(minutes=15),
                closed_at=last.closed_at + timedelta(minutes=15),
                observed_at=last.observed_at + timedelta(minutes=15),
            )
            issued5 = reference.observed_at + timedelta(seconds=1)
            _insert_5m_path(conn, source=reference, issued=issued5,
                            options=options, asset=asset)
            for bar in _future_path(reference, steps=1):
                conn.execute(
                    "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    _row(replace(bar, contract=asset)),
                )
    # Cycle 1: every 15m side settles; no 5m side may consume a slot.
    assert ledger.settle(now=now) == {"settled": 8, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _settled_sides(conn, 15) == 8
        assert _pending_sides(conn, 5) == 8
    # Cycle 2: with 15/60m drained, the leftover bound settles the 5m batch.
    assert ledger.settle(now=now) == {"settled": 8, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _pending_sides(conn, 5) == 0
        assert _settled_sides(conn, 5) == 8
    assert ledger.settle(now=now) == {"settled": 0, "ambiguous": 0}
