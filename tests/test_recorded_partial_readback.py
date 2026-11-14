"""Post-merge private-candle readback proof stays safe and independently testable."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools import verify_recorded_partial_restore as proof

NOW = datetime(2026, 11, 14, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_verified_original_restoration_reports_only_public_safe_counts(monkeypatch, tmp_path):
    events = []

    class Drive:
        async def aclose(self):
            events.append("closed")

    monkeypatch.setattr(proof, "Settings", lambda: object())
    monkeypatch.setattr(proof, "_drive", lambda _: Drive())

    async def restore(_drive, *, asset, root, first_day, last_day, now):
        assert asset == "BTC" and root == tmp_path
        assert str(first_day) == "2026-11-07"
        assert str(last_day) == "2026-11-13"
        assert now == NOW
        events.append("restore")
        return {
            "restored": [str(tmp_path / "original.manifest.json")],
            "source_files": [{"sha256": "secret-source-digest"}],
            "missing_backup_days": ["2026-11-09"],
            "partitions": [{"unknown_trade_count": 8}],
        }

    def audit(path):
        events.append("audit")
        assert path.name == "original.manifest.json"
        return {
            "venue": "bitget", "contract": "BTCUSDT", "month": "2026-11",
            "rows": 18, "full_calendar_month_verified": False,
        }

    monkeypatch.setattr(proof, "restore_recorded_partitions", restore)
    monkeypatch.setattr(proof, "audit_partition", audit)
    result = await proof.verify(asset="BTC", root=tmp_path, now=NOW, lookback_days=7)
    assert events == ["restore", "audit", "closed"]
    assert result["status"] == "verified_original_readback"
    assert result["source_backup_days"] == 1
    assert result["missing_backup_days"] == 1
    assert result["unknown_trade_count"] == 8
    assert result["partitions"][0]["rows"] == 18
    assert not result["trained_model"]
    assert not result["eligible_historical_splits_proven"]
    assert not result["private_drive_modified"]
    assert "secret-source-digest" not in json.dumps(result)


@pytest.mark.asyncio
async def test_absent_originals_are_not_false_success(monkeypatch, tmp_path):
    class Drive:
        closed = False

        async def aclose(self):
            self.closed = True

    drive = Drive()
    monkeypatch.setattr(proof, "Settings", lambda: object())
    monkeypatch.setattr(proof, "_drive", lambda _: drive)

    async def restore(*_args, **_kwargs):
        return {"restored": [], "source_files": [], "missing_backup_days": [],
                "partitions": []}

    monkeypatch.setattr(proof, "restore_recorded_partitions", restore)
    result = await proof.verify(asset="ETH", root=tmp_path, now=NOW, lookback_days=1)
    assert drive.closed
    assert result["status"] == "no_eligible_originals"
    assert not result["original_sha_readback_checked"]


def test_cli_failure_writes_safe_receipt_without_exposing_provider_payload(
    monkeypatch, tmp_path, capsys,
):
    async def failing(**_kwargs):
        raise ValueError("invalid recorded candle original: invalid recorded candle trade count; private-secret")

    monkeypatch.setattr(proof, "verify", failing)
    out = tmp_path / "receipt.json"
    assert proof.main([
        "--asset", "XRP", "--root", str(tmp_path / "private"),
        "--out", str(out),
    ]) == 1
    result = json.loads(out.read_text())
    assert result["status"] == "restoration_failed"
    assert result["error_class"] == "ValueError"
    assert result["error_category"] == "invalid_trade_count"
    assert "private-secret" not in out.read_text() + capsys.readouterr().out



@pytest.mark.asyncio
async def test_exact_xmarket_admission_uses_only_sha_verified_native_and_original_frames(
    monkeypatch, tmp_path,
):
    import hashlib

    from parlay_bot.research import archive_training, xmarket_history, xmarket_training

    raw = json.dumps({"original_source": "token-never-print"}).encode()
    calls = []

    class Drive:
        async def read_verified_research_reports(self, **kwargs):
            calls.append(("read", kwargs))
            assert kwargs["return_next_page"] and kwargs["limit"] == 20
            assert kwargs["name_prefix"].startswith("xmarket-inputs-BTC-")
            return [("private-drive-id", raw)], None

    def frames(root, *, target_asset):
        import pandas as pd

        calls.append(("load_frames", target_asset))
        assert root == tmp_path
        table = pd.DataFrame(index=pd.date_range("2026-11-08", periods=2, tz="UTC"))
        return {"bitget|BTCUSDT|USDT": (table, "causal-features")}, {
            "recorded_partitions": {"bitget:2026-11": "secret-source-hash"},
        }

    monkeypatch.setattr(archive_training, "load_frames_manifests", frames)
    monkeypatch.setattr(xmarket_history, "validate_bundle", lambda doc: {
        "asset": "BTC", "samples": [], **doc,
    })

    def combine(bundles):
        assert len(bundles) == 1
        assert bundles[0]["_drive_file_id"] == "private-drive-id"
        assert bundles[0]["_bundle_sha256"] == hashlib.sha256(raw).hexdigest()
        return {"samples": [{"source": source, "at": "2026-11-08T00:05:00+00:00"}
                            for source in ("polymarket", "kalshi")],
                "verification": {"polymarket": {"status": "verified"},
                                 "kalshi": {"status": "verified"}}}

    monkeypatch.setattr(xmarket_history, "combine", combine)

    def run(frames, *, asset, horizon, market_data, provenance,
            train_end, test_start, trained_through, code_version,
            admission_only):
        assert admission_only and asset == "BTC"
        assert len(frames) == 1 and len(market_data["samples"]) == 2
        assert market_data["asset"] == "BTC"
        assert (train_end.isoformat(), test_start.isoformat()) == (
            "2026-09-19T00:00:00+00:00", "2026-10-17T00:00:00+00:00",
        )
        assert trained_through.isoformat() == "2026-11-13T23:55:00+00:00"
        assert code_version == "read-only-original-admission"
        assert provenance["recorded_partitions"]
        calls.append(("admit", horizon))
        return {
            "artifact": None, "report": {
                "training_status": "admission_only", "input_count": 18,
                "dispersion_venues": ["bitget", "okx"],
                "row_admission": {
                    "bitget|BTCUSDT|USDT": {"complete_rows": 50 + horizon},
                },
                "overlap_evidence": {
                    "eligible": False,
                    "by_stream": {"bitget|BTCUSDT|USDT": {"all_sources_rows": 1780}},
                    "splits": {
                        "train": {"rows": 0, "shortfall": 300, "unique_bars": 0},
                        "validation": {"rows": 0, "shortfall": 100, "unique_bars": 0},
                        "test": {"rows": 55, "shortfall": 45, "unique_bars": 55},
                    },
                },
                "effective_window": {"rows": 55},
            },
        }

    monkeypatch.setattr(xmarket_training, "train_run", run)
    receipt = await proof.verify_xmarket_admission(
        Drive(), asset="BTC", root=tmp_path, now=NOW,
    )
    assert receipt["status"] == "exact_features_joined"
    assert receipt["verified_native_fidelity"] == {
        "polymarket": True, "kalshi": True,
    }
    assert receipt["verified_source_bundles"] == 1
    assert receipt["horizons"]["5"]["split_rows"]["train"]["shortfall"] == 300
    assert receipt["horizons"]["15"]["split_rows"]["validation"]["rows"] == 0
    assert receipt["horizons"]["60"]["effective_joined_rows"] == 55
    assert receipt["native_source_windows"]["polymarket"]["points"] == 1
    assert receipt["candle_frame_windows"]["bitget|BTCUSDT|USDT"]["rows"] == 2
    assert [e for e in calls if e[0] == "admit"] == [
        ("admit", 5), ("admit", 15), ("admit", 60),
    ]
    assert "private-drive-id" not in json.dumps(receipt)
    assert "secret-source-hash" not in json.dumps(receipt)
    assert receipt["training_or_model_published"] is False


@pytest.mark.asyncio
async def test_xmarket_no_source_bundles_is_not_reported_as_admitted(monkeypatch, tmp_path):
    from parlay_bot.research import archive_training

    # Restore loader returns both frames and provenance.
    monkeypatch.setattr(
        archive_training, "load_frames_manifests",
        lambda root, *, target_asset: (
            {"bitget|BTCUSDT|USDT": ("candles", "features")}, {},
        ),
    )

    class Drive:
        async def read_verified_research_reports(self, **kwargs):
            return [], None

    receipt = await proof.verify_xmarket_admission(
        Drive(), asset="BTC", root=tmp_path, now=NOW,
    )
    assert receipt["status"] == "no_verified_native_source_bundles"
    assert receipt["training_or_model_published"] is False


def test_cli_fails_closed_when_originals_pass_but_xmarket_has_no_complete_frames(
    monkeypatch, tmp_path,
):
    async def proof_without_usable_join(**kwargs):
        assert kwargs["xmarket_admission"]
        return {"status": "verified_original_readback", "xmarket_admission": {
            "status": "no_complete_source_and_label_rows",
        }}

    monkeypatch.setattr(proof, "verify", proof_without_usable_join)
    out = tmp_path / "report.json"
    assert proof.main([
        "--asset", "BTC", "--root", str(tmp_path / "original"),
        "--out", str(out), "--xmarket-admission",
    ]) == 3
    assert json.loads(out.read_text())["xmarket_admission"]["status"] == (
        "no_complete_source_and_label_rows"
    )


def test_optional_xmarket_runner_dependency_errors_are_safe_and_precise():
    assert proof._category(ModuleNotFoundError("No module named pandas", name="pandas")) == (
        "missing_xmarket_runner_dependency"
    )
    assert proof._category(ModuleNotFoundError("private source", name="private_name")) == (
        "missing_runner_module"
    )


@pytest.mark.asyncio
async def test_xmarket_rejects_a_lookback_that_cannot_reach_training_before_drive(monkeypatch, tmp_path):
    def forbidden(*_args):
        raise AssertionError("private Drive must not be contacted for a truncated check")

    monkeypatch.setattr(proof, "_drive", forbidden)
    with pytest.raises(ValueError, match="all three chronological windows"):
        await proof.verify(asset="BTC", root=tmp_path, now=NOW, lookback_days=28,
                           xmarket_admission=True)


@pytest.mark.asyncio
async def test_xmarket_uses_production_months_and_keeps_private_restore_details_local(
    monkeypatch, tmp_path,
):
    from parlay_bot.research import archive_training

    events = []

    class Drive:
        async def aclose(self):
            events.append("closed")

    drive = Drive()
    monkeypatch.setattr(proof, "Settings", lambda: object())
    monkeypatch.setattr(proof, "_drive", lambda _: drive)

    async def production_restore(**kwargs):
        assert kwargs["drive"] is drive
        assert kwargs["months"] == [f"2026-{month:02}" for month in range(5, 11)]
        assert str(kwargs["records_since"]) == "2026-08-16"
        assert str(kwargs["records_through"]) == "2026-11-13"
        events.append("production_restore")
        (tmp_path / "complete.manifest.json").write_text("{}")
        return {
            "months_requested": kwargs["months"],
            "restored": {"okx": ["private-id/original-path"]},
            "excluded": {"recorded-partial-partitions": {
                "reason": "HistoryImportError", "detail": "private-token-and-file-id",
            }},
        }

    monkeypatch.setattr(archive_training, "restore_verified_months", production_restore)
    monkeypatch.setattr(proof, "audit_partition", lambda _: {
        "venue": "okx", "contract": "BTC-USDT-SWAP", "month": "2026-09",
        "rows": 8640, "full_calendar_month_verified": True,
    })

    async def admission(*_args, **_kwargs):
        events.append("admission")
        return {"status": "exact_features_joined", "verified_native_fidelity": {
            "polymarket": True, "kalshi": True,
        }, "horizons": {str(h): {"split_eligible": True} for h in (5, 15, 60)}}

    monkeypatch.setattr(proof, "verify_xmarket_admission", admission)
    result = await proof.verify(asset="BTC", root=tmp_path, now=NOW, lookback_days=90,
                                xmarket_admission=True)
    assert events == ["production_restore", "admission", "closed"]
    assert result["status"] == "verified_original_readback"
    assert result["partitions"][0]["full_calendar_month_verified"] is True
    assert result["recorded_backup_inventory_available"] is False
    assert result["input_restoration"]["excluded_reason_classes"] == {
        "recorded-partial-partitions": "HistoryImportError",
    }
    assert result["eligible_historical_splits_proven"] is True, result["xmarket_admission"]
    assert result["trained_model"] is False
    assert "private-" not in json.dumps(result)


@pytest.mark.asyncio
async def test_real_originals_reach_all_splits_through_production_restore_without_a_fit(
    monkeypatch, tmp_path,
):
    from datetime import timedelta

    from parlay_bot.perpetuals import flow_challenger_v3
    from parlay_bot.research import (
        bitget_source_bridge,
        source_native,
        xmarket_history,
        xmarket_training,
    )
    from tests.test_recorded_candle_restoration import Drive as OriginalDrive
    from tests.test_recorded_candle_restoration import candle
    from tests.xmarket_fixtures import point

    rows, samples = [], []
    for day in ("2026-09-01", "2026-09-25", "2026-11-08"):
        start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        # Preserve the production 420-bar warm-up after every history gap.
        for i in range(720):
            at = start + timedelta(minutes=5 * i)
            rows.extend(candle(at, venue=venue, close=100 + i % 20)
                        for venue in ("bitget", "okx", "gate"))
            samples.extend(point(source, at + timedelta(minutes=5), .51 + i % 3 * .01,
                                 origin=origin)
                           for source in ("polymarket", "kalshi")
                           for origin in ("live", "backfill"))

    raw = json.dumps({"schema": xmarket_history.BUNDLE_SCHEMA, "asset": "BTC",
                      "samples": samples, "live_venues": ["bitget", "okx", "gate"]}).encode()

    class Drive(OriginalDrive):
        async def read_verified_research_reports(self, **kwargs):
            assert kwargs["name_prefix"] == xmarket_history.BUNDLE_PREFIX + "BTC-"
            return [("private-native-source-id", raw)], None

        async def aclose(self):
            self.calls.append("closed")

    drive = Drive(rows)
    monkeypatch.setattr(proof, "Settings", lambda: object())
    monkeypatch.setattr(proof, "_drive", lambda _: drive)
    monthly_calls = []

    async def absent_complete_months(client, **kwargs):
        assert client is drive
        monthly_calls.append(kwargs)
        return []  # Actual SHA-verified daily originals remain available.

    monkeypatch.setattr(bitget_source_bridge, "restore_verified_bitget_market_months",
                        absent_complete_months)
    monkeypatch.setattr(source_native, "restore_verified_source_months", absent_complete_months)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("input acceptance must never fit or calibrate")

    monkeypatch.setattr(xmarket_training, "_fit_logistic", forbidden)
    monkeypatch.setattr(flow_challenger_v3, "fit_platt", forbidden)

    # Reproduce the old blind spot with the very same original source bytes.
    short_root = tmp_path / "short"
    await proof.verify(asset="BTC", root=short_root, now=NOW, lookback_days=28)
    short = await proof.verify_xmarket_admission(drive, asset="BTC", root=short_root, now=NOW)
    assert short["horizons"]["5"]["split_rows"]["train"]["rows"] == 0
    assert short["horizons"]["5"]["split_rows"]["validation"]["rows"] == 0

    result = await proof.verify(asset="BTC", root=tmp_path / "production", now=NOW,
                                lookback_days=90, xmarket_admission=True)
    assert len(monthly_calls) == 3
    assert all(len(call["months"]) == 6 for call in monthly_calls)
    assert drive.calls[-1] == "closed"
    assert result["eligible_historical_splits_proven"] is True, result["xmarket_admission"]
    assert result["trained_model"] is False
    assert result["private_drive_modified"] is False
    assert result["production_database_modified"] is False
    admission = result["xmarket_admission"]
    assert admission["verified_native_fidelity"] == {"polymarket": True, "kalshi": True}
    assert admission["native_source_windows"]["polymarket"]["first_utc"].startswith("2026-09")
    for horizon in (5, 15, 60):
        verdict = admission["horizons"][str(horizon)]
        assert verdict["input_features"] == 18
        assert verdict["split_eligible"] is True
        assert all(part["shortfall"] == 0 for part in verdict["split_rows"].values())
        assert not verdict["model_fitted"] and not verdict["model_published"]
    assert "private-native-source-id" not in json.dumps(result)


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.asyncio
async def test_production_restorer_closes_only_owned_clients_after_source_failure(
    monkeypatch, tmp_path, injected,
):
    from parlay_bot.research import (
        archive_training,
        bitget_source_bridge,
        publish,
        recorded_candles,
        source_native,
    )

    class Drive:
        closed = False

        async def aclose(self):
            self.closed = True

    drive = Drive()
    created = []

    def create(_settings):
        created.append(True)
        return drive

    async def absent(*_args, **_kwargs):
        return []

    async def failed(*_args, **_kwargs):
        raise ValueError("invalid original bytes")

    monkeypatch.setattr(publish, "_drive", create)
    monkeypatch.setattr(bitget_source_bridge, "restore_verified_bitget_market_months", absent)
    monkeypatch.setattr(source_native, "restore_verified_source_months", absent)
    monkeypatch.setattr(recorded_candles, "restore_recorded_partitions", failed)
    result = await archive_training.restore_verified_months(
        asset="BTC", months=["2026-09"], root=tmp_path, now=NOW,
        **({"drive": drive} if injected else {}),
    )
    assert created == ([] if injected else [True])
    assert drive.closed is (not injected)
    assert result["excluded"]["recorded-partial-partitions"]["reason"] == "ValueError"


def test_independent_readback_never_changes_training_trigger_or_uploads_originals():
    root = Path(__file__).resolve().parents[1] / ".github/workflows"
    yaml = (root / "recorded-partial-readback.yml").read_text()
    assert 'branches: [main]' in yaml
    assert '"src/parlay_bot/research/recorded_candles.py"' in yaml
    assert '"tools/verify_recorded_partial_restore.py"' in yaml
    assert "asset: [BTC, ETH, SOL, XRP]" in yaml
    assert "max-parallel: 2" in yaml
    assert "group: rival-recorded-original-readback" in yaml
    assert "verify_recorded_partial_restore.py" in yaml
    assert "--xmarket-admission" in yaml
    assert "--lookback-days 90" in yaml
    assert '"src/parlay_bot/research/archive_training.py"' in yaml
    assert '"pandas>=2.2,<4"' in yaml
    assert '"numpy>=1.26,<3"' in yaml
    assert "import pandas, numpy" in yaml
    assert yaml.index("import pandas, numpy") < yaml.index("--xmarket-admission")
    assert "name: recorded-partial-proof-" in yaml
    assert "path: data/native-reports/recorded-partial-proof-" in yaml
    assert "if: always()" in yaml
    assert "data/recorded-private-originals" not in yaml.split(
        "- name: Upload concise restoration verdict only", 1
    )[1]
    assert "--publish" not in yaml

    trainer = (root / "archive-model-training.yml").read_text()
    push = trainer.split("  push:\n", 1)[1].split("  workflow_dispatch:\n", 1)[0]
    paths = [line.strip().removeprefix('- "').removesuffix('"')
             for line in push.splitlines() if line.strip().startswith('- "')]
    assert paths == [
        ".github/workflows/archive-model-training.yml",
        "src/parlay_bot/research/xmarket_training.py",
    ]
    assert "should_run=false" in trainer
