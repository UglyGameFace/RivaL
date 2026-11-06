"""The provider-hash field must hold the provider bytes' hash, never the
normalized NDJSON hash (round-5 N4). Pre-fix rows carried the normalized
hash under the same field name; a Postgres-ledger month has no provider
original at all and must say so instead of being relabelled."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.research.free_history import _persist
from parlay_bot.research.learning import (
    assemble,
    evaluate,
    provider_source_sha256,
)


def _bars(start: datetime, count: int) -> list[dict]:
    rows, previous = [], 100.0
    for i in range(count):
        close = 100 * math.exp(
            0.004 * math.sin(i * .19) + 0.003 * math.sin(i * .031)
            + 0.0003 * math.sin(i * .007)
        )
        opened = start + timedelta(minutes=5 * i)
        rows.append({
            "opened_at": opened.isoformat(),
            "closed_at": (opened + timedelta(minutes=5, milliseconds=-1)).isoformat(),
            "open": previous, "high": max(previous, close) * 1.000025,
            "low": min(previous, close) * 0.999975, "close": close,
            "volume_base": 5.0 + (i % 11), "trades": 10,
        })
        previous = close
    return rows


def _binance_manifest(tmp_path, rows, *, month, raw_sha):
    return _persist(
        source="binance_um_klines", key=f"BTCUSDT-5m-{month}",
        records=rows, raw_sha=raw_sha,
        url="https://data.binance.vision/example-fixture-only.zip",
        out_dir=tmp_path,
        extra={
            "venue": "binance", "contract": "BTCUSDT", "quote": "USDT",
            "interval_minutes": 5,
            "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
        },
    ).manifest_path


def test_assemble_records_provider_hash_not_normalized_hash(tmp_path):
    provider_sha = "a" * 64  # stands in for the provider ZIP's hash
    manifest_path = _binance_manifest(
        tmp_path, _bars(datetime(2026, 4, 1, tzinfo=UTC), 12),
        month="2026-04", raw_sha=provider_sha,
    )
    manifest = json.loads(manifest_path.read_text())
    normalized_sha = manifest["data_sha256"]
    assert normalized_sha != provider_sha
    _identity, _candles, provenance = assemble([manifest_path])
    assert provenance["source_sha256_by_month"] == {"2026-04": provider_sha}
    assert provenance["provider_sha256_by_month"] == {"2026-04": provider_sha}
    assert provenance["months_without_provider_hash"] == []
    assert provenance["source_sha256_by_month"]["2026-04"] != normalized_sha


def test_assemble_marks_postgres_ledger_month_without_provider_hash(tmp_path):
    ledger_digest = "b" * 64  # hash of ledger-derived records, not provider bytes
    rows = []
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for i, bar in enumerate(_bars(start, 12)):
        opened = start + timedelta(minutes=5 * i)
        rows.append({
            **bar,
            "venue": "hyperliquid", "contract": "BTC", "quote": "USDC",
            "interval_minutes": 5,
            "first_observed_at": (
                opened + timedelta(minutes=5, milliseconds=-1)
            ).isoformat(),
        })
    manifest_path = _persist(
        source="verified_venue_perp_klines",
        key="hyperliquid-BTC-USDC-5m-2026-09",
        records=rows, raw_sha=ledger_digest,
        url="RivaL verified private venue ledger",
        out_dir=tmp_path,
        extra={
            "venue": "hyperliquid", "contract": "BTC", "quote": "USDC",
            "interval_minutes": 5, "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
            "source_integrity": "postgres-first-observed-at-v1",
        },
    ).manifest_path
    _identity, _candles, provenance = assemble([manifest_path])
    # The legacy field keeps the manifest's declared source hash, but the
    # month is explicitly listed as having no provider original.
    assert provenance["source_sha256_by_month"] == {"2026-09": ledger_digest}
    assert provenance["provider_sha256_by_month"] == {}
    assert provenance["months_without_provider_hash"] == ["2026-09"]


def test_provider_source_sha256_rules():
    assert provider_source_sha256({"source_sha256": "c" * 64}) == "c" * 64
    assert provider_source_sha256({
        "source_sha256": "c" * 64,
        "source_integrity": "postgres-first-observed-at-v1",
    }) is None
    assert provider_source_sha256({}) is None
    assert provider_source_sha256({"source_sha256": "not-a-hash"}) is None


def test_evaluate_carries_provider_provenance_in_model_spec(tmp_path):
    provider_sha = "a" * 64
    april = _binance_manifest(
        tmp_path, _bars(datetime(2026, 4, 1, tzinfo=UTC), 30 * 24 * 12),
        month="2026-04", raw_sha=provider_sha,
    )
    may = _binance_manifest(
        tmp_path, _bars(datetime(2026, 5, 1, tzinfo=UTC), 31 * 24 * 12),
        month="2026-05", raw_sha=provider_sha,
    )
    result = evaluate([april, may], horizon_minutes=60)
    spec = result["model_spec"]
    assert spec["source_sha256_by_month"] == {
        "2026-04": provider_sha, "2026-05": provider_sha,
    }
    assert spec["provider_sha256_by_month"] == {
        "2026-04": provider_sha, "2026-05": provider_sha,
    }
    assert spec["months_without_provider_hash"] == []
