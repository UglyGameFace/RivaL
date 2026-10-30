"""Real, point-in-time forward audit: issue first, settle once, never invent skill."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def seed(market, *, now, bars=150, venue="hyperliquid"):
    # Completed bars were observed progressively, not in one later backfill.
    boundary = now.replace(minute=now.minute // 5 * 5,
                           second=0, microsecond=0)
    latest_open = boundary - timedelta(minutes=5)
    first = latest_open - timedelta(minutes=5 * (bars - 1))
    market.initialize()
    with market.store.connect() as conn:
        for i in range(bars):
            opened = first + timedelta(minutes=5 * i)
            closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
            observed = closed + timedelta(milliseconds=1)
            p = 100 + i / 20 + (i % 7) / 5
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    venue, "BTC", "USDC", 5, opened.isoformat(), closed.isoformat(),
                    observed.isoformat(), p, p + 2, p - 2, p + 0.5, 10, 20,
                ),
            )
    return latest_open


def test_live_predictions_are_immutable_deduped_and_settled_once(tmp_path):
    store = SQLiteHotStore(tmp_path / "prospective.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    now = datetime.now(UTC)
    latest = seed(market, now=now)
    try:
        issued, settled = monitor.run_once(now=now)
        assert issued == 2 and settled == 0
        assert monitor.run_once(now=now) == (0, 0)
        with store.connect() as conn:
            records = conn.execute(
                "SELECT * FROM perp_forward_audit ORDER BY horizon_minutes"
            ).fetchall()
        assert len(records) == 2
        assert {x["horizon_minutes"] for x in records} == {5, 15}
        assert all(0 < x["probability_up"] < 1 for x in records)
        assert all(int(x["training_evaluated"]) >= 20 for x in records)
        assert all(x["settled_at"] is None for x in records)
        # Insert outcome only AFTER forecast issuance. A source-specific
        # completed target is observable only in the future.
        target_open = latest + timedelta(minutes=5)
        target_closed = target_open + timedelta(minutes=5) - timedelta(milliseconds=1)
        known = now + timedelta(minutes=8)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", 5, target_open.isoformat(),
                 target_closed.isoformat(), known.isoformat(), 110, 115, 108,
                 112, 12, 20),
            )
        assert monitor._settle(now + timedelta(minutes=9)) == 1
        assert monitor._settle(now + timedelta(minutes=9)) == 0
        summary = monitor.summary(asset="BTC", now=now + timedelta(minutes=9))
        recent = summary["last_7d"][5]
        assert recent["issued"] == 1 and recent["settled"] == 1
        assert 0 <= recent["brier"] <= 1
        assert 0 <= recent["baseline_brier"] <= 1
        assert recent["model_beats_baseline"] is None  # N=1 cannot qualify.
        assert summary["last_7d"][15]["settled"] == 0
        assert not summary["independent_trials"]
    finally:
        store.close()


def test_backfilled_history_does_not_issue_false_live_predictions(tmp_path):
    store = SQLiteHotStore(tmp_path / "backfill.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    now = datetime.now(UTC)
    seed(market, now=now)
    with store.connect() as conn:
        conn.execute(
            "UPDATE perp_market_candles SET first_observed_at=?",
            (now.isoformat(),),
        )
    try:
        assert monitor.run_once(now=now) == (0, 0)
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit"
            ).fetchone()["n"] == 0
    finally:
        store.close()


def test_prior_to_issue_outcome_cannot_settle_and_source_is_pinned(tmp_path):
    store = SQLiteHotStore(tmp_path / "censor.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    now = datetime.now(UTC)
    latest = seed(market, now=now)
    try:
        assert monitor.run_once(now=now)[0] == 2
        target = latest + timedelta(minutes=5)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", 5, target.isoformat(),
                 (target + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                 (now - timedelta(minutes=1)).isoformat(), 100, 110,
                 99, 108, 10, 20),
            )
        assert monitor._settle(now + timedelta(minutes=10)) == 0
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit WHERE settled_at IS NOT NULL"
            ).fetchone()["n"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_research_accuracy_tab_reads_real_database_without_invented_results(tmp_path):
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard

    store = SQLiteHotStore(tmp_path / "menu.sqlite")
    market = PerpetualMarketStore(store)
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("hyperliquid", "dydx"), shadow_enabled=True,
    )
    try:
        assert "rival:research:accuracy" in {
            item.custom_id for item in view.children
        }
        view.screen = "accuracy"
        view._rebuild()
        embed = await view.render()
        fields = {x.name: x.value for x in embed.fields}
        assert "Forward 5-minute evaluation" in fields
        assert "settled: **0**" in fields["Forward 5-minute evaluation"]
        assert "No measurable accuracy" in fields["Forward 5-minute evaluation"]
        view.section = "fifteen"
        fifteen = {x.name: x.value for x in (await view.render()).fields}
        assert "Forward 15-minute evaluation" in fifteen
        view.section = "method"
        method = {x.name: x.value for x in (await view.render()).fields}
        assert "future-outcome scores" in method["How accuracy is audited"]
        assert embed.timestamp is not None
    finally:
        view.stop()
        store.close()


def test_settlement_queue_prefers_oldest_eligible_outcomes_after_downtime(tmp_path):
    """New predictions cannot starve older matured, actually observed labels."""
    from parlay_bot.perpetuals.forward_audit import MAX_PENDING_SETTLEMENTS, MODEL

    store = SQLiteHotStore(tmp_path / "backlog.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    now = datetime.now(UTC)
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    total = MAX_PENDING_SETTLEMENTS + 45
    try:
        with store.connect() as conn:
            for i in range(total):
                target = boundary - timedelta(minutes=5 * (total + 2 - i))
                reference = target - timedelta(minutes=5)
                issued = target + timedelta(seconds=3)
                conn.execute(
                    """INSERT INTO perp_forward_audit
                       (venue,contract,quote,asset,horizon_minutes,model_version,
                        reference_opened_at,reference_close,issued_at,
                        target_opened_at,probability_up,baseline_up,training_evaluated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
                     reference.isoformat(), 100, issued.isoformat(),
                     target.isoformat(), .65, .50, 60),
                )
                if i < 12:
                    continue  # Missing target must not block the queue.
                price = 100 if i < 17 else 102
                observed = (
                    issued - timedelta(seconds=1) if 17 <= i < 20
                    else target + timedelta(minutes=5, seconds=2)
                )
                conn.execute(
                    """INSERT INTO perp_market_candles
                       (venue,contract,quote,interval_minutes,opened_at,
                        closed_at,first_observed_at,open_price,high_price,
                        low_price,close_price,volume_base,trades)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "BTC", "USDC", 5, target.isoformat(),
                     (target + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                     observed.isoformat(), 100, 105, 99, price, 1, 10),
                )
        assert monitor._settle(now) == MAX_PENDING_SETTLEMENTS
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT settled_at,outcome_up FROM perp_forward_audit ORDER BY target_opened_at"
            ).fetchall()
            assert rows[20]["settled_at"] is not None
            assert rows[-1]["settled_at"] is None
            # Missing targets and retrospectively observed outcomes never
            # settle. Flat closes DO settle now, as NOT UP: a tie is a
            # known completed target, not an unresolved one.
            assert all(r["settled_at"] is None for r in rows[:12])
            assert all(r["settled_at"] is None for r in rows[17:20])
            assert all(r["settled_at"] is not None for r in rows[12:17])
            assert all(int(r["outcome_up"]) == 0 for r in rows[12:17])
        restarted = ForwardAccuracyMonitor(market)
        assert restarted._settle(now) == total - 15 - MAX_PENDING_SETTLEMENTS
        assert restarted._settle(now) == 0
        with store.connect() as conn:
            scored = conn.execute(
                """SELECT model_brier,baseline_brier,outcome_up
                   FROM perp_forward_audit WHERE settled_at IS NOT NULL
                   AND outcome_up=1 LIMIT 1"""
            ).fetchone()
            assert scored["outcome_up"] == 1
            assert abs(scored["model_brier"] - .1225) < 1e-9
            assert abs(scored["baseline_brier"] - .25) < 1e-9
    finally:
        store.close()
