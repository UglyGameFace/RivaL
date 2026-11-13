"""Actual daily-original -> partial partition -> XMarket split/publication path."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.research import archive_training, learning, recorded_candles, xmarket_training
from parlay_bot.research.audit import audit_partition
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.training_warehouse import TrainingWarehouse
from tests.xmarket_fixtures import point, valid_doc

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def candle(at, *, asset="BTC", venue="bitget", close=100.):
    contract = {"bitget": f"{asset}USDT", "okx": f"{asset}-USDT-SWAP",
                "gate": f"{asset}_USDT"}[venue]
    return {"venue": venue, "contract": contract, "quote": "USDT", "interval_minutes": 5,
            "opened_at": at.isoformat(),
            "closed_at": (at + timedelta(minutes=5, milliseconds=-1)).isoformat(),
            "first_observed_at": (at + timedelta(minutes=5, seconds=1)).isoformat(),
            "open_price": close, "high_price": close + 1, "low_price": close - 1,
            "close_price": close, "volume_base": 20., "trades": 10}


class Drive:
    DRIVE_URL = "https://drive.test"
    folders = SimpleNamespace(history="hist")

    def __init__(self, rows):
        self._client = self
        self.files, self.originals, self.calls = [], {}, []
        days = sorted({r["first_observed_at"][:10] for r in rows})
        for day in days:
            raw = gzip.compress(b"\n".join(json.dumps(r).encode() for r in rows
                                         if r["first_observed_at"].startswith(day)), mtime=0)
            file_id = f"day-{day}"
            self.originals[file_id] = raw
            self.files.append({"id": file_id, "name":
                f"rival-records__perp_market_candles__{day}.ndjson.gz", "parents": ["hist"],
                "size": len(raw), "md5Checksum": hashlib.md5(raw, usedforsecurity=False).hexdigest(),
                "appProperties": {"rival_kind": "history", "rival_sha256": hashlib.sha256(raw).hexdigest()}})

    async def _verify_folder_boundary(self):
        self.calls.append("boundary")

    @staticmethod
    def _escape_query(value):
        return value

    async def _headers(self):
        return {}

    async def get(self, url, params, headers):
        self.calls.append(url)
        raw = self.originals[url.rsplit("/", 1)[-1]] if params.get("alt") == "media" else b""
        return SimpleNamespace(content=raw, json=lambda: {"files": self.files},
                               raise_for_status=lambda: None)


async def restore(drive, root, *, asset="BTC"):
    return await recorded_candles.restore_recorded_partitions(
        drive, asset=asset, root=root, first_day=date(2026, 11, 8),
        last_day=date(2026, 11, 12), now=NOW)


@pytest.mark.asyncio
async def test_verified_partial_restores_originals_gaps_clocks_and_honest_provenance(tmp_path):
    first = datetime(2026, 11, 8, tzinfo=UTC)
    rows = [candle(first + timedelta(minutes=5 * i)) for i in (0, 1, 3, 4)]
    drive = Drive(rows)
    result = await restore(drive, tmp_path)
    assert drive.calls[0] == "boundary"
    assert result["status"] == "restored_partial_partitions"
    assert result["missing_backup_days"] == [f"2026-11-{d:02d}" for d in range(9, 13)]
    path = tmp_path / "verified_venue_perp_klines"
    manifest = next(path.glob("*.manifest.json"))
    meta, normalized = learning._rows(manifest)
    assert normalized[0]["first_observed_at"] == rows[0]["first_observed_at"]
    assert meta["partition_coverage"]["internal_missing_rows"] == 1
    assert meta["partition_coverage"]["missing_elapsed_rows"] == 12 * 288 - 4
    assert not meta["full_calendar_month_verified"]
    assert not audit_partition(manifest)["full_calendar_month_verified"]
    frames, provenance = archive_training.load_frames_manifests(tmp_path, target_asset="BTC")
    assert list(frames) == ["bitget|BTCUSDT|USDT"]
    assert len(frames["bitget|BTCUSDT|USDT"][0]) == 4
    assert provenance["provider_sha256_by_month"] == {}
    assert provenance["months_without_provider_hash"] == ["bitget:2026-11"]
    receipt = provenance["recorded_partitions"]["bitget:2026-11"]["source_files"][0]
    assert receipt["sha256"] == hashlib.sha256(next(iter(drive.originals.values()))).hexdigest()
    # Repeated identical restoration is idempotent and preserves original bytes.
    again = await restore(drive, tmp_path)
    assert result == again
    assert len(list(path.glob("*.manifest.json"))) == 1


@pytest.mark.parametrize("defect", ["sha", "size", "missing_sha", "missing_md5", "duplicate_day", "corrupt_bytes"])
@pytest.mark.asyncio
async def test_unverified_or_conflicting_originals_fail_before_partition_creation(tmp_path, defect):
    drive = Drive([candle(datetime(2026, 11, 8, tzinfo=UTC))])
    if defect == "sha":
        drive.files[0]["appProperties"]["rival_sha256"] = "f" * 64
    elif defect == "size":
        drive.files[0]["size"] += 1
    elif defect == "missing_sha":
        drive.files[0]["appProperties"].pop("rival_sha256")
    elif defect == "missing_md5":
        drive.files[0].pop("md5Checksum")
    elif defect == "duplicate_day":
        drive.files.append({**drive.files[0], "id": "conflicting"})
    else:
        drive.originals[drive.files[0]["id"]] += b"changed"
    with pytest.raises(ValueError, match="integrity|SHA-256|conflicting"):
        await restore(drive, tmp_path)
    assert not list(tmp_path.rglob("*.manifest.json"))


@pytest.mark.parametrize("defect", ["off_grid", "early_observed", "wrong_day", "bad_price", "conflict"])
@pytest.mark.asyncio
async def test_invalid_ledger_rows_cannot_become_verified_partials(tmp_path, defect):
    row = candle(datetime(2026, 11, 8, tzinfo=UTC))
    if defect == "off_grid":
        row["opened_at"] = "2026-11-08T00:00:01+00:00"
    elif defect == "early_observed":
        row["first_observed_at"] = "2026-11-08T00:00:01+00:00"
    elif defect == "wrong_day":
        row["closed_at"] = "2026-11-09T00:04:59.999000+00:00"
    elif defect == "bad_price":
        row["high_price"] = 1
    rows = [row]
    if defect == "conflict":
        rows.append({**row, "close_price": 100.5})
    with pytest.raises(HistoryImportError):
        await restore(Drive(rows), tmp_path)
    assert not list(tmp_path.rglob("*.manifest.json"))


@pytest.mark.parametrize("defect", ["original_bytes", "source_row", "coverage", "future_clock", "wrong_quote"])
@pytest.mark.asyncio
async def test_reload_rechecks_originals_and_manifest_evidence(tmp_path, defect):
    await restore(Drive([candle(datetime(2026, 11, 8, tzinfo=UTC))]), tmp_path)
    manifest = next(tmp_path.rglob("*.manifest.json"))
    meta = json.loads(manifest.read_text())
    if defect == "original_bytes":
        next((tmp_path / "record-originals").rglob("*.gz")).write_bytes(b"corrupted")
    elif defect == "source_row":
        data = manifest.parent / meta["file"]
        rows = [json.loads(line) for line in data.read_bytes().splitlines()]
        rows[0]["close"] = 100.5
        changed = b"\n".join(json.dumps(row).encode() for row in rows)
        data.write_bytes(changed)
        meta["data_sha256"] = hashlib.sha256(changed).hexdigest()
        meta["source_sha256"] = hashlib.sha256(json.dumps(
            rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    elif defect == "coverage":
        meta["partition_coverage"]["missing_elapsed_rows"] = 0
    elif defect == "future_clock":
        meta["partition_coverage"]["through_exclusive"] = "2026-11-08T00:00:00+00:00"
    else:
        meta["quote"] = "USDC"
    manifest.write_text(json.dumps(meta))
    with pytest.raises(HistoryImportError):
        archive_training.load_frames_manifests(tmp_path, target_asset="BTC")


@pytest.mark.asyncio
async def test_alt_restoration_retains_matching_btc_reference_without_cross_asset_mix(tmp_path):
    first = datetime(2026, 11, 8, tzinfo=UTC)
    rows = [candle(first, asset=asset, venue=venue) for asset in ("BTC", "ETH", "SOL")
            for venue in ("bitget", "okx")]
    await restore(Drive(rows), tmp_path, asset="ETH")
    frames, provenance = archive_training.load_frames_manifests(tmp_path, target_asset="ETH")
    assert set(frames) == {"bitget|ETHUSDT|USDT", "okx|ETH-USDT-SWAP|USDT"}
    assert set(provenance["recorded_partitions"]) == {"bitget:2026-11", "okx:2026-11"}
    assert set(provenance["reference_recorded_partitions"]) == set(provenance["recorded_partitions"])


@pytest.mark.parametrize("horizon", [5, 15, 60])
@pytest.mark.asyncio
async def test_november_overlap_still_cannot_fill_fixed_historical_splits(tmp_path, monkeypatch, horizon):
    from parlay_bot.research import weekly_archive, xmarket_history

    first = datetime(2026, 11, 8, tzinfo=UTC)
    rows = [candle(first + timedelta(minutes=5 * i), close=100 + i % 20)
            for i in range(1200)]
    await restore(Drive(rows), tmp_path)
    frames, provenance = archive_training.load_frames_manifests(tmp_path, target_asset="BTC")
    samples = [point(source, first + timedelta(minutes=5 * (i + 1)), .51 + i % 3 * .01)
               for source in ("polymarket", "kalshi") for i in range(1200)]
    inputs = {"samples": samples, "live_venues": ["bitget"], "verification": {
        source: {"status": "verified"} for source in ("polymarket", "kalshi")}}
    # Older provider sources can satisfy provenance, but cannot invent earlier
    # prediction-market rows. This fixture isolates that important distinction.
    provenance["provider_sha256_by_month"] = {"bitget:2026-10": "a" * 64}
    training = {"asset": "BTC", "horizon": horizon, "train_end": datetime(2026, 9, 18, tzinfo=UTC),
                "test_start": datetime(2026, 10, 16, tzinfo=UTC),
                "trained_through": datetime(2026, 11, 12, 23, 55, tzinfo=UTC),
                "provenance": provenance, "code_version": "fixture"}
    async def local(*args, **kwargs):
        return inputs
    monkeypatch.setattr(xmarket_history, "weekly_inputs_once", local)
    async def no_request(*args, **kwargs):
        return None
    monkeypatch.setattr(weekly_archive, "next_scheduled_request", no_request)
    published = []
    class Publisher:
        async def ensure_research_output(self, **kwargs):
            published.append(kwargs["kind"])
        async def aclose(self):
            pass
    monkeypatch.setattr(weekly_archive, "_drive", lambda _: Publisher())
    original_fit = weekly_archive._fit
    monkeypatch.setattr(weekly_archive, "_fit", lambda module, *a, **kw:
                        original_fit(module, *a, **kw) if module is xmarket_training
                        else ({"gates_passed": False}, None, "blocked"))
    assert await weekly_archive.train_weekly_archive(frames, training=training, census={},
                                                     out=tmp_path / "models", publish=True) == 0
    report = json.loads((tmp_path / f"models/xmarket-v1-BTC-{horizon}m-report.json").read_text())
    proof = report["overlap_evidence"]
    assert proof["by_stream"]["bitget|BTCUSDT|USDT"]["all_sources_rows"] == 1200
    assert proof["splits"]["test"]["rows"] > 100
    assert proof["splits"]["train"]["shortfall"] == 300
    assert proof["splits"]["validation"]["shortfall"] == 100
    assert not proof["eligible"] and report["training_status"] == "blocked"
    assert report["provenance"]["recorded_partitions"]
    assert set(published) == {"report"}
    assert not list((tmp_path / "models").glob("*-model.json"))


def test_partial_source_cannot_import_without_proven_overlap_splits():
    doc = valid_doc()
    doc["provenance"]["recorded_partitions"] = {"bitget:2026-11": {}}
    with pytest.raises(ValueError, match="overlap"):
        xmarket_training.validate(doc)
    # Previously validated artifacts retain their existing contract.
    old = copy.deepcopy(doc)
    old["provenance"].pop("recorded_partitions")
    assert xmarket_training.validate(old) is old


def test_workflow_exposes_restoration_metadata_before_expensive_training():
    from pathlib import Path

    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/archive-model-training.yml").read_text()
    restore_step = workflow.index("- name: Upload restoration evidence")
    train_step = workflow.index("- name: Train (all horizons)")
    assert restore_step < train_step
    upload = workflow[restore_step:train_step]
    assert "path: data/archive-v1-restore/restore-report.json" in upload
    assert "path: data/archive-v1-restore/\n" not in upload  # never publish private originals
    assert '-28 days' in workflow and '-56 days' in workflow


@pytest.mark.asyncio
async def test_missing_originals_report_missing_days_without_making_partitions(tmp_path):
    result = await restore(Drive([]), tmp_path)
    assert result["status"] == "no_eligible_recorded_candles"
    assert len(result["missing_backup_days"]) == 5
    assert result["restored"] == []


@pytest.mark.asyncio
async def test_later_restoration_extends_partial_snapshot_without_duplicate_training_rows(tmp_path):
    row = candle(datetime(2026, 11, 8, tzinfo=UTC))
    await restore(Drive([row]), tmp_path)
    await restore(Drive([row, candle(datetime(2026, 11, 9, tzinfo=UTC))]), tmp_path)
    assert len(list(tmp_path.rglob("*.manifest.json"))) == 2  # originals retained
    frames, _ = archive_training.load_frames_manifests(tmp_path, target_asset="BTC")
    assert len(frames["bitget|BTCUSDT|USDT"][0]) == 2


@pytest.mark.asyncio
async def test_advancing_cutoff_with_no_new_candles_retains_immutable_snapshots(tmp_path):
    drive = Drive([candle(datetime(2026, 11, 8, tzinfo=UTC))])
    await restore(drive, tmp_path)
    await recorded_candles.restore_recorded_partitions(
        drive, asset="BTC", root=tmp_path, first_day=date(2026, 11, 8),
        last_day=date(2026, 11, 13), now=datetime(2026, 11, 14, tzinfo=UTC))
    assert len(list(tmp_path.rglob("*.manifest.json"))) == 2
    frames, _ = archive_training.load_frames_manifests(tmp_path, target_asset="BTC")
    assert len(frames["bitget|BTCUSDT|USDT"][0]) == 1


@pytest.mark.asyncio
async def test_verified_complete_month_wins_over_partial_ledger_copy(learning_store, tmp_path):
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.research.source_native import export_source_month
    from tests.test_source_native_research import _seed_month

    market = PerpetualMarketStore(learning_store)
    market.initialize()
    _seed_month(market, venue="bitget", contract="BTCUSDT", quote="USDT", month=9)
    complete = export_source_month(market, venue="bitget", contract="BTCUSDT", quote="USDT",
                                   month="2026-09", root=tmp_path, now=NOW)
    original = complete.data_path.read_bytes()
    result = await recorded_candles.restore_recorded_partitions(
        Drive([candle(datetime(2026, 9, 8, tzinfo=UTC), close=500)]), asset="BTC", root=tmp_path,
        first_day=date(2026, 9, 8), last_day=date(2026, 9, 12), now=NOW)
    assert not result["restored"]
    assert complete.data_path.read_bytes() == original
    assert audit_partition(complete.manifest_path)["full_calendar_month_verified"]


@pytest.mark.asyncio
async def test_bounded_listing_cannot_claim_a_truncated_inventory(tmp_path):
    drive = Drive([])
    async def page(*args, **kwargs):
        return SimpleNamespace(json=lambda: {"files": [], "nextPageToken": "still-more"},
                               raise_for_status=lambda: None)
    drive.get = page
    with pytest.raises(ValueError, match="bounded pages"):
        await TrainingWarehouse(drive, tmp_path).records("perp_market_candles", require_sha256=True)
