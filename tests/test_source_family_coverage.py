"""2021+ ledger is strict about missing evidence, data hashes and source windows."""
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.free_history import _persist
from parlay_bot.research.source_family_coverage import (
    last_completed_month, months, scan_local, write_ledger,
)

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _binance_day_partition(root, *, missing_day=None, asset="BTC"):
    start = datetime(2021, 2, 1, tzinfo=UTC)
    rows = []
    for day in range(28):
        if day == missing_day:
            continue
        opened = start + timedelta(days=day)
        rows.append({
            "opened_at": opened.isoformat(),
            "closed_at": (opened + timedelta(days=1, milliseconds=-1)).isoformat(),
            "open": 100., "high": 102., "low": 98., "close": 101.,
            "volume_base": 4., "trades": 10,
        })
    return _persist(
        source="binance_um_klines", key=f"{asset}USDT-1d-2021-02",
        records=rows, raw_sha="a" * 64,
        url="https://data.binance.vision/data/futures/um/monthly/klines/" +
            f"{asset}USDT/1d/{asset}USDT-1d-2021-02.zip",
        out_dir=root, extra={
            "venue": "binance", "contract": asset + "USDT", "quote": "USDT",
            "interval_minutes": 1440, "checksum_verified": True,
            "time_reference": "exchange_candle_open_not_first_observed_at",
        },
    )


def _rows(out):
    with gzip.open(out / "source-family-coverage.jsonl.gz", "rt") as handle:
        return [json.loads(line) for line in handle]


def test_2021_calendar_excludes_open_month():
    assert months("2021-01", "2021-03") == ["2021-01", "2021-02", "2021-03"]
    assert last_completed_month(NOW) == "2026-10"
    with pytest.raises(ValueError):
        months("2020-12", "2021-01")
    with pytest.raises(ValueError):
        write_ledger(roots=[], first="2021-01", through="2026-11",
                     out_dir=None, now=NOW)


def test_no_manifests_never_means_complete(tmp_path):
    summary = write_ledger(
        roots=[], first="2021-01", through="2021-01",
        out_dir=tmp_path, now=NOW,
    )
    assert summary["status_counts"] == {
        "not_verified_in_this_run": summary["source_month_slots"]
    }
    assert not summary["full_2021_to_present_coverage_proven"]
    assert any(
        r["venue"] == "kalshi" and r["status"] == "not_verified_in_this_run"
        and r["provider_publication_and_contract_listing"] == "unverified"
        for r in _rows(tmp_path)
    )


def test_full_month_is_local_only_and_other_families_stay_unknown(tmp_path):
    root = tmp_path / "root"
    _binance_day_partition(root)
    report = tmp_path / "reports"
    summary = write_ledger(
        roots=[root], first="2021-02", through="2021-02",
        out_dir=report, now=NOW,
    )
    matching = [
        r for r in _rows(report)
        if (r["venue"], r["asset"], r["family"]) ==
        ("binance", "BTC", "candles_1d")
    ]
    assert len(matching) == 1
    assert matching[0]["status"] == "local_full_calendar_checked_not_drive_reverified"
    assert matching[0]["drive_sha_readback_this_run"] is False
    assert matching[0]["production_model_feature_admitted"] is False
    assert summary["status_counts"]["local_full_calendar_checked_not_drive_reverified"] == 1
    assert next(r for r in _rows(report) if r["venue"] == "okx"
                and r["family"] == "candles_1d")["status"] == "not_verified_in_this_run"


def test_incomplete_month_never_qualifies(tmp_path):
    root = tmp_path / "incomplete"
    _binance_day_partition(root, missing_day=13)
    states, invalid, checked = scan_local([root])
    assert not invalid and checked == 1
    assert states[("binance", "BTC", "candles_1d", "2021-02")] == (
        "local_incomplete_calendar"
    )


def test_corrupt_data_hash_is_not_counted_as_source_evidence(tmp_path):
    root = tmp_path / "bad"
    artifact = _binance_day_partition(root)
    artifact.data_path.write_bytes(artifact.data_path.read_bytes() + b"tampered")
    states, invalid, _ = scan_local([root])
    assert not states and len(invalid) == 1
    assert "checksum" in invalid[0]["reason"].lower()


def test_archive_sha_manifest_alone_cannot_prove_other_families(tmp_path):
    root = tmp_path / "supplement"
    _persist(
        source="binance_um_funding", key="ETHUSDT-funding-2021-03",
        records=[{"funding_at": "2021-03-01T00:00:00+00:00", "rate": 0.01}],
        raw_sha="b" * 64, url="https://data.binance.vision/official.zip",
        out_dir=root, extra={"checksum_verified": True},
    )
    states, invalid, _ = scan_local([root])
    assert not invalid
    assert states[("binance", "ETH", "funding", "2021-03")] == (
        "local_integrity_only_month_continuity_not_qualified"
    )
