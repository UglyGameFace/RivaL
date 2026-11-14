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
