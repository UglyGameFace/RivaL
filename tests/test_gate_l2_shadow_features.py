"""Authentic archived Gate L2 to shadow research, never live model promotion."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_l2_shadow_features import (
    derive_features,
    load_sidecars,
    write_sidecar,
)

HOUR = "2023042505"
FIRST = 1682398802.134


def _source(*, invalid=False):
    snapshots = []
    for idx in range(2):
        snapshots.append({
            "current": FIRST + idx * 300,
            "update": FIRST + idx * 300 - .003,
            "id": 36309008890 + idx,
            "asks": [{"p": "27404", "s": 4}, {"p": "27404.2", "s": 2}],
            "bids": [{"p": "27403.9" if not invalid else "27405", "s": 3},
                     {"p": "27403.8", "s": 6}],
        })
    return gzip.compress(
        ("".join(json.dumps(row) + "\n" for row in snapshots)).encode()
    )


def _original(tmp_path: Path, raw: bytes) -> tuple[Path, Path]:
    sha = hashlib.sha256(raw).hexdigest()
    name = f"gate-hourly-l2-BTC-orderbooks_slice-{HOUR}--{sha[:16]}.gz"
    original = tmp_path / name
    manifest = tmp_path / (name + ".manifest.json")
    original.write_bytes(raw)
    manifest.write_text(json.dumps({
        "schema_version": "rival-gate-hourly-l2-original-v1",
        "venue": "gate", "asset": "BTC", "contract": "BTC_USDT",
        "quote": "USDT", "family": "orderbooks_slice",
        "utc_hour": HOUR, "original_file": name,
        "original_sha256": sha, "model_training_eligible": False,
        "forward_model_promoted": False,
    }))
    return original, manifest


def test_snapshot_features_only_include_real_observations(tmp_path):
    original, manifest = _original(tmp_path, _source())
    rows = derive_features(
        original.read_bytes(), json.loads(manifest.read_text()),
    )
    assert len(rows) == 2  # NEVER fabricate the ten absent five-minute buckets
    assert rows[0]["features"]["l2_top5_imbalance"] == pytest.approx(3 / 15)
    assert rows[0]["features"]["last_snapshot_spread_bps"] > 0
    assert rows[0]["event_bucket_open"] == "2023-04-25T05:00:00+00:00"
    assert rows[0]["available_after"] == "2023-04-25T05:05:00+00:00"
    assert rows[1]["event_bucket_open"] == "2023-04-25T05:05:00+00:00"
    assert all(not row["live_observation_time_verified"] for row in rows)
    assert all(not row["continuous_orderbook_replay_verified"] for row in rows)


def test_shadow_receipts_rehash_original_manifest_and_derived_bytes(tmp_path):
    original, manifest = _original(tmp_path, _source())
    sidecar, receipt = write_sidecar(original, manifest)
    assert len(load_sidecars([receipt], contract="BTC_USDT")) == 2
    proof = json.loads(receipt.read_text())
    assert proof["derived_bucket_count"] == 2
    assert proof["prospective_forecasts"] == 0
    assert proof["live_model_import_allowed"] is False
    assert proof["after_cost_edge_verified"] is False
    assert not proof["full_utc_hour_covered"]
    assert write_sidecar(original, manifest) == (sidecar, receipt)

    sidecar.write_text(sidecar.read_text() + "{}\n")
    with pytest.raises(HistoryImportError, match="verification failed"):
        load_sidecars([receipt], contract="BTC_USDT")


def test_source_and_manifest_conflicts_fail_closed(tmp_path):
    original, manifest = _original(tmp_path, _source())
    with pytest.raises(HistoryImportError, match="identity"):
        derive_features(original.read_bytes(), {
            **json.loads(manifest.read_text()), "asset": "ETH",
        })
    with pytest.raises(HistoryImportError, match="crossed"):
        original_bad, manifest_bad = _original(tmp_path, _source(invalid=True))
        write_sidecar(original_bad, manifest_bad)
    original, manifest = _original(tmp_path, _source())
    sidecar, receipt = write_sidecar(original, manifest)
    original.write_bytes(_source() + b"another-source")
    with pytest.raises(HistoryImportError):
        load_sidecars([receipt], contract="BTC_USDT")
    original.write_bytes(_source())
    with pytest.raises(HistoryImportError, match="conflict"):
        sidecar.write_bytes(b"tampered")
        write_sidecar(original, manifest)


def test_one_hour_cannot_be_admitted_as_six_month_training(tmp_path):
    from parlay_bot.research.gate_l2_shadow_features import evaluate_shadow

    original, manifest = _original(tmp_path, _source())
    _, receipt = write_sidecar(original, manifest)
    with pytest.raises((HistoryImportError, ValueError)):
        evaluate_shadow([], [receipt], horizon=5)
