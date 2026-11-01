"""L2 replay and offline auxiliary model anti-leakage regressions."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.okx_auxiliary_ablation import _asof, _load_one, load_sidecars
from parlay_bot.research.okx_l2_replay import ArchivedBook

T0 = datetime(2026, 5, 1, tzinfo=UTC)


def test_sequence_reconstruction_and_depth_imbalance():
    book = ArchivedBook(400)
    initial = book.apply({
        "action": "snapshot", "seqId": 8,
        "bids": [["100", "3"], ["99", "2"]],
        "asks": [["101", "1"], ["102", "2"]],
    }, 1000)
    assert initial["snapshot"] is True
    assert initial["sequence_verified"] is False
    assert initial["l2_top5_imbalance"] > 0
    result = book.apply({
        "action": "update", "prevSeqId": 8, "seqId": 9,
        "bids": [["100", "0"], ["99", "4"]],
        "asks": [["101", "5"]],
    }, 1001)
    assert result["sequence_verified"] is True
    assert result["snapshot"] is False
    assert book.applied_updates == 1
    assert max(book.bids) == 99


def test_sequence_gap_invalidates_and_requires_new_snapshot():
    book = ArchivedBook(400)
    book.apply({"action": "snapshot", "seqId": "8",
                "bids": [["100", "1"]], "asks": [["101", "1"]]}, 1000)
    assert book.apply({"action": "update", "seqId": 10, "prevSeqId": 7,
                       "bids": [["100", "2"]]}, 1001) is None
    assert book.gaps == 1
    assert book.valid is False
    assert book.apply({"action": "update", "seqId": 11, "prevSeqId": 10,
                       "bids": [["100", "3"]]}, 1002) is None
    assert book.unlinked == 1
    book.apply({"action": "snapshot", "seqId": 12,
                "bids": [["100", "1"]], "asks": [["101", "1"]]}, 1003)
    assert book.apply({"action": "update", "seqId": 13, "prevSeqId": 12,
                       "bids": [["100", "2"]]}, 1004) is not None


def test_no_sequence_means_no_reconstructed_update():
    book = ArchivedBook(400)
    snap = book.apply({"action": "snapshot", "bids": [["100", "1"]],
                       "asks": [["101", "1"]]}, 1000)
    assert snap["sequence_verified"] is False
    assert book.apply({"action": "update", "bids": [["100", "9"]]}, 1001) is None
    assert not book.valid


@pytest.mark.parametrize("data", [
    {"action": "snapshot", "bids": [["100", "1"]],
     "asks": [["100", "1"]]},
    {"action": "snapshot", "bids": [["100", "-1"]],
     "asks": [["101", "1"]]},
    {"action": "snapshot", "bids": [["100", "1"], ["100", "2"]],
     "asks": [["101", "1"]]},
])
def test_crossed_or_invalid_l2_never_produces_quote(data):
    with pytest.raises(HistoryImportError):
        ArchivedBook(400).apply(data, 1000)


def test_asof_never_uses_future_and_bounds_funding_staleness():
    issue = T0 + timedelta(minutes=15)
    samples = [(issue, issue + timedelta(minutes=5), (0.1,)*10, 1)]
    late = [(issue + timedelta(milliseconds=1), (.123,))]
    assert _asof(samples, late, module="funding_rates") == []
    earlier = [(issue - timedelta(hours=8), (.123,))]
    assert len(_asof(samples, earlier, module="funding_rates")) == 1
    very_old = [(issue - timedelta(hours=10), (.123,))]
    assert _asof(samples, very_old, module="funding_rates") == []


def _receipt(tmp_path, month="2026-05", *, instrument="BTC-USDT-SWAP"):
    base = f"{instrument}-funding-{month}.zip"
    raw = b"source-immutable-archive-bytes"
    body = {
        "venue": "okx", "contract": instrument, "event_bucket_open": T0.isoformat(),
        "available_after": (T0 + timedelta(minutes=5)).isoformat(),
        "features": {"funding_rate": 0.0001},
    }
    payload = (json.dumps(body) + "\n").encode()
    (tmp_path / base).write_bytes(raw)
    (tmp_path / (base + ".research.ndjson")).write_bytes(payload)
    receipt = {
        "schema_version": "okx-auxiliary-research-v1",
        "venue": "okx", "contract": instrument, "module": "funding_rates",
        "month": month, "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "feature_sha256": hashlib.sha256(payload).hexdigest(), "feature_buckets": 1,
        "prospective_forecast": False, "live_model_import_allowed": False,
        "after_cost_edge_verified": False, "source_provider_checksum_verified": False,
    }
    path = tmp_path / (base + ".receipt.json")
    path.write_text(json.dumps(receipt))
    return path


def test_auxiliary_receipts_require_exact_byte_checksums_and_venue(tmp_path):
    path = _receipt(tmp_path)
    module, items = _load_one(path, venue="okx", contract="BTC-USDT-SWAP")
    assert module == "funding_rates" and len(items) == 1
    assert load_sidecars([path], venue="okx", contract="BTC-USDT-SWAP")[0] == module
    with pytest.raises(HistoryImportError, match="source-native"):
        _load_one(path, venue="bitget", contract="BTC-USDT-SWAP")
    raw = tmp_path / path.name.removesuffix(".receipt.json")
    raw.write_bytes(b"modified")
    with pytest.raises(HistoryImportError, match="checksum"):
        _load_one(path, venue="okx", contract="BTC-USDT-SWAP")


def test_duplicate_archives_and_future_available_at_fail_closed(tmp_path):
    path = _receipt(tmp_path)
    with pytest.raises(HistoryImportError, match="duplicate"):
        load_sidecars([path, path], venue="okx", contract="BTC-USDT-SWAP")
    sidecar = tmp_path / (path.name.removesuffix(".receipt.json") + ".research.ndjson")
    data = json.loads(sidecar.read_text())
    data["available_after"] = T0.isoformat()
    updated = (json.dumps(data) + "\n").encode()
    sidecar.write_bytes(updated)
    receipt = json.loads(path.read_text())
    receipt["feature_sha256"] = hashlib.sha256(updated).hexdigest()
    path.write_text(json.dumps(receipt))
    with pytest.raises(HistoryImportError, match="premature"):
        _load_one(path, venue="okx", contract="BTC-USDT-SWAP")
