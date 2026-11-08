"""Liquidity track (Phase 3, spec §2.10): converter, liq_v1, Track C verdict.

Track A: the converter produces live-bucket-shaped sidecar rows only --
spread + top-5 imbalance from depth archives (labelled approximations),
exact tape buckets from trades archives, nothing invented, nothing
zero-filled, never a live row. Track B: liq_v1 shadow-issues from live
buckets, settles in shadow, stops on its kill switch, and serves only
through the Route B continuity check. Track C: the Binance tape verdict
(include, flagged) is recorded in the archive contract with the
context-absent 0.005 bar as a gate (enforcement proven by
tests/test_archive_model.py::test_context_absent_failure_blocks_serving_capable_artifact).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parlay_bot.discord_app.out_the_gate import live_lean, otg_snapshot
from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.family_governance import BLOCKED_FAMILIES, FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.liquidity_challenger_v1 import (
    FAMILY as LIQ_FAMILY,
)
from parlay_bot.perpetuals.liquidity_challenger_v1 import (
    FEATURE_CONTRACT as LIQ_CONTRACT,
)
from parlay_bot.perpetuals.liquidity_challenger_v1 import (
    FEATURE_DIGEST as LIQ_DIGEST,
)
from parlay_bot.perpetuals.liquidity_challenger_v1 import (
    FLAG_NAMES,
    FLOW_FEATURE_NAMES,
    N_FLOW,
    LiquidityChallengerV1,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import (
    CHALLENGER_PREFIXES,
    ShadowModelCompetition,
)
from parlay_bot.perpetuals.promotion_receipts import record_promotion_receipt
from parlay_bot.research import archive_features, archive_training
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.liquidity_converter import (
    CONVERTER_VERSION,
    convert_gate_slice,
    convert_okx_l2,
    convert_okx_trades,
    convert_tardis_trades,
    load_converted_sidecar,
    overlap_report,
    write_converted_sidecar,
)
from parlay_bot.research.liquidity_converter import (
    main as converter_main,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 4, 12, 0, 30, tzinfo=UTC)


# ---------- shared store/bucket fixtures (flow_v3 test pattern) ----------

def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "liq1.sqlite")
    LiquidityChallengerV1(store).initialize()
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_forward_audit (
            venue TEXT, contract TEXT, quote TEXT, horizon_minutes INTEGER,
            reference_opened_at TEXT, settled_at TEXT, outcome_up INTEGER,
            asset TEXT)""")
    return store


def _bucket(conn, venue, start, recorded, buy, sell, bid=10.0, ask=5.0,
            long_liq=0.0, short_liq=0.0, spread=1.0):
    conn.execute(
        """INSERT INTO perp_liquidity_5m (venue,asset,bucket_start,complete,book_samples,
           spread_bps_mean,spread_bps_max,mid_last,bid_depth_10bps_mean,ask_depth_10bps_mean,
           bid_depth_25bps_mean,ask_depth_25bps_mean,depth_25bps_covered_fraction,
           buy_trades,sell_trades,buy_volume,sell_volume,first_event_at,last_event_at,
           recorded_at,long_liq_volume,short_liq_volume,long_liq_count,short_liq_count)
           VALUES (?, 'BTC', ?, 1, 10, ?, 2.0, 100, ?, ?, ?, ?, 1, 5, 5, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
        (venue, start.isoformat(), spread, bid, ask, bid, ask, buy, sell,
         start.isoformat(), start.isoformat(), recorded.isoformat(),
         long_liq, short_liq))


def _context(conn, observed, funding=0.0, oi=1000.0, mark=100.0, oracle=100.0):
    conn.execute(
        """INSERT INTO perp_market_contexts
           (venue,contract,quote,observed_at,mark_price,oracle_price,
            funding_rate,open_interest_base)
           VALUES ('hyperliquid','BTC','USDC',?,?,?,?,?)""",
        (observed.isoformat(), mark, oracle, funding, oi))


class _Bundle:
    """Fake live BinanceContextFeed bundle (in-memory, like the real one)."""

    def __init__(self, funding=(), oi=()):
        self._funding = funding
        self._oi = oi

    def bundle(self, asset):
        return {"funding": self._funding, "open_interest": self._oi}


def _seed_cycle_candles(market, *, now, bars=150):
    """150 live 5m bars on hyperliquid/BTC, as the cycle diagnostics use."""
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    market.initialize()
    with market.store.connect() as conn:
        for i in range(bars):
            opened = boundary - timedelta(minutes=5 * (bars - i))
            closed = opened + timedelta(minutes=5, milliseconds=-1)
            observed = closed + timedelta(milliseconds=1)
            p = 100 + i / 15 + (i % 9) / 8
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "hyperliquid", "BTC", "USDC", 5,
                    opened.isoformat(), closed.isoformat(), observed.isoformat(),
                    p, p + 2, p - 2, p + 0.5, 10, 12,
                ),
            )


def _promotion_row(store, *, family, evaluated_at, artifact, asset="BTC"):
    FamilyPromotion(store).initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                selected_family,status,reason,evidence_json)
               VALUES ('hyperliquid','BTC','USDC',?,5,?,?,'promoted_paper','ok',?)
               ON CONFLICT(venue,contract,quote,horizon_minutes)
               DO UPDATE SET evaluated_at=excluded.evaluated_at,
                selected_family=excluded.selected_family,
                status=excluded.status, reason=excluded.reason,
                evidence_json=excluded.evidence_json""",
            (asset, evaluated_at.isoformat(), family,
             json.dumps({family: {"artifact": artifact}})))


