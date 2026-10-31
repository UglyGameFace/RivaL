"""No Binance cross-seeding, retroactive predictions or incomplete source months."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.research.audit import audit_catalog, audit_partition
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _rows, assemble
from parlay_bot.research.source_native import (
    SOURCE,
    NativeMonthlyPublisher,
    export_source_month,
)
from parlay_bot.research.source_native_offline import run_native_research
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


@pytest.fixture
def native_market(tmp_path):
    store = SQLiteHotStore(tmp_path / "native_source.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    try:
        yield market
    finally:
        store.close()


def _seed_month(market, *, venue="hyperliquid", contract="BTC",
                quote="USDC", month=9, remove=None):
    first = datetime(2026, month, 1, tzinfo=UTC)
    last = first.replace(month=month + 1)
    data = []
    total = int((last - first).total_seconds() / 300)
    for i in range(total):
        if i == remove:
            continue
        opened = first + timedelta(minutes=5*i)
        closed = opened + timedelta(minutes=5, milliseconds=-1)
        observed = closed + timedelta(minutes=3)
        close = 100 + (i % 29) * .1
        data.append((
            venue, contract, quote, 5, opened.isoformat(), closed.isoformat(),
            observed.isoformat(), close, close+1, close-1, close, 12., 4,
        ))
    with market.store.connect() as conn:
        # The production compatibility wrapper intentionally exposes only
        # execute, not DB-driver-specific executemany. Batch below SQLite's
        # minimum variable limit and keep PostgreSQL parameter counts bounded.
        prefix = """INSERT INTO perp_market_candles
                    (venue,contract,quote,interval_minutes,opened_at,closed_at,
                     first_observed_at,open_price,high_price,low_price,close_price,
                     volume_base,trades) VALUES """
        for offset in range(0, len(data), 50):
            batch = data[offset:offset + 50]
            slots = ",".join(["(" + ",".join("?" for _ in range(13)) + ")"] * len(batch))
            conn.execute(prefix + slots, tuple(value for row in batch for value in row))
    return total


def test_native_exact_month_audit_never_mislabels_backfill(native_market, tmp_path):
    count = _seed_month(native_market)
    root = tmp_path / "research"
    export = export_source_month(
        native_market, venue="hyperliquid", contract="BTC", quote="USDC",
        month="2026-09", root=root, now=datetime(2026, 10, 31, tzinfo=UTC),
    )
    assert export.records == count
    manifest, rows = _rows(export.manifest_path)
    assert len(rows) == count
    assert manifest["source"] == SOURCE
    assert manifest["provider_signed_checksum"] is False
    assert manifest["prospective_forecast"] is False
    assert manifest["live_trading_authorized"] is False
    assert rows[0]["venue"] == "hyperliquid"
    assert rows[0]["quote"] == "USDC"
    assert rows[0]["first_observed_at"] > rows[0]["closed_at"]
    assert audit_partition(export.manifest_path)["full_calendar_month_verified"]
    summary = audit_catalog(
        root, months=["2026-09"], sources=(SOURCE,),
    )
    assert summary["streams"][0]["venue"] == "hyperliquid"
    assert summary["streams"][0]["quote"] == "USDC"
    assembled = assemble([export.manifest_path])
    assert assembled[0]["venue"] == "hyperliquid"
    assert assembled[0]["quote"] == "USDC"
    assert assembled[2]["months"] == ["2026-09"]
    with pytest.raises(HistoryImportError):
        export_source_month(
            native_market, venue="binance", contract="BTC", quote="USDC",
            month="2026-09", root=root, now=datetime(2026, 10, 31, tzinfo=UTC),
        )
    with pytest.raises(HistoryImportError):
        export_source_month(
            native_market, venue="hyperliquid", contract="BTC", quote="USDC",
            month="2026-10", root=root, now=datetime(2026, 10, 31, tzinfo=UTC),
        )


def test_native_missing_bar_fails_before_archive_promotion(native_market, tmp_path):
    _seed_month(native_market, remove=123)
    with pytest.raises(HistoryImportError, match="incomplete"):
        export_source_month(
            native_market, venue="hyperliquid", contract="BTC", quote="USDC",
            month="2026-09", root=tmp_path, now=datetime(2026, 10, 31, tzinfo=UTC),
        )
    assert not list((tmp_path / SOURCE).glob("*"))


def test_native_tampering_and_wrong_venue_rejected(native_market, tmp_path):
    _seed_month(native_market)
    artifact = export_source_month(
        native_market, venue="hyperliquid", contract="BTC", quote="USDC",
        month="2026-09", root=tmp_path, now=datetime(2026, 10, 31, tzinfo=UTC),
    )
    info = json.loads(artifact.manifest_path.read_text())
    info["venue"] = "okx"
    artifact.manifest_path.write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(HistoryImportError, match="unverified"):
        _rows(artifact.manifest_path)


class _FakeArchiveDrive:
    def __init__(self):
        self.folders = SimpleNamespace(history="hist", manifests="manifest")
        self.history = []
        self.manifest = []
        self.restored = []

    async def ensure_history_file(self, *, artifact, batch_id, mime_type):
        assert mime_type == "application/x-ndjson"
        assert batch_id.startswith("native-")
        self.history.append((artifact.sha256, artifact.size_bytes))
        return SimpleNamespace(file_id="h-" + artifact.sha256[:24])

    async def ensure_manifest_file(self, *, artifact, batch_id):
        assert batch_id.startswith("native-")
        self.manifest.append(artifact.sha256)
        return SimpleNamespace(file_id="m-" + artifact.sha256[:24])

    async def verify_downloaded_artifact(self, *, file_id, parent_id, artifact):
        assert parent_id in {"hist", "manifest"}
        assert hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest() == artifact.sha256
        self.restored.append(file_id)


@pytest.mark.asyncio
async def test_native_publisher_receipts_avoid_replays(native_market, tmp_path):
    _seed_month(native_market)
    drive = _FakeArchiveDrive()
    publisher = NativeMonthlyPublisher(native_market, drive, tmp_path)
    stamp = datetime(2026, 10, 31, 12, tzinfo=UTC)
    result = await publisher.run_once(now=stamp, max_batches=2)
    assert len(result) == 1
    assert result[0]["venue"] == "hyperliquid"
    assert len(drive.history) == len(drive.manifest) == len(drive.restored) // 2 == 1
    assert await publisher.run_once(now=stamp, max_batches=2) == []
    assert len(drive.history) == 1
    with native_market.store.connect() as conn:
        receipt = conn.execute(
            "SELECT * FROM rival_native_research_receipts"
        ).fetchone()
    assert receipt["month"] == "2026-09"


@pytest.mark.asyncio
async def test_native_research_fails_closed_when_archive_has_no_months(tmp_path):
    class EmptyDrive:
        async def _verify_folder_boundary(self):
            return None
        folders = SimpleNamespace(manifests="manifest")
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        _escape_query = staticmethod(lambda value: value)
        async def _headers(self):
            return {}
        class _Client:
            async def get(self, *args, **kwargs):
                return SimpleNamespace(status_code=200, json=lambda: {"files": []})
        _client = _Client()

    result = await run_native_research(
        drive=EmptyDrive(), venue="hyperliquid",
        months=["2026-04","2026-05","2026-06","2026-07","2026-08","2026-09"],
        root=tmp_path / "history", report_dir=tmp_path / "reports", publish=True,
    )
    assert result["status"] == "source_months_incomplete"
    assert result["qualified_source_scopes"] == 0
    assert result["published_models"] == 0
    assert result["promoted_live"] is False
    assert result["profitability_verified"] is False
    reports = list((tmp_path / "reports").glob("source-hyperliquid-research-*.json"))
    assert len(reports) == 1
    saved = json.loads(reports[0].read_text())
    assert saved["status"] == "source_months_incomplete"
    assert saved["published_models"] == 0
    assert saved["requested_months"] == [
        "2026-04", "2026-05", "2026-06",
        "2026-07", "2026-08", "2026-09",
    ]


@pytest.mark.asyncio
async def test_native_research_rejects_research_grade_month_with_one_gap(
    tmp_path, monkeypatch,
):
    import parlay_bot.research.source_native_offline as native

    months = [f"2026-{month:02d}" for month in range(4, 10)]
    scope = {"venue": "hyperliquid", "contract": "BTC",
             "quote": "USDC", "interval_minutes": 5}

    async def restore(*args, **kwargs):
        return [tmp_path / "present.manifest.json"]

    def audit(*args, **kwargs):
        return {
            "streams": [{
                **scope, "months_present": 6, "research_grade": True,
                "missing_months": [],
            }],
            "partitions": [
                {**scope, "month": month,
                 "full_calendar_month_verified": month != "2026-09"}
                for month in months
            ],
            "validation_failures": [],
        }

    monkeypatch.setattr(native, "restore_verified_source_months", restore)
    monkeypatch.setattr(native, "audit_catalog", audit)
    result = await native.run_native_research(
        drive=object(), venue="hyperliquid", months=months,
        root=tmp_path / "source", report_dir=tmp_path / "reports",
        publish=True,
    )
    assert result["status"] == "source_months_incomplete"
    assert result["source_scopes"][0]["verified_full_months"] == 5
    assert result["source_scopes"][0]["missing_or_incomplete_months"] == [
        "2026-09",
    ]


@pytest.mark.parametrize(
    ("status", "models", "exit_code"),
    [
        ("source_months_incomplete", 0, 2),
        ("source_models_not_published", 0, 2),
        ("verified_research_published", 0, 2),
        ("verified_research_published", 3, 0),
    ],
)
def test_native_cli_requires_actual_published_models(
    status, models, exit_code, monkeypatch, capsys,
):
    import parlay_bot.research.source_native_offline as native

    class Drive:
        async def aclose(self):
            return None

    monkeypatch.setattr(native, "Settings", lambda: object())
    monkeypatch.setattr(native, "_drive", lambda settings: Drive())

    async def run(**kwargs):
        assert kwargs["publish"] is True
        return {"status": status, "published_models": models}

    monkeypatch.setattr(native, "run_native_research", run)
    assert native.main(["--venue", "hyperliquid", "--publish"]) == exit_code
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_native_boosted_contract_is_accepted_only_for_its_exact_source(native_market):
    from parlay_bot.research.candidate_registry import (
        ResearchCandidateRegistry,
        verified_candidate,
    )
    from tests.test_portable_boosted_forward import _document

    doc = _document()
    scope = doc["model_spec"]["identity"]
    scope.update(venue="hyperliquid", contract="BTC", quote="USDC")
    doc["model_sha256"] = hashlib.sha256(json.dumps(
        doc["model_spec"], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    data = json.dumps(doc).encode()
    assert verified_candidate(data)["asset"] == "BTC"
    registry = ResearchCandidateRegistry(native_market.store)
    now = datetime(2026, 10, 31, tzinfo=UTC)
    registry.import_verified("sourceExactModel", data, now=now-timedelta(days=4))
    assert len(registry.compatible_boosters(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon=5, now=now,
    )) == 1
    assert registry.compatible_boosters(
        venue="binance", contract="BTCUSDT", quote="USDT", horizon=5, now=now,
    ) == []
    assert registry.compatible_boosters(
        venue="hyperliquid", contract="BTC", quote="USD", horizon=5, now=now,
    ) == []


def test_native_month_export_real_postgresql16(tmp_path):
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL CI service required")
    hot = PostgresHotStore(dsn)
    market = PerpetualMarketStore(hot)
    market.initialize()
    try:
        count = _seed_month(
            market, venue="hyperliquid", contract="XBT_NATIVE_TEST",
            quote="USDC",
        )
        archive = export_source_month(
            market, venue="hyperliquid", contract="XBT_NATIVE_TEST",
            quote="USDC", month="2026-09", root=tmp_path,
            now=datetime(2026, 10, 31, tzinfo=UTC),
        )
        assert archive.records == count
        assert audit_partition(archive.manifest_path)["full_calendar_month_verified"]
        rows = assemble([archive.manifest_path])
        assert rows[0]["contract"] == "XBT_NATIVE_TEST"
        assert rows[0]["venue"] == "hyperliquid"
        assert rows[2]["months"] == ["2026-09"]
    finally:
        with hot.connect() as conn:
            conn.execute(
                """DELETE FROM perp_market_candles
                   WHERE venue=? AND contract=? AND quote=?""",
                ("hyperliquid", "XBT_NATIVE_TEST", "USDC"),
            )
        hot.close()


def test_native_month_readiness_requires_real_archival_receipts(
    native_market, tmp_path,
):
    from parlay_bot.research.source_native import source_month_readiness

    clock = datetime(2026, 10, 31, 12, tzinfo=UTC)
    _seed_month(native_market, venue="hyperliquid", contract="BTC",
                quote="USDC", month=9)
    before = source_month_readiness(
        native_market, venue="hyperliquid", contract="BTC", quote="USDC",
        now=clock,
    )
    assert len(before["months"]) == 6
    september = before["months"][-1]
    assert september["month"] == "2026-09"
    assert september["bars"] == september["expected_bars"]
    assert september["status"] == "count_only_not_certified"
    assert before["months_with_receipts"] == 0
    assert before["receipt_read_available"] is False
    assert before["six_month_receipts_ready"] is False
    assert before["training_approved"] is False

    import asyncio
    publisher = NativeMonthlyPublisher(native_market, _FakeArchiveDrive(), tmp_path)
    published = asyncio.run(publisher.run_once(now=clock, max_batches=2))
    assert len(published) == 1
    after = source_month_readiness(
        native_market, venue="hyperliquid", contract="BTC", quote="USDC",
        now=clock,
    )
    assert after["receipt_read_available"] is True
    assert after["months_with_receipts"] == 1
    assert after["months"][-1]["status"] == "published_receipt"
    assert after["months"][0]["status"] == "incomplete"
    assert after["six_month_receipts_ready"] is False
    assert after["training_approved"] is False


def test_native_month_readiness_never_mixes_contract_or_quote(native_market):
    from parlay_bot.research.source_native import source_month_readiness

    clock = datetime(2026, 10, 31, 12, tzinfo=UTC)
    _seed_month(native_market, venue="hyperliquid", contract="BTC",
                quote="USDC", month=9)
    wrong_quote = source_month_readiness(
        native_market, venue="hyperliquid", contract="BTC", quote="USDT",
        now=clock,
    )
    assert all(row["bars"] == 0 for row in wrong_quote["months"])
    with pytest.raises(ValueError):
        source_month_readiness(
            native_market, venue="binance", contract="BTC", quote="USDC",
            now=clock,
        )
