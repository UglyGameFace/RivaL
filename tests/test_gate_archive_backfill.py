"""Gate raw public futures archive through private, SHA-readback Drive."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import gate_archive_backfill as gate
from parlay_bot.research.gate_bulk_archive import inspect, url_for

MONTH = "2023-04"
BTC_DATA = gzip.compress(b"1680307200,12,100,101,99,100\n")
SHA = hashlib.sha256(BTC_DATA).hexdigest()


def test_month_scope_and_immutable_source_name():
    assert gate.complete_months(datetime(2023, 3, 1, tzinfo=UTC)) == [
        "2023-01", "2023-02",
    ]
    assert gate.complete_months(datetime(2023, 1, 1, tzinfo=UTC)) == []
    with pytest.raises(ValueError):
        gate.complete_months(datetime(2026, 1, 1))
    name = gate.stem(asset="BTC", kind="candlesticks_5m",
                     month=MONTH, sha=SHA)
    assert name == (
        "gatebulk-futures_usdt-BTC_USDT-candlesticks_5m-"
        f"2023-04--{SHA[:16]}"
    )
    assert gate.EXACT_NAME.fullmatch(name + ".manifest.json")
    with pytest.raises(ValueError):
        gate.stem(asset="BTC", kind="candlesticks_5m",
                  month=MONTH, sha="changed")
    with pytest.raises(ValueError):
        gate.stem(asset="BTC", kind="trades",
                  month="../../etc", sha=SHA)


def test_round_robin_pending_jobs_does_not_starve_on_404():
    work = [("BTC", "candlesticks_5m", f"2023-{i:02d}") for i in range(1, 13)]
    runs = {
        tuple(gate.rotate_attempts(
            work, now=datetime(2023, 3, 1 + i, tzinfo=UTC), count=4,
        ))
        for i in range(6)
    }
    assert len(runs) >= 2
    assert all(len(x) == 4 for x in runs)


@pytest.mark.asyncio
async def test_drive_listing_paginates_and_ignores_non_gate(tmp_path):
    pages = []
    class Client:
        async def get(self, url, *, params, headers):
            pages.append(params)
            if len(pages) == 1:
                return httpx.Response(200, json={
                    "files": [{
                        "name": (
                            "gatebulk-futures_usdt-BTC_USDT-candlesticks_5m-"
                            f"2023-04--{SHA[:16]}.manifest.json"
                        ),
                        "parents": ["manifests"],
                        "appProperties": {"rival_kind": "manifest"},
                    }],
                    "nextPageToken": "next",
                })
            return httpx.Response(200, json={"files": [{
                "name": "unrelated-manifest.json",
                "parents": ["manifests"],
                "appProperties": {"rival_kind": "manifest"},
            }]})

    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="manifests")
        _client = Client()
        async def _verify_folder_boundary(self):
            pass
        async def _headers(self):
            return {}
        @staticmethod
        def _escape_query(value):
            return value

    listed = await gate.listed_archives(Drive())
    assert listed == {("BTC", "candlesticks_5m", MONTH): SHA[:16]}
    assert pages[1]["pageToken"] == "next"
    assert "gatebulk" in pages[0]["q"]


@pytest.mark.asyncio
async def test_drive_listing_rejects_conflicting_hashes():
    class Client:
        async def get(self, url, *, params, headers):
            base = "gatebulk-futures_usdt-BTC_USDT-candlesticks_5m-2023-04--"
            return httpx.Response(200, json={"files": [
                {"name": base + sha + ".manifest.json",
                 "parents": ["manifests"],
                 "appProperties": {"rival_kind": "manifest"}}
                for sha in ("a" * 16, "b" * 16)
            ]})
    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="manifests")
        _client = Client()
        async def _verify_folder_boundary(self):
            pass
        async def _headers(self):
            return {}
        @staticmethod
        def _escape_query(value):
            return value
    with pytest.raises(ValueError, match="conflicting Gate"):
        await gate.listed_archives(Drive())


@pytest.mark.asyncio
async def test_original_source_bytes_and_both_artifacts_reverified(tmp_path):
    source = inspect(BTC_DATA, asset="BTC", kind="candlesticks_5m", month=MONTH)
    source["source_url"] = url_for(asset="BTC", kind="candlesticks_5m", month=MONTH)
    archive = tmp_path / "original.csv.gz"
    archive.write_bytes(BTC_DATA)
    class Drive:
        folders = SimpleNamespace(history="history", manifests="manifests")
        def __init__(self):
            self.history = []
            self.manifests = []
            self.verified = []
        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            assert mime_type == "application/gzip"
            assert batch_id == "gatebulk-" + SHA[:24]
            self.history.append(artifact)
            return SimpleNamespace(file_id="raw-id")
        async def ensure_manifest_file(self, *, artifact, batch_id):
            self.manifests.append(artifact)
            return SimpleNamespace(file_id="manifest-id")
        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            self.verified.append((file_id, parent_id, artifact.sha256))
    drive = Drive()
    receipt = await gate.publish_raw(
        drive, archive=archive, source=source, asset="BTC",
        kind="candlesticks_5m", month=MONTH, root=tmp_path,
    )
    assert receipt["warehouse_readback_sha256_verified"] is True
    assert receipt["source_native_model_eligible"] is False
    assert [x[0] for x in drive.verified] == ["raw-id", "manifest-id"]
    assert drive.history[0].path.read_bytes() == BTC_DATA
    meta = json.loads(drive.manifests[0].path.read_text())
    assert meta["raw_sha256"] == SHA
    assert meta["provider_signed_checksum"] is False
    assert meta["model_training_eligible"] is False
    assert meta["source_url"] == source["source_url"]
    assert "retrieved_at" not in meta
    second = await gate.publish_raw(
        drive, archive=archive, source=source, asset="BTC",
        kind="candlesticks_5m", month=MONTH, root=tmp_path,
    )
    assert second["raw_sha256"] == SHA
    assert len(drive.verified) == 4


@pytest.mark.asyncio
async def test_modified_provider_bytes_never_publish(tmp_path):
    source = inspect(BTC_DATA, asset="BTC", kind="candlesticks_5m", month=MONTH)
    source["source_url"] = url_for(asset="BTC", kind="candlesticks_5m", month=MONTH)
    archive = tmp_path / "changed.gz"
    archive.write_bytes(b"malformed")
    with pytest.raises(ValueError, match="diverged"):
        await gate.publish_raw(
            None, archive=archive, source=source, asset="BTC",
            kind="candlesticks_5m", month=MONTH, root=tmp_path,
        )


@pytest.mark.asyncio
async def test_scheduled_batch_skips_existing_and_publishes_new(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "complete_months",
                        lambda now: ["2023-03", "2023-04"])
    async def listed(drive):
        return {("BTC", "candlesticks_5m", "2023-03"): SHA[:16]}
    monkeypatch.setattr(gate, "listed_archives", listed)
    monkeypatch.setattr(gate, "rotate_attempts",
                        lambda work, *, now, count: work[:count])
    calls = []
    def download(client, *, asset, kind, month, output):
        calls.append((asset, kind, month))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(BTC_DATA)
        source = inspect(BTC_DATA, asset=asset, kind=kind, month=month)
        source["source_url"] = url_for(asset=asset, kind=kind, month=month)
        return source
    monkeypatch.setattr(gate, "download", download)
    published = []
    async def publish_raw(drive, *, archive, source, asset, kind, month, root):
        published.append(archive.read_bytes())
        return {"asset": asset, "kind": kind, "month": month,
                "warehouse_readback_sha256_verified": True}
    monkeypatch.setattr(gate, "publish_raw", publish_raw)
    result = await gate.collect_gate(
        drive=object(), root=tmp_path / "data",
        report_dir=tmp_path / "reports", assets=("BTC",),
        kinds=("candlesticks_5m",), max_new=1, max_attempts=2,
        now=datetime(2023, 5, 1, tzinfo=UTC),
    )
    assert calls == [("BTC", "candlesticks_5m", "2023-04")]
    assert published == [BTC_DATA]
    assert len(result["published"]) == 1
    assert result["remote_manifest_names_skipped_without_readback"] == 1
    assert result["full_source_coverage_proven"] is False
    assert result["live_predictions_modified"] is False
    persisted = json.loads((tmp_path / "reports/gate-bulk-progress.json").read_text())
    assert persisted["published"][0]["month"] == "2023-04"


@pytest.mark.asyncio
async def test_rate_limit_stops_requests_with_visible_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "complete_months", lambda now: ["2023-04"])
    async def listed(drive):
        return {}
    monkeypatch.setattr(gate, "listed_archives", listed)
    monkeypatch.setattr(gate, "rotate_attempts",
                        lambda work, *, now, count: work[:count])
    calls = []
    def download(client, *, asset, kind, month, output):
        calls.append(asset)
        raise ValueError("Gate public archive unavailable: HTTP 429")
    monkeypatch.setattr(gate, "download", download)
    result = await gate.collect_gate(
        drive=object(), root=tmp_path / "data",
        report_dir=tmp_path / "reports", assets=("BTC", "ETH"),
        kinds=("candlesticks_5m",), max_new=1, max_attempts=2,
        now=datetime(2023, 5, 1, tzinfo=UTC),
    )
    assert calls == ["BTC"]
    assert result["provider_throttled"] is True
    assert len(result["unavailable_or_rejected"]) == 1