def _seed_liq1_state(store):
    """One graded liq_v1 row, learned: returns the current artifact."""
    challenger = LiquidityChallengerV1(store)
    flow = (0.0,) * 21 + (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    with store.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS perp_forward_audit (
            venue TEXT, contract TEXT, quote TEXT, horizon_minutes INTEGER,
            reference_opened_at TEXT, settled_at TEXT, outcome_up INTEGER,
            asset TEXT)""")
        ref = NOW - timedelta(minutes=30)
        challenger.call(conn, venue="hyperliquid", contract="BTC", quote="USDC",
                        asset="BTC", horizon=5, reference_opened_at=ref,
                        issued_at=ref, features=(0.1,) * 10, flow=flow)
        conn.execute("INSERT INTO perp_forward_audit VALUES "
                     "('hyperliquid','BTC','USDC',5,?,?,1,'BTC')",
                     (ref.isoformat(), NOW.isoformat()))
    assert challenger.learn(NOW) == 1
    return LiquidityChallengerV1.artifact_identity(store, "BTC", 5)


# ---------- Track A: Gate slice converter ----------

GATE_HOUR = "2023042505"
GATE_FIRST = 1682398802.134


def _gate_source(spreads=(10.0, 12.0)):
    """Two Gate snapshots with engineered spreads (bps) at mid 100."""
    snapshots = []
    for idx, spread in enumerate(spreads):
        half = spread * 200 / 20000 / 2
        snapshots.append({
            "current": GATE_FIRST + idx * 300,
            "update": GATE_FIRST + idx * 300 - .003,
            "id": 36309008890 + idx,
            "asks": [{"p": str(100 + half), "s": 4}, {"p": "100.2", "s": 2}],
            "bids": [{"p": str(100 - half), "s": 3}, {"p": "99.8", "s": 6}],
        })
    return gzip.compress(
        ("".join(json.dumps(row) + "\n" for row in snapshots)).encode(),
        mtime=0,
    )


def _gate_original(tmp_path: Path, raw: bytes) -> tuple[Path, Path]:
    sha = hashlib.sha256(raw).hexdigest()
    name = f"gate-hourly-l2-BTC-orderbooks_slice-{GATE_HOUR}--{sha[:16]}.gz"
    original = tmp_path / name
    manifest = tmp_path / (name + ".manifest.json")
    original.write_bytes(raw)
    manifest.write_text(json.dumps({
        "schema_version": "rival-gate-hourly-l2-original-v1",
        "venue": "gate", "asset": "BTC", "contract": "BTC_USDT",
        "quote": "USDT", "family": "orderbooks_slice",
        "utc_hour": GATE_HOUR, "original_file": name,
        "original_sha256": sha, "model_training_eligible": False,
        "forward_model_promoted": False,
    }))
    return original, manifest


def test_gate_slice_converter_produces_only_spread_and_top5_sidecars(tmp_path):
    original, manifest = _gate_original(tmp_path, _gate_source())
    rows = convert_gate_slice(
        original.read_bytes(), json.loads(manifest.read_text()))
    # Only the two sampled buckets: the ten unsampled buckets are never filled.
    assert len(rows) == 2
    row = rows[0]
    assert row["venue"] == "gate" and row["asset"] == "BTC"
    assert row["source_family"] == "gate/orderbooks_slice"
    assert row["source_sha256"] == json.loads(manifest.read_text())["original_sha256"]
    assert row["converter_version"] == CONVERTER_VERSION
    assert row["model_training_eligible"] is False
    # The only live-shape quantities the slice family can carry: spread
    # (approximation: last snapshot, not the live mean) and the snapshot
    # count (exact). Everything else is unavailable, never zero-filled.
    assert row["labels"]["spread_bps_mean"] == "approximation"
    assert row["fields"]["spread_bps_mean"] == pytest.approx(10.0, abs=1e-6)
    assert row["labels"]["book_samples"] == "exact"
    assert row["fields"]["book_samples"] == 1
    assert row["labels"]["recorded_at"] == "exact"
    assert row["labels"]["last_event_at"] == "approximation"
    for field in ("spread_bps_max", "mid_last", "bid_depth_10bps_mean",
                  "ask_depth_10bps_mean", "bid_depth_25bps_mean",
                  "ask_depth_25bps_mean", "depth_25bps_covered_fraction",
                  "buy_trades", "sell_trades", "buy_volume", "sell_volume",
                  "first_event_at", "long_liq_volume", "short_liq_volume",
                  "long_liq_count", "short_liq_count"):
        assert row["labels"][field] == "unavailable", field
        assert row["fields"][field] is None, field
    # The live complete flag is unavailable: the sidecar reports only
    # whether all twelve buckets were sampled, which is not the live flag.
    assert row["labels"]["complete"] == "unavailable"
    assert row["fields"]["complete"] is None
    # Top-5 imbalance lives in extra: it has no live-bucket column.
    assert row["extra"]["l2_top5_imbalance"] == pytest.approx(3 / 15)
    assert row["extra"]["l2_snapshot_count"] == 1
    assert rows[1]["fields"]["spread_bps_mean"] == pytest.approx(12.0, abs=1e-6)


def test_gate_slice_converter_refuses_untrusted_source(tmp_path):
    original, manifest = _gate_original(tmp_path, _gate_source())
    with pytest.raises(HistoryImportError):
        convert_gate_slice(original.read_bytes(), {
            **json.loads(manifest.read_text()), "asset": "ETH",
        })


# ---------- Track A: OKX L2 replay converter ----------

OKX_BASE_MS = 1682398800000  # 2023-04-25T05:00:00Z


def _okx_snapshot(ts_ms, seq, bid=100.0, ask=100.1, bid_size=3.0, ask_size=4.0):
    return {"ts": ts_ms, "action": "snapshot", "seqId": seq, "prevSeqId": seq - 1,
            "bids": [[bid, bid_size], [bid - 0.1, 6.0]],
            "asks": [[ask, ask_size], [ask + 0.1, 2.0]]}


def _okx_update(ts_ms, seq, prev, bids=(), asks=()):
    return {"ts": ts_ms, "action": "update", "seqId": seq, "prevSeqId": prev,
            "bids": [[float(p), float(s)] for p, s in bids],
            "asks": [[float(p), float(s)] for p, s in asks]}


def test_okx_l2_replay_converter_uses_only_anchored_books(tmp_path):
    events = [
        _okx_snapshot(OKX_BASE_MS, 1),
        _okx_update(OKX_BASE_MS + 60_000, 2, 1, bids=((100.0, 5.0),)),
        _okx_snapshot(OKX_BASE_MS + 300_000, 3),
    ]
    rows = convert_okx_l2(events, asset="BTC", contract="BTC-USDT",
                          source_sha256="a" * 64, day_bytes=1024,
                          max_day_bytes=1024 * 1024)
    assert len(rows) == 2  # one row per bucket with a successful apply
    first, second = rows
    assert first["venue"] == "okx" and first["source_family"] == "okx/orderbook_l2_400"
    assert first["model_training_eligible"] is False
    # Snapshot spread: 20000 * 0.1 / 200.1 bps; top-5 imbalance (9-6)/15.
    assert first["labels"]["spread_bps_mean"] == "approximation"
    assert first["fields"]["spread_bps_mean"] == pytest.approx(20000 * 0.1 / 200.1)
    assert first["extra"]["l2_top5_imbalance"] == pytest.approx((11 - 6) / 17)
    assert first["labels"]["book_samples"] == "approximation"
    assert first["fields"]["book_samples"] == 2  # snapshot + linked update
    assert second["fields"]["book_samples"] == 1
    for field in ("bid_depth_10bps_mean", "buy_volume", "sell_volume",
                  "mid_last", "long_liq_volume"):
        assert first["labels"][field] == "unavailable", field
        assert first["fields"][field] is None, field

    # A delta stream with no anchor snapshot is unusable, not assumable.
    assert convert_okx_l2(
        [_okx_update(OKX_BASE_MS, 2, 1, bids=((100.0, 5.0),))],
        asset="BTC", contract="BTC-USDT", source_sha256="a" * 64,
        day_bytes=1024, max_day_bytes=1024 * 1024) == []
    # A gap invalidates the book until the next snapshot re-anchors it.
    gapped = [
        _okx_snapshot(OKX_BASE_MS, 1),
        _okx_update(OKX_BASE_MS + 60_000, 5, 4, bids=((100.0, 5.0),)),
        _okx_snapshot(OKX_BASE_MS + 300_000, 6),
    ]
    rows = convert_okx_l2(gapped, asset="BTC", contract="BTC-USDT",
                          source_sha256="a" * 64, day_bytes=1024,
                          max_day_bytes=1024 * 1024)
    assert len(rows) == 2  # the gapped update contributes nothing
    assert rows[0]["fields"]["book_samples"] == 1
    # Nonmonotonic timestamps fail the conversion outright.
    with pytest.raises(HistoryImportError):
        convert_okx_l2(
            [_okx_snapshot(OKX_BASE_MS + 300_000, 1),
             _okx_snapshot(OKX_BASE_MS, 2)],
            asset="BTC", contract="BTC-USDT", source_sha256="a" * 64,
            day_bytes=1024, max_day_bytes=1024 * 1024)


def test_okx_l2_day_size_guard_refuses_oversized_day_without_approval():
    events = [_okx_snapshot(OKX_BASE_MS, 1)]
    kwargs = {"asset": "BTC", "contract": "BTC-USDT", "source_sha256": "a" * 64}
    # The budget must be stated: a missing budget is an error, not a pass.
    with pytest.raises(HistoryImportError, match="byte budget must be stated"):
        convert_okx_l2(events, day_bytes=None, max_day_bytes=1024, **kwargs)
    # A day above budget converts only with explicit per-batch approval.
    with pytest.raises(HistoryImportError, match="above the .* byte per-day budget"):
        convert_okx_l2(events, day_bytes=478_630_000, max_day_bytes=100_000_000,
                       **kwargs)
    rows = convert_okx_l2(events, day_bytes=478_630_000,
                          max_day_bytes=100_000_000, approve_large_day=True,
                          **kwargs)
    assert len(rows) == 1


# ---------- Track A: exact tape buckets ----------

def test_okx_trades_converter_produces_exact_tape_buckets():
    rows = convert_okx_trades([
        {"timestamp": OKX_BASE_MS + 10_000, "side": "buy", "price": 100.0,
         "amount": 2.0},
        {"timestamp": OKX_BASE_MS + 20_000, "side": "buy", "price": 101.0,
         "amount": 3.0},
        {"timestamp": OKX_BASE_MS + 30_000, "side": "sell", "price": 100.5,
         "amount": 1.0},
        {"timestamp": OKX_BASE_MS + 300_000, "side": "sell", "price": 102.0,
         "amount": 4.0},
    ], asset="BTC", contract="BTC-USDT", source_sha256="b" * 64)
    assert len(rows) == 2
    first, second = rows
    assert first["source_family"] == "okx/trades"
    for field in ("buy_trades", "sell_trades", "buy_volume", "sell_volume"):
        assert first["labels"][field] == "exact", field
    assert (first["fields"]["buy_trades"], first["fields"]["sell_trades"]) == (2, 1)
    assert (first["fields"]["buy_volume"], first["fields"]["sell_volume"]) == (5.0, 1.0)
    assert (second["fields"]["buy_trades"], second["fields"]["sell_trades"]) == (0, 1)
    assert second["fields"]["sell_volume"] == 4.0
    # Tape-only: no spread, depth, mid, or liquidations -- and no live
    # complete flag from a trades-only archive.
    for field in ("spread_bps_mean", "bid_depth_10bps_mean", "mid_last",
                  "long_liq_volume", "complete"):
        assert first["labels"][field] == "unavailable", field
        assert first["fields"][field] is None, field
    with pytest.raises(HistoryImportError, match="missing"):
        convert_okx_trades([{"timestamp": OKX_BASE_MS, "side": "buy"}],
                           asset="BTC", contract="BTC-USDT",
                           source_sha256="b" * 64)
    with pytest.raises(HistoryImportError, match="bad side"):
        convert_okx_trades([{"timestamp": OKX_BASE_MS, "side": "hold",
                             "price": 1.0, "amount": 1.0}],
                           asset="BTC", contract="BTC-USDT",
                           source_sha256="b" * 64)


def test_tardis_trades_converter_partitions_by_local_timestamp():
    # local_timestamp is the arrival timestamp the vendor partitions by;
    # exchange clocks can differ, so timestamp is never used for buckets.
    rows = convert_tardis_trades([
        {"exchange": "bitget", "symbol": "BTCUSDT",
         "timestamp": OKX_BASE_MS + 600_000, "local_timestamp": OKX_BASE_MS + 10_000,
         "id": "1", "side": "buy", "price": 100.0, "amount": 2.0},
        {"exchange": "bitget", "symbol": "BTCUSDT",
         "timestamp": OKX_BASE_MS + 610_000, "local_timestamp": OKX_BASE_MS + 300_000,
         "id": "2", "side": "sell", "price": 101.0, "amount": 1.0},
    ], asset="BTC", contract="BTCUSDT", source_sha256="c" * 64)
    assert len(rows) == 2
    assert rows[0]["bucket_start"] == "2023-04-25T05:00:00+00:00"
    assert rows[1]["bucket_start"] == "2023-04-25T05:05:00+00:00"
    assert rows[0]["fields"]["buy_volume"] == 2.0
    # Non-sample days are simply absent from the input: no rows invented.
    assert rows[0]["source_family"] == "tardis/trades"
    with pytest.raises(HistoryImportError, match="not Bitget"):
        convert_tardis_trades([
            {"exchange": "binance", "symbol": "BTCUSDT",
             "timestamp": OKX_BASE_MS, "local_timestamp": OKX_BASE_MS,
             "id": "1", "side": "buy", "price": 100.0, "amount": 1.0}],
            asset="BTC", contract="BTCUSDT", source_sha256="c" * 64)


# ---------- Track A: sidecar IO + overlap report ----------

def test_converted_sidecar_is_immutable_and_rehash_verified(tmp_path):
    original, manifest = _gate_original(tmp_path, _gate_source())
    rows = convert_gate_slice(
        original.read_bytes(), json.loads(manifest.read_text()))
    sidecar, receipt = write_converted_sidecar(
        rows, source_family="gate/orderbooks_slice",
        source_sha256=json.loads(manifest.read_text())["original_sha256"],
        out_dir=tmp_path / "out", name="gate-test")
    assert len(load_converted_sidecar(receipt)) == 2
    proof = json.loads(receipt.read_text())
    assert proof["derived_bucket_count"] == 2
    assert proof["model_training_eligible"] is False
    assert proof["live_model_import_allowed"] is False
    assert proof["converter_version"] == CONVERTER_VERSION
    assert write_converted_sidecar(
        rows, source_family="gate/orderbooks_slice",
        source_sha256=json.loads(manifest.read_text())["original_sha256"],
        out_dir=tmp_path / "out", name="gate-test") == (sidecar, receipt)
    sidecar.write_text(sidecar.read_text() + "{}\n")
    with pytest.raises(HistoryImportError, match="verification failed"):
        load_converted_sidecar(receipt)


def _live_row(venue, bucket, spread=None, buy_volume=None, sell_volume=None,
              buy_trades=None, sell_trades=None):
    return {"venue": venue, "asset": "BTC", "bucket_start": bucket,
            "spread_bps_mean": spread, "buy_volume": buy_volume,
            "sell_volume": sell_volume, "buy_trades": buy_trades,
            "sell_trades": sell_trades}


def test_overlap_report_measures_exact_and_approximation_fields(tmp_path):
    # Exact tape: converted OKX trades vs live OKX buckets, same venue.
    converted = convert_okx_trades([
        {"timestamp": OKX_BASE_MS + 10_000, "side": "buy", "price": 100.0,
         "amount": 2.0},
        {"timestamp": OKX_BASE_MS + 20_000, "side": "sell", "price": 100.5,
         "amount": 1.0},
        {"timestamp": OKX_BASE_MS + 300_000, "side": "buy", "price": 101.0,
         "amount": 3.0},
    ], asset="BTC", contract="BTC-USDT", source_sha256="b" * 64)
    live = [
        _live_row("okx", "2023-04-25T05:00:00+00:00", buy_volume=2.0, sell_volume=1.0,
                  buy_trades=1, sell_trades=1),
        _live_row("okx", "2023-04-25T05:05:00+00:00", buy_volume=3.0, sell_volume=0.0,
                  buy_trades=1, sell_trades=0),
    ]
    report = overlap_report(live, converted, exact_max_mean_abs_diff=1e-9,
                            approx_min_correlation=0.8)
    assert report["passes"] is True
    assert report["fields"]["buy_volume"]["kind"] == "exact"
    assert report["fields"]["buy_volume"]["overlapping_buckets"] == 2
    assert report["fields"]["buy_volume"]["mean_difference"] == 0.0
    assert report["fields"]["buy_volume"]["exact_agreement_fraction"] == 1.0
    assert report["thresholds"]["exact_max_mean_abs_diff"] == 1e-9
    # A live tape that disagrees fails the exact bar.
    live[0]["buy_volume"] = 2.5
    report = overlap_report(live, converted, exact_max_mean_abs_diff=1e-9,
                            approx_min_correlation=0.8)
    assert report["passes"] is False
    assert report["fields"]["buy_volume"]["mean_abs_difference"] == pytest.approx(0.25)
    assert report["fields"]["buy_volume"]["passes"] is False

    # Approximation spread: converted Gate rows vs live hyperliquid spread
    # for the same asset (Gate has no live recorder of its own), checked
    # for bias/correlation, never equality.
    original, manifest = _gate_original(tmp_path, _gate_source())
    gate_rows = convert_gate_slice(
        original.read_bytes(), json.loads(manifest.read_text()))
    live_hl = [
        _live_row("hyperliquid", "2023-04-25T05:00:00+00:00", spread=10.5),
        _live_row("hyperliquid", "2023-04-25T05:05:00+00:00", spread=12.5),
    ]
    report = overlap_report(live_hl, gate_rows, exact_max_mean_abs_diff=1e-9,
                            approx_min_correlation=0.8,
                            reference_venue="hyperliquid")
    assert report["passes"] is True
    spread = report["fields"]["spread_bps_mean"]
    assert spread["kind"] == "approximation"
    assert spread["overlapping_buckets"] == 2
    assert spread["mean_difference"] == pytest.approx(-0.5)
    assert spread["correlation"] == pytest.approx(1.0)
    assert report["live_reference_venue"] == "hyperliquid"
    # Exact fields are not evaluated under a reference join.
    assert report["fields"]["book_samples"]["passes"] is None
    assert "same-venue overlap" in report["fields"]["book_samples"]["reason"]
    # No overlap at all: the field fails, it is not silently passed.
    report = overlap_report([], gate_rows, exact_max_mean_abs_diff=1e-9,
                            approx_min_correlation=0.8,
                            reference_venue="hyperliquid")
    assert report["passes"] is False
    assert report["fields"]["spread_bps_mean"]["overlapping_buckets"] == 0


def test_converter_cli_gate_slice_and_overlap_report(tmp_path):
    raw = _gate_source()
    original, manifest = _gate_original(tmp_path, raw)
    out_dir = tmp_path / "converted"
    assert converter_main(["gate-slice", "--raw", str(original),
                           "--manifest", str(manifest),
                           "--out-dir", str(out_dir)]) == 0
    receipt = out_dir / (original.name.removesuffix(".gz") + ".research.receipt.json")
    assert receipt.exists()
    live_path = tmp_path / "live.jsonl"
    live_path.write_text(
        '{"venue": "hyperliquid", "asset": "BTC", '
        '"bucket_start": "2023-04-25T05:00:00+00:00", "spread_bps_mean": 10.5}\n'
        '{"venue": "hyperliquid", "asset": "BTC", '
        '"bucket_start": "2023-04-25T05:05:00+00:00", "spread_bps_mean": 12.5}\n')
    out = tmp_path / "report.json"
    assert converter_main(["overlap-report", "--live", str(live_path),
                           "--receipt", str(receipt),
                           "--exact-max-mean-abs-diff", "0.000000001",
                           "--approx-min-correlation", "0.8",
                           "--reference-venue", "hyperliquid",
                           "--out", str(out)]) == 0
    assert json.loads(out.read_text())["passes"] is True

# ---------- Track B: liq_v1 identity ----------

def test_liq_v1_identity_is_37_inputs_with_spot_labelled():
    assert LIQ_FAMILY == "liq_v1"
    assert LIQ_CONTRACT == "liq-price10+flow21+flags6-v1"
    assert len(FLOW_FEATURE_NAMES) == 21
    assert len(FLAG_NAMES) == 6
    assert FLAG_NAMES == ("binance_bundle", "hl_context", "hl_liquidity",
                          "okx", "coinbase", "binance_buckets")
    assert N_FLOW == 27  # 21 raw + 6 flags; +10 price = 37 inputs (+bias = 38)
    # The Binance bucket group is new (no model read Binance buckets at 0cee87c).
    assert "bin_depth_imb_10bps" in FLOW_FEATURE_NAMES
    assert "bin_depth_imb_25bps" in FLOW_FEATURE_NAMES
    assert "bin_tape_imb_5m" in FLOW_FEATURE_NAMES
    assert "bin_tape_imb_15m" in FLOW_FEATURE_NAMES
    # Coinbase tape is spot: the label is in the feature name itself.
    assert "cb_spot_tape_imb_5m" in FLOW_FEATURE_NAMES
    assert "cb_spot_tape_imb_15m" in FLOW_FEATURE_NAMES
    assert "coinbase_tape_imb_5m" not in FLOW_FEATURE_NAMES
    # Shadow prefix: the family is graded and settled by the generic
    # challenger paths, with no per-family wiring needed there.
    assert LIQ_FAMILY.startswith(CHALLENGER_PREFIXES)


def test_liq_v1_inputs_include_binance_buckets_and_spot_label(tmp_path):
    store = _store(tmp_path)
    try:
        bundle = _Bundle(
            funding=[(NOW - timedelta(minutes=10), 0.0001)],
            oi=[(NOW - timedelta(minutes=60), 100.0),
                (NOW - timedelta(minutes=5), 110.0)],
        )
        with store.connect() as conn:
            start = NOW - timedelta(minutes=10)
            _bucket(conn, "hyperliquid", start, NOW - timedelta(minutes=4), buy=9, sell=1)
            _bucket(conn, "okx", start, NOW - timedelta(minutes=4), buy=3, sell=1,
                    bid=2.0, ask=2.0, long_liq=30.0, short_liq=10.0)
            _bucket(conn, "coinbase", start, NOW - timedelta(minutes=4), buy=1, sell=3,
                    bid=2.0, ask=2.0)
            _bucket(conn, "binance", start, NOW - timedelta(minutes=4), buy=7, sell=3,
                    bid=4.0, ask=6.0)
            _context(conn, NOW - timedelta(minutes=60), funding=0.0002, oi=1000.0)
            _context(conn, NOW - timedelta(minutes=2), funding=0.0002, oi=1100.0)
        vector = LiquidityChallengerV1(store, binance_context=bundle).asset_inputs(
            NOW, {"BTC"})["BTC"]
        assert len(vector) == N_FLOW
        (bin_fund, hl_fund, bin_oi, hl_oi, hl_d10, hl_d25, okx_d10, okx_d25,
         bin_d10, bin_d25, hl_t5, hl_t15, okx_t5, okx_t15, bin_t5, bin_t15,
         liq_net, liq_share, hl_spread, cb_t5, cb_t15, *flags) = vector
        assert bin_fund == 1.0  # 0.0001 * 1e4 bps
        assert hl_fund == 2.0
        assert abs(bin_oi - 0.1) < 1e-9
        assert hl_oi == math.log(1.1)
        assert hl_d10 == hl_d25 == (10 - 5) / 15
        assert okx_d10 == okx_d25 == 0.0  # balanced 2 vs 2 books
        assert bin_d10 == bin_d25 == (4 - 6) / 10  # Binance depth, new group
        assert hl_t5 == hl_t15 == 0.8 and okx_t5 == okx_t15 == 0.5
        assert bin_t5 == bin_t15 == 0.4  # (7-3)/10, Binance tape, new group
        assert cb_t5 == cb_t15 == -0.5  # Coinbase SPOT tape
        assert liq_net == 0.5  # (30-10)/40, OKX-only
        assert liq_share == 10.0  # 40 liq volume / 4 trade volume
        assert hl_spread == 1.0
        assert flags == [1.0] * 6  # every source available, incl. Binance buckets
        # No data at all: the asset is skipped, not issued an all-zero vector.
        assert LiquidityChallengerV1(store).asset_inputs(
            NOW, {"ETH"}) == {}
    finally:
        store.close()


def test_liq_v1_call_refuses_all_zero_flags_and_persists_exact_inputs(tmp_path):
    store = _store(tmp_path)
    try:
        challenger = LiquidityChallengerV1(store)
        with store.connect() as conn:
            assert challenger.call(
                conn, venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5, reference_opened_at=NOW,
                issued_at=NOW, features=(0.1,) * 10,
                flow=(0.0,) * 21 + (0.0,) * 6) is None
            call = challenger.call(
                conn, venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5, reference_opened_at=NOW,
                issued_at=NOW, features=(0.1,) * 10,
                flow=(0.5,) + (0.0,) * 20 + (1.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            assert call is not None and call["family"] == LIQ_FAMILY
            assert call["training_rows"] == 0
            row = conn.execute(
                "SELECT features, raw_features FROM rival_liq1_features"
            ).fetchone()
            # Exact inputs persisted: 10 price features, then the 21 raw
            # flow features (flags are stored alongside, not in raw).
            assert json.loads(row["raw_features"]) == (
                [0.1] * 10 + [0.5] + [0.0] * 20)
            assert json.loads(row["features"])[:10] == [0.1] * 10
    finally:
        store.close()


def test_liq_v1_young_model_stays_near_the_up_rate(tmp_path):
    """A young family stays near the up-rate: trust comes from its OWN
    graded count, which starts at zero."""
    store = _store(tmp_path)
    try:
        challenger = LiquidityChallengerV1(store)
        with store.connect() as conn:
            state = challenger._state(conn, "BTC", 5)
        assert state["updates"] == 0
        assert LiquidityChallengerV1.probability(
            state, (0.0,) * 37) == 0.5  # base 5/10, trust 0/(0+300) = 0
        # One graded update earns 1/301 of a say, not half: the trust is
        # n / (n + 300) with n = this family's OWN graded count.
        state["updates"] = 1
        state["positives"] = 1
        state["weights"] = [0.0] * 38
        state["weights"][1] = 2.0  # first input's weight
        inputs = (1.0,) + (0.0,) * 36
        raw = 1 / (1 + math.exp(-2.0))
        base = 6 / 11  # (positives + 5) / (updates + 10)
        expected = base + (1 / 301) * (raw - base)
        assert LiquidityChallengerV1.probability(
            state, inputs) == pytest.approx(expected, abs=1e-9)
    finally:
        store.close()


# ---------- Track B: kill switch + settlement ----------

def test_liq_v1_kill_switch_blocks_issuance_in_cycle(tmp_path, monkeypatch):
    """The kill switch stops new liq_v1 issuance; the incumbent still runs.

    Control first: with the flag on, the cycle issues liq_v1 forecasts
    (features rows + shadow rows). With the flag off -- its production
    value when Felix kills it -- the same cycle issues nothing new for
    liq_v1, while the incumbent cycle still runs.
    """
    assert forward_audit.ISSUE_LIQ_V1_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "liq-kill.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        LiquidityChallengerV1(store).initialize()
        _seed_cycle_candles(market, now=now)
        with store.connect() as conn:
            _bucket(conn, "hyperliquid", now - timedelta(minutes=10),
                    now - timedelta(minutes=4), buy=9, sell=1)
            _context(conn, now - timedelta(minutes=2), funding=0.0002, oi=1100.0)
            _context(conn, now - timedelta(minutes=60), funding=0.0002, oi=1000.0)

        def _liq1_counts():
            with store.connect() as conn:
                features = conn.execute(
                    "SELECT COUNT(*) AS n FROM rival_liq1_features"
                ).fetchone()["n"]
                shadow = conn.execute(
                    "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
                    "WHERE family='liq_v1'"
                ).fetchone()["n"]
            return features, shadow

        monitor.run_once(now=now)
        features_on, shadow_on = _liq1_counts()
        assert features_on >= 1  # the control proves this cycle can issue liq_v1
        assert shadow_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_LIQ_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _liq1_counts() == (features_on, shadow_on)
    finally:
        store.close()


def test_liq_v1_rows_settle_in_shadow(tmp_path):
    """Already-issued liq_v1 rows keep settling after the switch flips.

    Settlement is flag-independent: ForwardAccuracyMonitor._settle grades
    every pending perp_forward_audit row, and ShadowModelCompetition.settle
    grades every shadow row from its verified incumbent outcome. Neither
    path reads ISSUE_LIQ_V1_CHALLENGERS.
    """
    start = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    now = start + timedelta(hours=2)
    target = start + timedelta(minutes=60)
    ref = target - timedelta(minutes=5)
    issued = target - timedelta(seconds=40)
    store = SQLiteHotStore(tmp_path / "liq-settle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        market.initialize()
        monitor.initialize()
        challenger = LiquidityChallengerV1(store)
        challenger.initialize()
        flow = (0.5,) + (0.0,) * 20 + (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        with store.connect() as conn:
            call = challenger.call(
                conn, venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5, reference_opened_at=ref, issued_at=issued,
                features=(0.1,) * 10, flow=flow)
            assert call is not None and call["family"] == LIQ_FAMILY
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
                 ref.isoformat(), 100.0, issued.isoformat(), target.isoformat(),
                 0.7, 0.5, 50))
            closed = target + timedelta(minutes=5) - timedelta(milliseconds=1)
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", 5, target.isoformat(),
                 closed.isoformat(), (target + timedelta(minutes=6)).isoformat(),
                 101.0, 102.0, 100.0, 101.0, 10, 20))
            ShadowModelCompetition(store).initialize()
            conn.execute(
                """INSERT INTO rival_model_shadow_forecasts
                   (venue,contract,quote,asset,horizon_minutes,family,
                    reference_opened_at,issued_at,target_opened_at,
                    probability_up,incumbent_up,baseline_up,
                    training_count,training_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, "liq_v1",
                 ref.isoformat(), issued.isoformat(), target.isoformat(),
                 0.7, 0.7, 0.5, 1, "a" * 64))
        assert monitor._settle(now) >= 1
        with store.connect() as conn:
            row = conn.execute(
                "SELECT settled_at, outcome_up FROM perp_forward_audit"
            ).fetchone()
        assert row["settled_at"] is not None and row["outcome_up"] == 1
        assert ShadowModelCompetition(store).settle(now=now) >= 1
        with store.connect() as conn:
            shadow = conn.execute(
                "SELECT settled_at, outcome_up FROM rival_model_shadow_forecasts "
                "WHERE family='liq_v1'"
            ).fetchone()
        assert shadow["settled_at"] is not None and shadow["outcome_up"] == 1
    finally:
        store.close()


