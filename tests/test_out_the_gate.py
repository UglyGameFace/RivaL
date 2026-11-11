"""Out-the-gate display: immutable issued call + recomputed live lean, no writes.

The OTG call is the issuance-time snapshot; the live lean is recomputed at
render from current candles and persisted weights. Neither surface may write
history: the only sanctioned display-path write is the one-time flow_v3
first-serve receipt, asserted separately.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.home import crypto_snapshot_card
from parlay_bot.discord_app.opportunities import crypto_desk_embed, recent_watches
from parlay_bot.discord_app.out_the_gate import (
    WATCH_THRESHOLD,
    below_line_label,
    confidence_pct,
    direction_of,
    lean_line,
    lean_short,
    live_lean,
    otg_display,
    otg_line,
    otg_snapshot,
    strength_band,
)
from parlay_bot.discord_app.signals import model_strength, signal_embed
from parlay_bot.discord_app.targeting import (
    TargetSnapshot,
    target_embed,
    target_snapshot,
)
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.flow_challenger_v3 import (
    FEATURE_DIGEST,
    N_INPUTS,
    FlowChallengerV3,
    _weights_sha256,
)
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import BookQuote, PerpReferenceBook
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_checkpoints import LiveModelCheckpoints
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.storage.hot import SQLiteHotStore

HISTORY_TABLES = (
    "perp_forward_audit",
    "perp_multi_forward",
    "rival_model_shadow_forecasts",
    "rival_family_promotion",
    "perp_model_promotion",
    "perp_model_governance",
    "rival_family_quote_shadow",
    "rival_live_model_checkpoints",
    "rival_family_promotion_receipts",
)


def seed_candles(market, now, *, venue="hyperliquid", contract="BTC",
                 quote="USDC", count=60, start_price=100.0, end_price=101.0):
    """Continuous 5m candles ending one minute before ``now``."""
    last_opened = now - timedelta(minutes=6)
    candles = []
    for i in range(count):
        opened = last_opened - timedelta(minutes=5 * (count - 1 - i))
        closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
        frac = 1.0 if count == 1 else i / (count - 1)
        close = start_price + (end_price - start_price) * frac
        prev_frac = 1.0 if count == 1 else max(i - 1, 0) / (count - 1)
        previous = start_price + (end_price - start_price) * prev_frac
        candles.append((opened, closed, previous, close))
    # One batch, one observation instant: ingest requires every candle's
    # observed_at to equal the batch's observed_at.
    observed = candles[-1][1] + timedelta(milliseconds=1)
    batch = tuple(MarketCandle(
        venue, contract, quote, 5, opened, closed, observed,
        previous, max(previous, close) * 1.001,
        min(previous, close) * 0.999, close, 10.0, 34,
    ) for opened, closed, previous, close in candles)
    market.ingest(MarketBatch((), batch, 5, observed))
    return batch


def seed_checkpoint(store, now, *, venue="hyperliquid", contract="BTC",
                    quote="USDC", horizon=5):
    """A real digest-valid checkpoint row, fitted the same way production does."""
    first = now - timedelta(minutes=500)
    rows = tuple(LabeledExample(
        venue=venue, contract=contract, quote=quote, horizon_minutes=horizon,
        issued_at=first + timedelta(minutes=5 * i),
        label_known_at=first + timedelta(minutes=5 * i + 6),
        features=(.1 if i % 2 else -.1, .2, .01, .03, .05, .2, .1, .1, .1, .1),
        label_up=i % 2,
    ) for i in range(80))
    return LiveModelCheckpoints(store).fit(rows, as_of=now)


def seed_audit_row(market, now, *, venue="hyperliquid", contract="BTC",
                   quote="USDC", probability=0.68, horizon=5,
                   model_version=MODEL, target_in_minutes=2):
    market.initialize()
    market.record_feed_health(
        venue=venue, asset="BTC", status="ok", attempted_at=now,
        new_candles=1, total_candles=1, last_candle_close=now - timedelta(minutes=1),
    )
    ForwardAccuracyMonitor(market).initialize()
    target = now + timedelta(minutes=target_in_minutes)
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, contract, quote, "BTC", horizon, model_version,
                (target - timedelta(minutes=5)).isoformat(), 100.0,
                (now - timedelta(seconds=30)).isoformat(),
                target.isoformat(), probability, 0.5, 120,
            ),
        )
    return target


def seed_withdrawn_pair(market, now):
    """A settled shadow row whose incumbent result was withdrawn (no-trade).

    Before the leak closure, any render that ran
    ShadowModelCompetition.initialize() rewrote this shadow row (outcome_up
    and briers cleared). After the closure it must survive refreshes intact.
    """
    issued = now - timedelta(days=1)
    target = issued + timedelta(minutes=5)
    reference = issued - timedelta(minutes=5)
    ForwardAccuracyMonitor(market).initialize()
    ShadowModelCompetition(market.store).initialize()
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated,
                settled_at,outcome_up,model_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
             reference.isoformat(), 100.0, issued.isoformat(),
             target.isoformat(), 0.68, 0.5, 120,
             (target + timedelta(minutes=5)).isoformat(), None, None),
        )
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts
               (venue,contract,quote,asset,horizon_minutes,family,
                reference_opened_at,issued_at,target_opened_at,
                probability_up,incumbent_up,baseline_up,training_count,
                training_sha256,settled_at,outcome_up,model_brier,
                incumbent_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("hyperliquid", "BTC", "USDC", "BTC", 5, "gaussian_nb_v1",
             reference.isoformat(), issued.isoformat(), target.isoformat(),
             0.62, 0.68, 0.5, 120, "seeded",
             (target + timedelta(minutes=5)).isoformat(), 0, 0.25, 0.20, 0.25),
        )


def dump_history(store) -> dict:
    """Byte-identical contents of every history table that exists."""
    with store.connect() as conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }
        dump = {}
        for table in HISTORY_TABLES:
            if table not in existing:
                # A table the display path merely created (empty DDL) holds
                # the same contents as a missing one: nothing. Only rows count.
                dump[table] = []
                continue
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            dump[table] = sorted(tuple(dict(row).values()) for row in rows)
        return dump


def mock_quotes(now):
    hyper = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=100.0, ask=100.1,
        bid_quantity=10, ask_quantity=10, exchange_at=now, received_at=now,
    )
    binance = BookQuote(
        asset="BTC", venue="binance_usdm", symbol="BTCUSDT",
        bid=100.0, ask=100.1, bid_quantity=9, ask_quantity=9,
        exchange_at=now, received_at=now,
    )
    return type("Quotes", (), {
        "get_reference": AsyncMock(return_value=hyper),
        "get": AsyncMock(return_value=binance),
    })()


def test_direction_confidence_and_strength_helpers():
    assert direction_of(0.62) == "UP"
    assert direction_of(0.38) == "DOWN"
    assert direction_of(0.5) == "FLAT"
    assert confidence_pct(0.62) == 62
    assert confidence_pct(0.38) == 62
    assert confidence_pct(0.5) == 50
    assert confidence_pct(0.999) == 100
    assert strength_band(0.62) == "directional watch"
    assert strength_band(0.54) == "weak"
    assert strength_band(0.51) == "very weak"
    assert strength_band(0.44) == "directional watch"
    for bad in (0.0, 1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            direction_of(bad)
        with pytest.raises(ValueError):
            confidence_pct(bad)
    assert strength_band(0.0) == "invalid"


def test_watch_threshold_and_bands_match_signals():
    from parlay_bot.discord_app.signals import WATCH_THRESHOLD as SIGNAL_THRESHOLD

    assert WATCH_THRESHOLD == SIGNAL_THRESHOLD == 0.56
    for p in (0.51, 0.52, 0.53, 0.54, 0.55, 0.56, 0.59, 0.62, 0.44, 0.38, 0.5):
        assert strength_band(p) == model_strength(p)


def test_display_module_imports_no_writer():
    """AST audit: the display module may not import or call any writer."""
    path = Path(__file__).resolve().parent.parent / "src" / "parlay_bot" / "discord_app" / "out_the_gate.py"
    tree = ast.parse(path.read_text())
    writers = {
        "_run_once", "issue", "settle", "evaluate", "evaluate_promotion",
        "review_model", "_withdraw_no_trade_labels", "record_promotion_receipt",
        "learn", "fit", "call",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in writers, f"writer import: {alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[-1] not in writers
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            assert name not in writers, f"writer call: {name}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(
                r"\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b",
                node.value, re.IGNORECASE,
            ), f"write SQL literal: {node.value[:60]}"


def test_otg_snapshot_is_the_issued_call():
    now = datetime.now(UTC).replace(microsecond=0)
    target = now + timedelta(minutes=2)
    watch = {
        "asset": "BTC", "venue": "hyperliquid", "horizon_minutes": 5,
        "probability_up": 0.68, "issued": now - timedelta(seconds=30),
        "target": target, "model_version": MODEL,
    }
    snapshot = otg_snapshot(watch)
    assert snapshot["direction"] == "UP"
    assert snapshot["confidence_pct"] == 68
    assert snapshot["probability"] == 0.68
    assert snapshot["issued"] == watch["issued"]
    assert snapshot["target"] == target
    assert snapshot["model_version"] == MODEL
    assert snapshot["calibrated"] is False
    assert "raw, uncalibrated model output" in otg_line(snapshot)
    assert below_line_label(snapshot) is None  # 68% is a directional watch
    weak = otg_snapshot({**watch, "probability_up": 0.54})
    assert below_line_label(weak) == (
        "Lean: UP 54% — below the 56% watch line, not a qualified call"
    )
    assert lean_line(None) == "Live lean unavailable — data stale"


def test_incumbent_lean_reads_persisted_weights(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "lean.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        seed_checkpoint(store, now)
        lean = live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now,
        )
        assert lean is not None
        assert 0 < lean["probability"] < 1
        assert direction_of(lean["probability"]) in ("UP", "DOWN")
        assert lean["calibrated"] is False
        assert lean["engine"] == "live"
        assert lean["as_of"] == now - timedelta(minutes=1)
        with store.connect() as conn:
            row = conn.execute(
                "SELECT trained_at FROM rival_live_model_checkpoints",
            ).fetchone()
        assert lean["weights_trained_at"] == row["trained_at"]
        assert lean_short(lean) is not None
        assert "Live lean:" in lean_line(lean)
        assert "updating as data arrives (not the issued call)" in lean_line(lean)
    finally:
        store.close()


def test_lean_unavailable_without_checkpoint_candles_or_when_stale(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        # No candles, no checkpoint.
        assert live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now,
        ) is None
        seed_candles(market, now)
        # Candles but no checkpoint row.
        assert live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now,
        ) is None
        seed_checkpoint(store, now)
        assert live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now,
        ) is not None
        # Same candles thirty minutes later are stale, never shown as current.
        assert live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now + timedelta(minutes=30),
        ) is None
        # Fewer than 40 candles cannot build the feature window.
        store2 = SQLiteHotStore(tmp_path / "few.sqlite")
        market2 = PerpetualMarketStore(store2)
        try:
            market2.initialize()
            seed_candles(market2, now, count=10)
            seed_checkpoint(store2, now)
            assert live_lean(
                market2, asset="BTC", horizon=5, venue="hyperliquid",
                contract="BTC", quote="USDC", now=now,
            ) is None
        finally:
            store2.close()
    finally:
        store.close()


def test_lean_never_substitutes_another_family(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "family.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        seed_checkpoint(store, now)
        # A substituted family with no read-only recompute path has no lean,
        # even though the incumbent's lean is computable. flow_v3 is retired
        # (Nov 7, 2026, PR #439): it has no lean either, never the
        # incumbent's.
        for family in ("gaussian_nb_v1", "knn21_v1", "boosted_v1", "calib_v1",
                       "flow_v3"):
            assert live_lean(
                market, asset="BTC", horizon=5, venue="hyperliquid",
                contract="BTC", quote="USDC", now=now,
                model_version=family,
            ) is None
        # No model version given means the incumbent: lean is available.
        assert live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now,
        ) is not None
    finally:
        store.close()


def test_rerender_changes_lean_not_otg(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "rerender.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now, end_price=101.0)
        seed_checkpoint(store, now)
        seed_audit_row(market, now, probability=0.68)
        watches = recent_watches(market, asset="BTC", now=now, limit=5)
        assert len(watches) == 1
        watch = watches[0]
        first_otg = otg_snapshot(watch, market=market)
        first_lean = live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now,
        )
        assert first_lean is not None
        # One new candle, five minutes on, price jumped 5%: the lean moves.
        later = now + timedelta(minutes=5)
        seed_candles(
            market, later, count=1, start_price=101.0, end_price=106.0,
        )
        second_otg = otg_snapshot(watch, market=market)
        second_lean = live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=later,
        )
        assert second_otg == first_otg  # issuance snapshot is immutable
        assert second_lean is not None
        assert second_lean["probability"] != first_lean["probability"]
        assert second_lean["as_of"] > first_lean["as_of"]
    finally:
        store.close()


def test_otg_display_composes_slot_and_empty_slot(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "slot.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        seed_candles(market, now, venue="gate", contract="BTC_USDT",
                    quote="USDT", count=1)
        seed_checkpoint(store, now)
        seed_audit_row(market, now, venue="hyperliquid", probability=0.68)
        seed_audit_row(market, now, venue="gate", contract="BTC_USDT",
                       quote="USDT", probability=0.54)
        view = otg_display(market, asset="BTC", horizon=5, now=now)
        assert view["status"] == "ok"
        assert view["engine"] == "live"
        assert view["headline"]["otg"]["venue"] == "hyperliquid"
        assert view["headline"]["otg"]["direction"] == "UP"
        assert view["headline"]["otg"]["confidence_pct"] == 68
        assert view["headline"]["lean"] is not None
        assert len(view["venues"]) == 2
        gate_row = next(v for v in view["venues"] if v["otg"]["venue"] == "gate")
        assert gate_row["otg"]["confidence_pct"] == 54
        assert below_line_label(gate_row["otg"]) is not None
        empty = otg_display(market, asset="ETH", horizon=5, now=now)
        assert empty["status"] == "no_forecast"
        assert empty["headline"] is None
        assert empty["venues"] == []
        with pytest.raises(ValueError):
            otg_display(market, asset="DOGE", horizon=5, now=now)
    finally:
        store.close()


def test_display_refreshes_do_not_mutate_history(tmp_path):
    """Render-path leak test: history contents byte-identical after refreshes."""
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "leak.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        seed_candles(market, now, venue="gate", contract="BTC_USDT",
                    quote="USDT", count=1)
        seed_checkpoint(store, now)
        seed_audit_row(market, now, venue="hyperliquid", probability=0.68)
        # Both watches strong: the first one target_snapshot reads must reach
        # the venue-proof lookup (venue_summary -> initialize()), or the
        # render-path UPDATE the leak test guards would never be exercised.
        seed_audit_row(market, now, venue="gate", contract="BTC_USDT",
                       quote="USDT", probability=0.68)
        # Warm up the schema first: the first initialize() runs the (now
        # settlement-only) withdrawal. The pair is seeded AFTER that, so a
        # render-path UPDATE would be the only thing that could rewrite it.
        target_snapshot(market, asset="BTC", horizon_minutes=5, now=now)
        seed_withdrawn_pair(market, now)
        with store.connect() as conn:
            row = conn.execute(
                "SELECT outcome_up FROM rival_model_shadow_forecasts "
                "WHERE family='gaussian_nb_v1'"
            ).fetchone()
        assert row["outcome_up"] == 0, "pair must start un-withdrawn"
        # Simulate a fresh process: the next display refresh is the first
        # initialize() call, which (before the leak closure) ran the UPDATE
        # on the shadow ledger. Resetting the readiness flags re-arms it.
        market._forward_schema_ready = False
        store._shadow_competition_ready = False
        before = dump_history(store)
        assert before["perp_forward_audit"], "fixture must seed history rows"
        # Every display surface, twice: Get Signal, target DM, desk, slot view.
        for _ in range(2):
            otg_display(market, asset="BTC", horizon=5, now=now)
            otg_display(market, asset="BTC", horizon=15, now=now)
            target_snapshot(market, asset="BTC", horizon_minutes=5, now=now)
            crypto_desk_embed(market, asset="BTC", now=now)
        after = dump_history(store)
        assert after == before
    finally:
        store.close()


@pytest.mark.asyncio
async def test_display_refreshes_do_not_mutate_history_via_signal_embed(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "leak-signal.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        seed_checkpoint(store, now)
        seed_audit_row(market, now, venue="hyperliquid", probability=0.68)
        # Warm up the schema first (see test_display_refreshes_do_not_mutate_history).
        target_snapshot(market, asset="BTC", horizon_minutes=5, now=now)
        seed_withdrawn_pair(market, now)
        with store.connect() as conn:
            row = conn.execute(
                "SELECT outcome_up FROM rival_model_shadow_forecasts "
                "WHERE family='gaussian_nb_v1'"
            ).fetchone()
        assert row["outcome_up"] == 0, "pair must start un-withdrawn"
        # Simulate a fresh process (see above): re-arm the first initialize().
        market._forward_schema_ready = False
        store._shadow_competition_ready = False
        before = dump_history(store)
        embed = await signal_embed(
            market, mock_quotes(now), asset="BTC", horizon_minutes=5, now=now,
        )
        fields = {f.name: f.value for f in embed.fields}
        assert "📡 Out the gate • live lean" in fields
        assert "Out the gate: UP 68%" in fields["📡 Out the gate • live lean"]
        assert "Live lean:" in fields["📡 Out the gate • live lean"]
        assert "Just watch: this is not a signal to trade." in fields[
            "📡 Out the gate • live lean"]
        assert dump_history(store) == before
    finally:
        store.close()


def test_flow_v3_first_serve_receipt_is_the_only_display_write(tmp_path):
    """The sanctioned exception: one receipt row, first serve only, nothing else."""
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "receipt.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        FlowChallengerV3(store).initialize()
        FamilyPromotion(store).initialize()
        ShadowModelCompetition(store).initialize()
        target = now + timedelta(minutes=2)
        issued = now - timedelta(seconds=30)
        reference = target - timedelta(minutes=5)
        # Persisted flow_v3 artifact, so selected() serves it. The recorded
        # weights SHA must be the real 64-char digest of the stored weights:
        # selected() fails closed on any other value.
        weights = [0.5] + [0.0] * N_INPUTS
        artifact = {
            "feature_digest": FEATURE_DIGEST,
            "weights_sha256": _weights_sha256(weights),
            "trained_through": (now - timedelta(minutes=10)).isoformat(),
            "graded_count": 1,
        }
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_flow3_model_state
                   (asset,horizon_minutes,weights,updates,positives,updated_at,
                    stats_json,calibration_json,feature_digest,weights_sha256,
                    trained_through,graded_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("BTC", 5, json.dumps(weights), 1, 0, now.isoformat(),
                 "{}", "{}", FEATURE_DIGEST, _weights_sha256(weights),
                 (now - timedelta(minutes=10)).isoformat(), 1),
            )
            conn.execute(
                """INSERT INTO rival_family_promotion
                   (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                    selected_family,status,reason,evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5,
                 (now - timedelta(minutes=5)).isoformat(), "flow_v3",
                 "promoted_paper", "seeded",
                 json.dumps({"flow_v3": {"artifact": artifact,
                                         "targets": 100, "days": 7,
                                         "challenger_trades": 25,
                                         "quote_coverage": 0.9}})),
            )
            conn.execute(
                """INSERT INTO rival_model_shadow_forecasts
                   (venue,contract,quote,asset,horizon_minutes,family,
                    reference_opened_at,issued_at,target_opened_at,
                    probability_up,incumbent_up,baseline_up,training_count,
                    training_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, "flow_v3",
                 reference.isoformat(), issued.isoformat(), target.isoformat(),
                 0.62, 0.68, 0.5, 120, "seeded"),
            )
        seed_audit_row(market, now, venue="hyperliquid", probability=0.68)
        before = dump_history(store)
        watches = recent_watches(market, asset="BTC", now=now, limit=5)
        assert len(watches) == 1
        assert watches[0]["model_version"] == "flow_v3"
        after_first = dump_history(store)
        changed = {
            table for table in after_first
            if after_first[table] != before.get(table, [])
        }
        assert changed == {"rival_family_promotion_receipts"}
        receipts = after_first["rival_family_promotion_receipts"]
        assert len(receipts) == 1
        # Second serve: deduped by primary key, no second row, nothing else.
        recent_watches(market, asset="BTC", now=now, limit=5)
        assert dump_history(store) == after_first
    finally:
        store.close()


def test_flow_v3_lean_is_gone_after_retirement(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "flowlean.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        challenger = FlowChallengerV3(store)
        challenger.initialize()
        weights = [0.5] + [0.0] * N_INPUTS
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_flow3_model_state
                   (asset,horizon_minutes,weights,updates,positives,updated_at,
                    stats_json,calibration_json,feature_digest,weights_sha256,
                    trained_through,graded_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("BTC", 5, json.dumps(weights), 1, 0, now.isoformat(),
                 "{}", "{}", FEATURE_DIGEST, "seeded",
                 (now - timedelta(minutes=10)).isoformat(), 1),
            )
            for minutes_ago in (20, 15, 10):
                bucket = now - timedelta(minutes=minutes_ago)
                conn.execute(
                    """INSERT INTO perp_liquidity_5m
                       (venue,asset,bucket_start,complete,book_samples,
                        spread_bps_mean,bid_depth_10bps_mean,ask_depth_10bps_mean,
                        bid_depth_25bps_mean,ask_depth_25bps_mean,
                        buy_trades,sell_trades,buy_volume,sell_volume,
                        recorded_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "BTC", bucket.isoformat(), 1, 10,
                     2.0, 1000.0, 1000.0, 2000.0, 2000.0,
                     5, 5, 100.0, 100.0, now.isoformat()),
                )
        # Retired Nov 7, 2026 (PR #439): persisted state and inputs are still
        # there, but there is no flow_v3 lean any more — frozen weights must
        # never render as a live lean.
        assert live_lean(
            market, asset="BTC", horizon=5, venue="hyperliquid",
            contract="BTC", quote="USDC", now=now, model_version="flow_v3",
        ) is None
    finally:
        store.close()


