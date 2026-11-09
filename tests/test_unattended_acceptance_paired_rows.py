"""The bounded acceptance sample read returns exactly the old full-join rows."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.unattended_acceptance import (
    _ASSETS,
    _HORIZONS,
    _SAMPLE,
    _paired_rows,
)

# The pre-fix query, kept verbatim (whitespace aside) as the reference.
_OLD = """SELECT f.venue,f.contract,f.quote,f.horizon_minutes,f.model_version,
 f.reference_opened_at,f.issued_at,f.target_opened_at,f.settled_at,f.outcome_up,
 f.model_brier AS incumbent_brier,s.family,s.training_sha256,s.probability_up,
 s.model_brier AS challenger_brier,s.incumbent_brier AS paired_incumbent_brier,
 s.settled_at AS challenger_settled_at,q.state AS quote_state,q.entry_at,q.exit_at,
 q.entry_bid,q.entry_ask,q.exit_bid,q.exit_ask
 FROM perp_forward_audit AS f JOIN rival_model_shadow_forecasts AS s
 ON s.venue=f.venue AND s.contract=f.contract AND s.quote=f.quote AND s.asset=f.asset
 AND s.horizon_minutes=f.horizon_minutes AND s.reference_opened_at=f.reference_opened_at
 AND s.issued_at=f.issued_at AND s.target_opened_at=f.target_opened_at
 LEFT JOIN rival_family_quote_shadow AS q ON q.venue=s.venue AND q.contract=s.contract
 AND q.quote=s.quote AND q.horizon_minutes=s.horizon_minutes AND q.family=s.family
 AND q.reference_opened_at=s.reference_opened_at
 WHERE f.asset=? AND f.horizon_minutes=? AND f.issued_at>=? AND f.issued_at<=?
 AND f.settled_at IS NOT NULL AND s.settled_at IS NOT NULL
 ORDER BY f.issued_at DESC LIMIT ?"""


def _old_rows(store, stamp, days):
    start, rows = (stamp - timedelta(days=days)).isoformat(), []
    with store.connect() as conn:
        for asset in _ASSETS:
            for horizon in _HORIZONS:
                rows.extend(conn.execute(
                    _OLD, (asset, horizon, start, stamp.isoformat(), _SAMPLE)).fetchall())
    rows.sort(key=lambda r: str(r["issued_at"]), reverse=True)
    return rows[:_SAMPLE]


def _seed(store, now):
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    ShadowModelCompetition(store).initialize()
    FamilyQuoteShadow(store).initialize()
    with store.connect() as conn:
        for asset, horizon in (("BTC", 5),):  # one scope: no merge to mask a short scope
            for i in range(400):  # every 9 min: some rows fall outside 2 days
                issued = now - timedelta(minutes=10 + i * 9)
                ref = (issued - timedelta(minutes=horizon)).isoformat()
                done = (issued + timedelta(minutes=horizon + 1)).isoformat()
                settled = i % 11 != 3
                conn.execute(
                    "INSERT INTO perp_forward_audit (venue,contract,quote,asset,"
                    "horizon_minutes,model_version,reference_opened_at,reference_close,"
                    "issued_at,target_opened_at,probability_up,baseline_up,"
                    "training_evaluated,settled_at,outcome_up,model_brier)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("okx", asset, "USDT", asset, horizon, MODEL, ref, 100.0,
                     issued.isoformat(), issued.isoformat(), .6, .5, 100,
                     done if settled else None, i % 2 if settled else None,
                     .16 if settled else None))
                # Newest 120 rows (challengers paused) and every 7th: no pair,
                # so the newest audit rows alone hold too few pairs. Every 5th
                # has only unsettled pairs; the rest have 1-3 families.
                if i < 120 or i % 7 == 0:
                    continue
                ok = settled and i % 5 != 0
                for k, family in enumerate(("nb_v1", "knn_v1", "liq_v1")[: 1 + i % 3]):
                    conn.execute(
                        "INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,"
                        "asset,horizon_minutes,family,reference_opened_at,issued_at,"
                        "target_opened_at,probability_up,incumbent_up,baseline_up,"
                        "training_count,training_sha256,settled_at,outcome_up,"
                        "model_brier,incumbent_brier,baseline_brier)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("okx", asset, "USDT", asset, horizon, family, ref,
                         issued.isoformat(), issued.isoformat(), .55, .6, .5, 140,
                         "a" * 64, done if ok else None, i % 2 if ok else None,
                         *((.2, .16, .25) if ok else (None,) * 3)))
                    if k == 0 and i % 4 == 0:
                        conn.execute(
                            "INSERT INTO rival_family_quote_shadow (venue,contract,quote,"
                            "asset,horizon_minutes,family,reference_opened_at,issued_at,"
                            "target_close_at,training_sha256,challenger_up,incumbent_up,"
                            "challenger_side,incumbent_side,entry_at,entry_bid,entry_ask,"
                            "quantity,state,exit_at,exit_bid,exit_ask)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("okx", asset, "USDT", asset, horizon, family, ref,
                             issued.isoformat(), done, "a" * 64, .55, .6, "UP", "UP",
                             (issued + timedelta(seconds=20)).isoformat(), 100.0, 100.1,
                             1.0, "EXIT_QUOTED", done, 101.0, 101.1))


def _same(new, old):
    """Same issued_at order and rows; families sharing an issued_at (and the
    cut at the last issued_at) have no defined order in either query."""
    assert [r["issued_at"] for r in new] == [r["issued_at"] for r in old]
    edge = new[-1]["issued_at"] if new else None

    def rows(sample):
        return sorted(tuple(sorted((k, str(v)) for k, v in dict(r).items()))
                      for r in sample if r["issued_at"] != edge)
    assert rows(new) == rows(old)


def test_paired_rows_match_the_old_full_join(learning_store):
    now = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
    _seed(learning_store, now)
    for days in (2, 21):
        new = _paired_rows(learning_store, now, days)
        assert len(new) == _SAMPLE
        assert len({r["issued_at"] for r in new}) < _SAMPLE  # pairs, not audit rows
        _same(new, _old_rows(learning_store, now, days))
    short = now - timedelta(hours=20)  # fewer than _SAMPLE pairs in the window
    few = _paired_rows(learning_store, short, 0.1)
    assert 0 < len(few) < _SAMPLE
    _same(few, _old_rows(learning_store, short, 0.1))