def test_liq_v1_calibration_refit_reads_only_the_newest_rows(tmp_path, monkeypatch):
    """The Platt refit fetches only the newest MAX_CALIBRATION_ROWS graded rows.

    It used to read the whole, ever-growing graded history on every cycle and
    then keep the newest rows in Python (slow SELECT holding a pool
    connection on the live host). The fit must be identical to the old
    full-history-then-slice result.
    """
    from parlay_bot.perpetuals import liquidity_challenger_v1 as liq

    monkeypatch.setattr(liq, "MAX_CALIBRATION_ROWS", 80)
    store = _store(tmp_path)
    challenger = LiquidityChallengerV1(store)
    try:
        with store.connect() as conn:
            for i in range(130):
                ref = NOW - timedelta(minutes=5 * (200 - i))
                flow = ((i % 7) / 7.0,) + (0.0,) * 20 + (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                challenger.call(conn, venue="hyperliquid", contract="BTC", quote="USDC",
                                asset="BTC", horizon=5, reference_opened_at=ref,
                                issued_at=ref, features=((i % 5) / 5.0,) * 10, flow=flow)
                conn.execute("INSERT INTO perp_forward_audit VALUES "
                             "('hyperliquid','BTC','USDC',5,?,?,?,'BTC')",
                             (ref.isoformat(), (ref + timedelta(minutes=5)).isoformat(),
                              int(i % 3 == 0)))
        assert challenger.learn(NOW) == 130
        with store.connect() as conn:
            state = challenger._state(conn, "BTC", 5)
            # Reference: the previous full-history query, sliced in Python.
            full = conn.execute(
                """SELECT x.features, f.outcome_up FROM rival_liq1_features x
                   JOIN perp_forward_audit f
                     ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                    AND f.horizon_minutes=x.horizon_minutes
                    AND f.reference_opened_at=x.reference_opened_at
                   WHERE x.asset='BTC' AND x.horizon_minutes=5 AND x.learned=1
                     AND f.settled_at IS NOT NULL AND f.outcome_up IN (0,1)
                   ORDER BY f.settled_at, x.issued_at""").fetchall()[-80:]
            split = int(len(full) * 0.7)
            expected = liq.fit_platt(
                [(challenger.raw_probability(state, json.loads(r["features"])),
                  int(r["outcome_up"])) for r in full[:split]],
                [(challenger.raw_probability(state, json.loads(r["features"])),
                  int(r["outcome_up"])) for r in full[split:]])

            fetched: list[int] = []

            class Counting:
                def __init__(self, raw):
                    self.raw = raw

                def execute(self, sql, params=()):
                    cursor = self.raw.execute(sql, params)
                    if "SELECT features, outcome_up" in " ".join(sql.split()) or (
                            "x.learned=1" in sql):
                        rows = cursor.fetchall()
                        fetched.append(len(rows))

                        class Rows:
                            def fetchall(self):
                                return rows
                        return Rows()
                    return cursor

            challenger._refit_calibration(Counting(conn), ("BTC", 5), state, NOW.isoformat())
        assert fetched == [80]
        result = dict(state["calibration"])
        assert result["fit_window"] == {"graded_rows": 56}
        assert result["eval_window"] == {"graded_rows": 24}
        result.pop("fit_window"), result.pop("eval_window")
        assert result == expected
    finally:
        store.close()


class _Capture:
    """Connection wrapper that records the refit SELECT and its rows."""

    def __init__(self, raw, captured: list):
        self.raw, self.captured = raw, captured

    def execute(self, sql, params=()):
        cursor = self.raw.execute(sql, params)
        if "SELECT features, outcome_up" not in " ".join(sql.split()):
            return cursor
        rows = cursor.fetchall()
        self.captured.append((sql, params, rows))

        class Rows:
            def fetchall(self):
                return rows
        return Rows()


def test_liq_v1_refit_scope_keeps_rows_and_uses_the_scope_index(tmp_path, monkeypatch):
    """The refit's (asset, horizon) scope on the audit side changes no rows.

    Two assets x two horizons on several venues: for every key the scoped
    query returns exactly the rows of the previous unscoped query, and the
    feature side has idx_rival_liq1_features_scope so Postgres need not scan
    every stored feature row once per key.
    """
    from parlay_bot.perpetuals import liquidity_challenger_v1 as liq

    monkeypatch.setattr(liq, "MAX_CALIBRATION_ROWS", 40)
    store = _store(tmp_path)
    challenger = LiquidityChallengerV1(store)
    flow = (0.3,) + (0.0,) * 20 + (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    keys = [(a, h) for a in ("BTC", "ETH") for h in (5, 15)]
    try:
        with store.connect() as conn:
            for i in range(60):
                for venue in ("hyperliquid", "okx"):
                    for asset, horizon in keys:
                        ref = NOW - timedelta(minutes=5 * (200 - i))
                        challenger.call(conn, venue=venue, contract=asset, quote="USDC",
                                        asset=asset, horizon=horizon,
                                        reference_opened_at=ref, issued_at=ref,
                                        features=((i % 4) / 4.0,) * 10, flow=flow)
                        conn.execute(
                            "INSERT INTO perp_forward_audit VALUES (?,?,?,?,?,?,?,?)",
                            (venue, asset, "USDC", horizon, ref.isoformat(),
                             (ref + timedelta(minutes=horizon, seconds=i % 3)).isoformat(),
                             int((i + horizon) % 3 == 0), asset))
        assert challenger.learn(NOW) == 60 * 2 * len(keys)
        with store.connect() as conn:
            for asset, horizon in keys:
                old = conn.execute(
                    """SELECT x.features, f.outcome_up FROM rival_liq1_features x
                       JOIN perp_forward_audit f
                         ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                        AND f.horizon_minutes=x.horizon_minutes
                        AND f.reference_opened_at=x.reference_opened_at
                       WHERE x.asset=? AND x.horizon_minutes=? AND x.learned=1
                         AND f.settled_at IS NOT NULL AND f.outcome_up IN (0,1)
                       ORDER BY f.settled_at, x.issued_at, x.venue, x.contract, x.quote""",
                    (asset, horizon)).fetchall()[-40:]
                captured: list = []
                state = challenger._state(conn, asset, horizon)
                challenger._refit_calibration(_Capture(conn, captured), (asset, horizon),
                                              state, NOW.isoformat())
                ((sql, params, rows),) = captured
                assert [tuple(r) for r in rows] == [tuple(r) for r in old]
                assert "f.asset=? AND f.horizon_minutes=?" in " ".join(sql.split())
                assert params == (asset, horizon, asset, horizon, 40)
            columns = [r["name"] for r in conn.execute(
                "PRAGMA index_info(idx_rival_liq1_features_scope)").fetchall()]
        assert columns == ["asset", "horizon_minutes", "learned"]
    finally:
        store.close()


# ---------- Track B: promotion continuity + receipts + OTG ----------

def test_liq_v1_selected_continuity_check_and_blocked_families(tmp_path):
    store = _store(tmp_path)
    try:
        artifact = _seed_liq1_state(store)
        assert artifact is not None and artifact["feature_digest"] == LIQ_DIGEST
        _promotion_row(store, family="liq_v1", evaluated_at=NOW,
                       artifact=artifact)
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                  "quote": "USDC", "horizon": 5, "now": NOW}
        assert promoter.selected(**kwargs) == "liq_v1"
        # Kill switch 2: blocked even with a fresh promoted_paper row.
        BLOCKED_FAMILIES.add("liq_v1")
        try:
            assert promoter.selected(**kwargs) is None
        finally:
            BLOCKED_FAMILIES.discard("liq_v1")
        assert promoter.selected(**kwargs) == "liq_v1"
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="liq_v1",
                       evaluated_at=NOW - timedelta(hours=49), artifact=artifact)
        assert promoter.selected(**kwargs) is None
        _promotion_row(store, family="liq_v1", evaluated_at=NOW,
                       artifact=artifact)
        # A mismatched weights SHA with no further graded learning: not the
        # artifact that earned promotion.
        tampered = dict(artifact, weights_sha256="0" * 64)
        _promotion_row(store, family="liq_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # A refit/reset (state replaced, training cursor gone) never serves
        # on the name alone.
        _promotion_row(store, family="liq_v1", evaluated_at=NOW,
                       artifact=artifact)
        with store.connect() as conn:
            conn.execute(
                "UPDATE rival_liq1_model_state SET weights=?, updates=0, "
                "positives=0, graded_count=0, trained_through=NULL, "
                "weights_sha256=? WHERE asset='BTC' AND horizon_minutes=5",
                (json.dumps([0.0] * 38), "1" * 64))
        assert promoter.selected(**kwargs) is None
    finally:
        store.close()


def test_liq_v1_promotion_receipt_discloses_spot_and_liquidations(tmp_path):
    store = SQLiteHotStore(tmp_path / "liq-receipts.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS perp_forward_audit "
                         "(asset TEXT, horizon_minutes INTEGER, issued_at TEXT)")
            conn.execute("INSERT INTO perp_forward_audit VALUES ('BTC', 5, ?)",
                         ((NOW - timedelta(minutes=3)).isoformat(),))
            conn.execute("""CREATE TABLE rival_pooled_models (trained_through TEXT,
                            gates_passed INTEGER)""")
            conn.execute("INSERT INTO rival_pooled_models VALUES (?,1)",
                         (str(NOW - timedelta(days=2)).replace("T", " "),))
            conn.execute("CREATE TABLE rival_intelligence_calls (issued_at TEXT)")
            conn.execute("INSERT INTO rival_intelligence_calls VALUES (?)",
                         ((NOW - timedelta(minutes=10)).isoformat(),))
            conn.execute("""CREATE TABLE IF NOT EXISTS rival_model_shadow_forecasts (
                            family TEXT, horizon_minutes INTEGER, settled_at TEXT,
                            model_brier REAL, baseline_brier REAL, venue TEXT,
                            training_sha256 TEXT, issued_at TEXT, target_opened_at TEXT)""")
        evidence = {"targets": 180, "days": 22, "challenger_trades": 60,
                    "quote_coverage": 0.9, "ece": 0.05,
                    "cost_proof": {
                        "conservative": {"paired_gain_bps": 3.0},
                        "severe": {"paired_gain_bps": 1.5}}}
        calibration = {"status": "evaluated", "ece": 0.05,
                       "top_bucket_hit_rate": 0.8, "top_bucket_n": 40}
        receipt = record_promotion_receipt(
            store, family="liq_v1", venue="hyperliquid", contract="BTC",
            quote="USDC", horizon=5, kind="promoted", evidence=evidence,
            calibration=calibration, now=NOW)
        assert receipt is not None
        assert "targets=180" in receipt["receipt_text"]
        assert "severe_paired_gain_bps=1.5" in receipt["receipt_text"]
        assert "OKX-only coverage" in receipt["receipt_text"]
        assert "SPOT tape" in receipt["receipt_text"]
        # Second recording of the same first event: no duplicate receipt.
        assert record_promotion_receipt(
            store, family="liq_v1", venue="hyperliquid", contract="BTC",
            quote="USDC", horizon=5, kind="promoted", evidence=evidence,
            calibration=calibration, now=NOW) is None
        keys = {key for key, _ in research_alerts(store, now=NOW)}
        assert "liq-v1:promoted:hyperliquid:BTC:5m" in keys
    finally:
        store.close()


def test_liq_v1_otg_call_only_no_live_lean():
    """D11 rule for liq_v1: the OTG call shows, the live lean is None."""
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     contract="BTC", quote="USDC",
                     model_version="liq_v1") is None
    watch = {"probability_up": 0.7, "asset": "BTC", "venue": "hyperliquid",
             "horizon_minutes": 5, "issued": NOW.isoformat(),
             "target": (NOW + timedelta(minutes=5)).isoformat(),
             "model_version": "liq_v1"}
    snapshot = otg_snapshot(watch)
    assert snapshot["direction"] == "UP"
    assert snapshot["confidence_pct"] == 70
    assert snapshot["model_version"] == "liq_v1"


