"""No fabricated OKX minute history: catalog ZIPs, UTC edges and aggregation."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.research import okx_file_first as files
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows

START = datetime(2026, 5, 1, tzinfo=UTC)
END = START + timedelta(minutes=10)
NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _zip_minute_csv(*, first: int = 0, end: int = 8, missing: int = -1,
                    name: str = "candles.csv") -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote"])
    for i in range(first, end):
        if i == missing:
            continue
        ts = int((START + timedelta(minutes=i)).timestamp() * 1000)
        writer.writerow([ts, 100 + i, 102 + i, 99 + i, 101 + i,
                         "2", "0.5", "50"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, out.getvalue().encode())
    return buffer.getvalue()


def _handler(request, *, missing: int = -1, bad_member: bool = False,
):
    if request.url.path.endswith("/market-data-history"):
        month = (datetime.fromtimestamp(
            int(request.url.params["begin"]) / 1000, UTC,
        ) + timedelta(hours=8)).strftime("%Y-%m")
        # This fixture uses a UTC calendar; production uses UTC+8 catalog
        # boundaries and receives a one-month shifted timestamp.
        url = (
            "https://static.okx.com/cdn/okex/traderecords/"
            f"candles/monthly/{month.replace('-', '')}/"
            f"BTC-USDT-SWAP-1m-{month}.zip"
        )
        return httpx.Response(200, json={"code": "0", "data": [{
            "dateAggrType": "monthly",
            "details": [{"instFamily": "BTC-USDT", "instType": "SWAP",
                         "groupDetails": [{"url": url,
                                           "filename": url.rsplit("/", 1)[-1]}]}],
        }]})
    if str(request.url).startswith("https://static.okx.com"):
        # The second downloaded archive supplies the final 2 UTC minutes.
        second = "-06.zip" in str(request.url)
        return httpx.Response(200, content=_zip_minute_csv(
            first=8 if second else 0, end=10 if second else 8,
            missing=missing, name="../escape.csv" if bad_member else "candles.csv",
        ))
    raise AssertionError("unexpected provider endpoint")


@pytest.mark.asyncio
async def test_native_okx_file_month_rebuilds_exact_five_minute_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
    ) as client:
        result = await files.download_file_month(
            client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
        )
    meta, rows = _rows(result.manifest_path)
    assert len(rows) == 2
    assert meta["source_integrity"] == files.INTEGRITY
    assert meta["provider_signed_checksum"] is False
    assert meta["source_minutes_verified"] == 10
    assert len(meta["source_archive_files"]) == 2
    assert all(len(item["sha256"]) == 64 for item in meta["source_archive_files"])
    assert rows[0]["open"] == 100
    assert rows[0]["close"] == 105
    assert rows[0]["high"] == 106
    assert rows[0]["low"] == 99
    assert rows[0]["volume_contracts"] == 10
    assert rows[0]["volume_base"] == 2.5
    assert rows[0]["volume_quote"] == 250
    assert rows[1]["opened_at"] == (START + timedelta(minutes=5)).isoformat()
    assert rows[1]["first_observed_at"] == NOW.isoformat()
    assert meta["live_trading_authorized"] is False


@pytest.mark.asyncio
async def test_missing_source_minute_refuses_native_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _handler(request, missing=6)),
    ) as client:
        with pytest.raises(HistoryImportError, match="incomplete"):
            await files.download_file_month(
                client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
            )
    assert not list(tmp_path.rglob("*.ndjson"))


@pytest.mark.asyncio
async def test_unsafe_zip_member_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _handler(request, bad_member=True)
        ),
    ) as client:
        with pytest.raises(HistoryImportError, match="unsafe"):
            await files.download_file_month(
                client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
            )


def test_unsupported_csv_schema_and_duplicate_minute_rejected():
    ts = int(START.timestamp() * 1000)
    with pytest.raises(HistoryImportError, match="schema"):
        files._read_csv(b"whatever,columns\nabc,def\n",
                        target_start_ms=ts, target_end_ms=ts + 60000, asset="BTC")
    data = (
        f"ts,o,h,l,c,vol,volCcy,volCcyQuote\n"
        f"{ts},100,102,99,101,2,0.5,50\n"
        f"{ts},100,102,99,101,2,0.5,50\n"
    ).encode()
    with pytest.raises(HistoryImportError, match="duplicate"):
        files._read_csv(data, target_start_ms=ts,
                        target_end_ms=ts + 60000, asset="BTC")


@pytest.mark.asyncio
async def test_stream_archive_429_blocks_and_preserves_fallback_signal():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(429),
    )) as client:
        with pytest.raises(HistoryImportError, match="HTTP 429"):
            await files._download_archive(
                client, url="https://static.okx.com/cdn/okex/sample.zip",
            )


@pytest.mark.asyncio
async def test_adjacent_monthly_zip_cache_does_not_redownload(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    archive_downloads = []

    def record(request):
        if str(request.url).startswith("https://static.okx.com"):
            archive_downloads.append(str(request.url))
        return _handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        for _ in range(2):
            await files.download_file_month(
                client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
            )
    assert len(archive_downloads) == 2
    cache = list(tmp_path.rglob("*.zip"))
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_tampered_archive_cache_is_not_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "month_bounds", lambda month: (START, END))
    monkeypatch.setattr(files, "_next_month", lambda month: "2026-06")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
    ) as client:
        await files.download_file_month(
            client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
        )
        cache = list(tmp_path.rglob("*.zip"))
        assert len(cache) == 2
        cache[0].write_bytes(b"modified archive")
        with pytest.raises(HistoryImportError, match="cached OKX archive content changed"):
            await files.download_file_month(
                client, asset="BTC", month="2026-05", root=tmp_path, now=NOW,
            )


@pytest.mark.asyncio
async def test_bootstrap_preserves_original_zip_bytes_and_verified_5m(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from parlay_bot.research import audit
    from parlay_bot.research import official_okx_bootstrap as okx
    from parlay_bot.research.free_history import _persist
    from parlay_bot.research.source_native import SOURCE

    async def verify_contract(client, asset):
        return "BTC-USDT-SWAP"

    async def restore(*args, **kwargs):
        return []

    async def download_file(client, *, asset, month, root, now):
        assert month == "2026-10"
        archives = []
        for archive_month in ("2026-10", "2026-11"):
            url = ("https://static.okx.com/cdn/okex/monthly/"
                   f"BTC-USDT-SWAP-1m-{archive_month}.zip")
            path = files.archive_cache_path(
                root=root, asset=asset, month=archive_month, url=url,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"official-{archive_month}".encode()
            path.write_bytes(payload)
            archives.append({
                "month": archive_month,
                "url": url,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        return _persist(
            source=SOURCE, key=f"okx-BTC-USDT-SWAP-USDT-5m-{month}",
            records=[{
                "venue": "okx", "contract": "BTC-USDT-SWAP",
                "quote": "USDT", "interval_minutes": 5,
                "opened_at": "2026-10-01T00:00:00+00:00",
                "closed_at": "2026-10-01T00:04:59.999000+00:00",
                "first_observed_at": NOW.isoformat(),
                "open": 100, "high": 101, "low": 99, "close": 100,
                "volume_contracts": 2, "volume_base": 1,
                "volume_quote": 100, "trades": -1,
            }],
            raw_sha="f" * 64, url=archives[0]["url"], out_dir=root,
            extra={
                "venue": "okx", "contract": "BTC-USDT-SWAP",
                "quote": "USDT", "interval_minutes": 5,
                "source_integrity": files.INTEGRITY,
                "source_archive_files": archives,
                "provider_signed_checksum": False,
            },
        )

    async def inventory(*args, **kwargs):
        return {"modules": {}}

    async def research(**kwargs):
        return {"status": "verified_research_published", "published_models": 0}

    class Drive:
        def __init__(self):
            self.history = []
            self.verifications = []
            self.folders = SimpleNamespace(history="history", manifests="manifests")
        async def ensure_history_file(self, *, artifact, batch_id, mime_type):
            self.history.append((artifact.file_name, artifact.sha256, mime_type))
            return SimpleNamespace(file_id="history" + str(len(self.history)))
        async def ensure_manifest_file(self, *, artifact, batch_id):
            return SimpleNamespace(file_id="manifest")
        async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
            self.verifications.append((file_id, parent_id, artifact.sha256))

    monkeypatch.setattr(okx, "verify_contract", verify_contract)
    monkeypatch.setattr(okx, "restore_verified_source_months", restore)
    monkeypatch.setattr(files, "download_file_month", download_file)
    monkeypatch.setattr(okx, "inventory_extra_modules", inventory)
    monkeypatch.setattr(okx, "run_native_research", research)
    monkeypatch.setattr(audit, "audit_catalog", lambda *a, **k: {"partitions": []})
    drive = Drive()
    result = await okx.bootstrap(
        drive=drive, client=object(), asset="BTC", root=tmp_path / "source",
        report_dir=tmp_path / "report", max_new_months=1,
        now=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert result["newly_published_months"] == ["2026-10"]
    assert len(result["raw_one_minute_archives_verified"]) == 2
    assert len(drive.history) == 3
    assert sum(mime == "application/zip" for _, _, mime in drive.history) == 2
    assert len(drive.verifications) == 4
    assert all("file_id" not in row for row in result["raw_one_minute_archives_verified"])
