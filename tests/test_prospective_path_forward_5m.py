"""Five-minute prospective paths: issuance, admission, completeness, capacity.

A 5-minute path needs exactly one complete exact-source candle. The
settlement worker gives 5-minute paths a guaranteed reserve of
MAX_SETTLE//5 paths per settle call (20 at the 100-path bound), with
15/60-minute paths keeping the remaining 80 and either horizon group
able to use the other's unused slots; the total stays bounded at
MAX_SETTLE per cycle. Issuance admits 5-minute horizons through
``HORIZONS`` in ``ProspectivePathLedger.issue`` / ``empirical_paths``
and through ``horizon in (5, 15, 60)`` in ``forward_audit``. The
settlement tests below seed 5-minute rows directly; the issuance tests
issue them through ``ProspectivePathLedger.issue`` instead.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.path_forward import (
    HORIZONS,
    MAX_SETTLE,
    TARGET_VERSION,
    VERSION,
    ProspectivePathLedger,
    _batched_candles,
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
    """5m never displaces 15/60m, even when newer.

    The 5m paths below target 5 minutes LATER than the 15m paths, so a
    naive ``IN (5, 15, 60)`` selection ordered by newest target would
    settle the 5m paths first and starve the 15m batch. At the patched
    MAX_SETTLE of 4 the 5m reserve (4//5) is zero, so cycle 1 must
    settle all four 15m paths and no 5m path; cycle 2 drains the 5m
    batch from the slots 15/60m left unused.
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


def _build_starved_fixture(ledger, store):
    """120 new paths (40 streams x 3 horizons) + 101 legacy 15m paths,
    every path with complete future candles. Returns the settle ``now``."""
    PerpetualMarketStore(store).initialize()
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = {h: empirical_paths(candles, horizon=h, now=issued) for h in HORIZONS}
    future = _future_path(last, steps=12)
    now = future[-1].observed_at + timedelta(minutes=1)
    venues = ("hyperliquid", "binance", "dydx", "bitget", "gate",
              "okx", "kucoin", "deribit", "lighter", "phemex")
    with store.connect() as conn:
        for index in range(MAX_SETTLE + 1):  # 101 legacy 15m paths
            shift = timedelta(days=-3, minutes=20 * index)
            prior = replace(last, opened_at=last.opened_at + shift,
                            closed_at=last.closed_at + shift,
                            observed_at=last.observed_at + shift)
            assert ledger.issue(conn, candles=[prior], asset="BTC", horizon=15,
                                now=issued + shift, issued_at=issued + shift,
                                options=options[15]) == 2
            for bar in _future_path(prior):
                conn.execute(
                    "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    _row(bar))
        for venue in venues:
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                source = replace(last, venue=venue, contract=asset)
                for horizon in HORIZONS:
                    assert ledger.issue(conn, candles=[source], asset=asset,
                                        horizon=horizon, now=issued,
                                        issued_at=issued,
                                        options=options[horizon]) == 2
                for bar in _future_path(source, steps=12):
                    conn.execute(
                        "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _row(bar))
    return now


def test_five_minute_paths_get_reserved_capacity_when_longer_horizons_exceed_cap(learning_store):
    """The 20-path 5m reserve settles 5m even when 15/60m due exceeds the cap.

    Fixture: 120 new paths (40 streams x 3 horizons) + 101 legacy 15m
    paths, all with complete future candles. Under leftover-only
    priority the first settle took 0 five-minute sides; with the
    reserve it takes exactly the 20-path reserve (40 sides) while
    15/60m keep their 80 (160 sides), and the 5m queue drains fully
    in the following settles.
    """
    ledger = ProspectivePathLedger(learning_store)
    now = _build_starved_fixture(ledger, learning_store)
    assert ledger.settle(now=now) == {"settled": 200, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _settled_sides(conn, 5) == 40
        assert _settled_sides(conn, 15) + _settled_sides(conn, 60) == 160
        assert _pending_sides(conn, 5) == 40
    assert ledger.settle(now=now) == {"settled": 200, "ambiguous": 0}
    assert ledger.settle(now=now) == {"settled": 42, "ambiguous": 0}
    with learning_store.connect() as conn:
        assert _pending_sides(conn, 5) == 0
        assert _pending_sides(conn, 15) + _pending_sides(conn, 60) == 0
    assert ledger.settle(now=now) == {"settled": 0, "ambiguous": 0}


def test_batched_candle_fetch_shares_rows_across_overlapping_keys(learning_store):
    """One batched fetch hands a shared bar to every key whose range holds it.

    Keys on one stream (hyperliquid/BTC/USDC) with overlapping ranges:
    a 5m path and a 15m path from the same reference share the
    reference+10m bar — the 15m key needs it as its first of three
    bars, the 5m key as its only bar. A second 5m path from a later
    reference settles too. A 5m path whose only bar is misaligned
    (opened 7m after its reference) passes the readiness COUNT but
    fails the exact-spacing validation and stays pending, and a 5m
    path with no candles at all is never selected. All four keys are
    fetched by the single batched query; if distribution missed the
    shared bar, the 15m path would stay pending.
    """
    PerpetualMarketStore(learning_store).initialize()
    ledger = ProspectivePathLedger(learning_store)
    ledger.initialize()
    candles = _candles()
    last = candles[-1]
    issued = last.observed_at + timedelta(seconds=1)
    options = empirical_paths(candles, horizon=15, now=issued)
    # After the last target is due: the missing-candle 5m target
    # (reference+10m) is 5m old and every bar is first-observed.
    now = last.opened_at + timedelta(minutes=61)
    reference_b = replace(
        last, opened_at=last.opened_at + timedelta(minutes=15),
        closed_at=last.closed_at + timedelta(minutes=15),
        observed_at=last.observed_at + timedelta(minutes=15),
    )
    reference_d = replace(
        last, opened_at=last.opened_at + timedelta(minutes=30),
        closed_at=last.closed_at + timedelta(minutes=30),
        observed_at=last.observed_at + timedelta(minutes=30),
    )
    reference_e = replace(
        last, opened_at=last.opened_at + timedelta(minutes=45),
        closed_at=last.closed_at + timedelta(minutes=45),
        observed_at=last.observed_at + timedelta(minutes=45),
    )
    defective_bar = replace(
        last, opened_at=last.opened_at + timedelta(minutes=37),
        closed_at=last.opened_at + timedelta(minutes=42, milliseconds=-1),
        observed_at=last.opened_at + timedelta(minutes=42, seconds=2),
        high_price=last.close_price + .001,
        low_price=last.close_price - .001,
    )
    with learning_store.connect() as conn:
        assert ledger.issue(conn, candles=[last], asset="BTC", horizon=15,
                            now=issued, issued_at=issued,
                            options=options) == 2
        _insert_5m_path(conn, source=last, issued=issued, options=options)
        _insert_5m_path(conn, source=reference_b,
                        issued=reference_b.observed_at + timedelta(seconds=1),
                        options=options)
        _insert_5m_path(conn, source=reference_d,
                        issued=reference_d.observed_at + timedelta(seconds=1),
                        options=options)
        _insert_5m_path(conn, source=reference_e,
                        issued=reference_e.observed_at + timedelta(seconds=1),
                        options=options)
        for bar in _future_path(last, steps=3):
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(bar))
        for bar in _future_path(reference_b, steps=1):
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(bar))
        conn.execute(
            "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _row(defective_bar))
    assert ledger.settle(now=now) == {"settled": 6, "ambiguous": 0}
    with learning_store.connect() as conn:
        # A (5m) + B (5m) settle; C (15m) settles only because the
        # shared reference+10m bar reached it; D (misaligned bar) and
        # E (no candles) stay pending.
        assert _settled_sides(conn, 5) == 4
        assert _settled_sides(conn, 15) == 2
        assert _pending_sides(conn, 5) == 4
        assert _pending_sides(conn, 15) == 0
    assert ledger.settle(now=now) == {"settled": 0, "ambiguous": 0}


def test_batched_candles_match_per_key_queries(learning_store):
    """_batched_candles returns exactly the per-key query's rows per key.

    Same stream, overlapping ranges (a 5m and a 15m key from one
    reference, plus a 5m key from a later reference): the shared bar
    must appear in both keys' lists, and each list must equal the
    rows the literal per-key SELECT returns for that key.
    """
    PerpetualMarketStore(learning_store).initialize()
    candles = _candles()
    last = candles[-1]
    reference_b = replace(
        last, opened_at=last.opened_at + timedelta(minutes=15),
        closed_at=last.closed_at + timedelta(minutes=15),
        observed_at=last.observed_at + timedelta(minutes=15),
    )
    issued = last.observed_at + timedelta(seconds=1)
    ref = last.opened_at
    keys = {
        ("hyperliquid", "BTC", "USDC", 5, ref.isoformat(),
         issued.isoformat(), (ref + timedelta(minutes=10)).isoformat()):
            (ref, ref + timedelta(minutes=10)),
        ("hyperliquid", "BTC", "USDC", 15, ref.isoformat(),
         issued.isoformat(), (ref + timedelta(minutes=20)).isoformat()):
            (ref, ref + timedelta(minutes=20)),
        ("hyperliquid", "BTC", "USDC", 5, (ref + timedelta(minutes=15)).isoformat(),
         issued.isoformat(), (ref + timedelta(minutes=25)).isoformat()):
            (ref + timedelta(minutes=15), ref + timedelta(minutes=25)),
    }
    per_key_sql = """SELECT opened_at,closed_at,first_observed_at,high_price,
                            low_price,close_price FROM perp_market_candles
                     WHERE venue=? AND contract=? AND quote=?
                       AND interval_minutes=5 AND opened_at>?
                       AND opened_at<=? ORDER BY opened_at"""
    columns = ("opened_at", "closed_at", "first_observed_at",
               "high_price", "low_price", "close_price")
    with learning_store.connect() as conn:
        for bar in _future_path(last, steps=3) + _future_path(reference_b, steps=1):
            conn.execute(
                "INSERT INTO perp_market_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(bar))
        batched = _batched_candles(conn, keys)
        assert set(batched) == set(keys)
        for key, (reference, target) in keys.items():
            expected = conn.execute(
                per_key_sql,
                (key[0], key[1], key[2],
                 (reference + timedelta(minutes=5)).isoformat(),
                 target.isoformat()),
            ).fetchall()
            got = [{column: row[column] for column in columns}
                   for row in batched[key]]
            assert got == [dict(row) for row in expected]
        # The reference+10m bar is shared by the 5m and 15m keys.
        key5, key15, key5b = keys
        assert len(batched[key5]) == 1
        assert len(batched[key15]) == 3
        assert len(batched[key5b]) == 1
        assert batched[key5][0]["opened_at"] == batched[key15][0]["opened_at"]
    assert _batched_candles(conn, {}) == {}