# ---------- Track C: Binance tape verdict (include, flagged) ----------

def test_track_c_verdict_recorded_include_flagged_with_context_absent_gate():
    """D14: the Binance tape group is IN v1 as a flagged group, and the
    context-absent evaluation is a gate on the artifact.

    The verdict facts are pinned here. The enforcement -- an artifact
    whose context-absent gain is <= 0.005 fails the gates and is not
    serving-capable -- is proven by
    tests/test_archive_model.py::test_context_absent_failure_blocks_serving_capable_artifact
    (Phase 1), which runs in the same suite.
    """
    assert archive_features.FEATURES[28:32] == (
        "taker_imb", "taker_imb_12", "trades_ratio", "avg_trade_size")
    assert archive_features.GROUPS["binance_tape"] == (28, 29, 30, 31)
    assert "binance_tape" in archive_features.FLAG_NAMES
    assert archive_features.BINANCE_TAPE_SCOPE == (
        "binance-per-asset flow as market-wide context")
    assert archive_training.MIN_CONTEXT_ABSENT_GAIN == 0.005
    assert archive_training.GATES["min_test_gain"] == 0.005
    # The group is Binance-per-asset flow applied as market-wide context,
    # not that venue's own tape: the contract states the scope.
    assert "market-wide context" in archive_features.BINANCE_TAPE_SCOPE