def test_target_fingerprint_covers_lean_but_not_status():
    base = TargetSnapshot(
        "BTC", 5, "UP", "Lean.", venue="hyperliquid", probability=0.54,
        price=100.0, live_lean_probability=0.58,
        live_lean_as_of=datetime.now(UTC),
    )
    changed_lean = TargetSnapshot(
        "BTC", 5, "UP", "Lean.", venue="hyperliquid", probability=0.54,
        price=100.0, live_lean_probability=0.59,
        live_lean_as_of=datetime.now(UTC),
    )
    assert base.fingerprint != changed_lean.fingerprint
    changed_status = TargetSnapshot(
        "BTC", 5, "UP", "Lean.", venue="hyperliquid", probability=0.54,
        price=100.0, live_lean_probability=0.58,
        live_lean_as_of=base.live_lean_as_of, status="weak_model",
    )
    assert base.fingerprint == changed_status.fingerprint


def test_auto_hunt_candidate_is_flat_at_exactly_half():
    from parlay_bot.discord_app.auto_hunt import HuntCandidate

    now = datetime.now(UTC)
    candidate = HuntCandidate(
        asset="BTC", horizon=5, venue="hyperliquid",
        issued=now, expires=now + timedelta(minutes=5),
        probability_up=0.5, bid=None, ask=None, quote_at=None,
        engine="live", model_version=MODEL,
    )
    assert candidate.direction == "FLAT"
    assert HuntCandidate(
        asset="BTC", horizon=5, venue="hyperliquid",
        issued=now, expires=now + timedelta(minutes=5),
        probability_up=0.51, bid=None, ask=None, quote_at=None,
        engine="live", model_version=MODEL,
    ).direction == "UP"
    assert HuntCandidate(
        asset="BTC", horizon=5, venue="hyperliquid",
        issued=now, expires=now + timedelta(minutes=5),
        probability_up=0.49, bid=None, ask=None, quote_at=None,
        engine="live", model_version=MODEL,
    ).direction == "DOWN"


