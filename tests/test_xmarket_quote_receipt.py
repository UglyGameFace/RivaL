"""Measure actual source-matched quote progress without a new cost evaluator."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from parlay_bot.perpetuals.xmarket_quote_receipt import read_xmarket_quotes
from tests.test_family_quote_progress import _book, _forecast, _observer

PROGRESS = {"scope_progress":[{"asset":"BTC","horizon_minutes":15,
                                 "stored_gate_passing_artifacts":1}]}


def _hyper(report):
    return next(row for row in report["scopes"] if row["venue"] == "hyperliquid")


def test_real_quote_entries_and_exits_advance_without_borrowing_another_source(learning_store):
    now = datetime.now(UTC).replace(microsecond=0)
    observer = _observer(learning_store)
    target = now+timedelta(minutes=10)
    with learning_store.connect() as conn:
        _forecast(conn,"xmarket_v1",issued=now-timedelta(seconds=30),target=target)
        _forecast(conn,"xmarket_v1",issued=now+timedelta(minutes=1),
                  target=target+timedelta(minutes=5))
        conn.execute("""INSERT INTO rival_model_shadow_forecasts
            (venue,contract,quote,asset,horizon_minutes,family,
             reference_opened_at,issued_at,target_opened_at,probability_up,
             incumbent_up,baseline_up,training_count,training_sha256)
            SELECT 'binance','BTCUSDT','USDT',asset,horizon_minutes,family,
                   reference_opened_at,issued_at,target_opened_at,probability_up,
                   incumbent_up,baseline_up,training_count,training_sha256
            FROM rival_model_shadow_forecasts WHERE issued_at<=?""",(now.isoformat(),))
    assert observer.observe(_book(now),now=now)["opened"] == 1
    report = read_xmarket_quotes(learning_store,now,PROGRESS)
    assert report["status"] == "recorded"
    assert report["read_only"] and report["does_not_prove_prediction_edge"]
    assert not report["counts_are_independent_targets"]
    hyper = _hyper(report)
    assert hyper["forecast_rows"] == hyper["entry_rows"] == 1
    assert hyper["closed_exit_rows"] == 0
    assert hyper["latest_entry_at"] == now.isoformat()
    binance = next(row for row in report["scopes"] if row["venue"] == "binance")
    assert binance["forecast_rows"] == 1 and binance["entry_rows"] == 0
    closed = target+timedelta(minutes=5,seconds=20)
    assert observer.observe(_book(closed),now=closed)["closed"] == 1
    advanced = _hyper(read_xmarket_quotes(learning_store,closed,PROGRESS))
    assert advanced["entry_rows"] == advanced["closed_exit_rows"] == 1
    assert advanced["latest_exit_at"] == closed.isoformat()
    # Same instrument/reference with a different artifact cannot borrow proof.
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_family_quote_shadow SET training_sha256=?",("b"*64,))
    mismatch = _hyper(read_xmarket_quotes(learning_store,closed,PROGRESS))
    assert mismatch["entry_rows"] == mismatch["closed_exit_rows"] == 0
    assert mismatch["latest_entry_at"] is None


def test_quote_receipt_selects_only_and_distinguishes_missing_ledger(learning_store):
    now = datetime.now(UTC)
    reads = []

    @contextmanager
    def connect():
        with learning_store.connect() as conn:
            class ReadOnly:
                def execute(self,sql,params=()):
                    assert sql.lstrip().upper().startswith("SELECT ")
                    reads.append(sql)
                    return conn.execute(sql,params)
            yield ReadOnly()

    readonly = SimpleNamespace(connect=connect)
    assert read_xmarket_quotes(readonly,now,PROGRESS)["status"] == "read_unavailable"
    _observer(learning_store)
    empty = read_xmarket_quotes(readonly,now,PROGRESS)
    assert empty["status"] == "recorded"
    assert all(row["forecast_rows"] == row["entry_rows"] == 0 for row in empty["scopes"])
    assert reads
    assert read_xmarket_quotes(readonly,now.replace(tzinfo=None),PROGRESS)[
        "status"
    ] == "read_unavailable"
    assert read_xmarket_quotes(readonly,now,{})["status"] == "read_unavailable"
    assert read_xmarket_quotes(readonly,now,{"scope_progress":[]})[
        "status"
    ] == "no_fitted_scopes"