def test_home_card_shows_lean_with_below_line_label():
    now = datetime.now(UTC)
    snapshot = TargetSnapshot(
        "BTC", 5, "UP", "Lean: UP 54% — below the 56% watch line.",
        venue="hyperliquid", probability=0.54, price=100.0,
        issued_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(minutes=4), status="weak_model",
        live_lean_probability=0.58,
        live_lean_as_of=now - timedelta(seconds=30),
    )
    card = crypto_snapshot_card(snapshot, asset="BTC", horizon=5, checked_at=now)
    assert "UP LEAN • RESEARCH ONLY" in card.title
    fields = {f.name: f.value for f in card.fields}
    assert "below the 56% watch line, not a qualified call" in fields["⚡ Strength"]
    assert "Live lean: UP 58%" in fields["⚡ Strength"]
    stale = TargetSnapshot(
        "BTC", 5, "UP", "Lean.", venue="hyperliquid", probability=0.54,
        price=100.0, issued_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(minutes=4), status="weak_model",
    )
    stale_card = crypto_snapshot_card(stale, asset="BTC", horizon=5, checked_at=now)
    stale_fields = {f.name: f.value for f in stale_card.fields}
    assert "Live lean unavailable — data stale" in stale_fields["⚡ Strength"]


def test_target_embed_shows_otg_and_lean_fields():
    now = datetime.now(UTC)
    snapshot = TargetSnapshot(
        "BTC", 5, "UP", "Lean: UP 54% — below the 56% watch line.",
        venue="hyperliquid", probability=0.54, price=100.0,
        issued_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(minutes=4), status="weak_model",
        live_lean_probability=0.58,
        live_lean_as_of=now - timedelta(seconds=30),
    )
    embed = target_embed(snapshot)
    assert "UP LEAN • NO SIGNAL" in embed.title
    fields = {f.name: f.value for f in embed.fields}
    assert "📡 Out the gate • live lean" in fields
    otg_field = fields["📡 Out the gate • live lean"]
    assert "Out the gate: UP 54%" in otg_field
    assert "raw, uncalibrated model output" in otg_field
    assert "Live lean: UP 58%" in otg_field
    assert "below the 56% watch line, not a qualified call" in otg_field
    assert "Just watch: this is not a signal to trade." in otg_field
    flat = TargetSnapshot("BTC", 5, "FLAT", "NO CALL: the latest model is tied.")
    flat_embed = target_embed(flat)
    assert "FLAT • NO CALL • COIN FLIP" in flat_embed.title


def test_desk_embed_shows_otg_and_lean_lines(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "desk.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        seed_candles(market, now)
        seed_checkpoint(store, now)
        seed_audit_row(market, now, venue="hyperliquid", probability=0.68)
        embed = crypto_desk_embed(market, asset="BTC", now=now)
        fields = {f.name: f.value for f in embed.fields}
        watch_field = next(
            value for name, value in fields.items()
            if name.startswith("👀 Leans UP")
        )
        # Nov 11 audit: one chance per card (it used to repeat it three
        # times); a "Now" line appears only when the live lean has moved.
        assert "Chance of UP: **68.0%**" in watch_field
        assert "Out the gate" not in watch_field
        assert "Now: UP 68%" not in watch_field      # never the same number twice
        assert "Just watch: this is not a signal to trade." in watch_field
    finally:
        store.close()
