"""Only historically screened research enters the model importer feed."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from parlay_bot.perpetuals.model_checkpoints import (
    FEATURE_CONTRACT, TARGET_CONTRACT, canonical,
)
from parlay_bot.research import source_native_offline as native
from tools.native_acceptance_status import render_status


def _evaluation(*, eligible: bool) -> dict:
    months = [f"2026-{month:02d}" for month in range(5, 11)]
    identity = {
        "venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT",
        "interval_minutes": 5,
    }
    spec = {
        "model": "offline-live-contract-logistic-v3",
        "identity": identity, "horizon_minutes": 5,
        "feature_contract": FEATURE_CONTRACT, "target_contract": TARGET_CONTRACT,
        "training_count": 1234, "training_cutoff": "2026-07-01T00:00:00+00:00",
        "validation_cutoff": "2026-09-01T00:00:00+00:00",
        "evaluation_completed_through": "2026-10-31T23:55:00+00:00",
        "weights": [0.01] * 11, "train_base_rate": .5,
        "source_sha256_by_month": {
            month: hashlib.sha256(month.encode()).hexdigest() for month in months
        },
    }
    gain = .01 if eligible else .001
    return {
        "model_spec": spec,
        "model_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "validation": {
            "count": 240, "brier_gain": gain, "ece_10": .05, "days": 25,
            "brier": .25 - gain, "baseline_brier": .25,
        },
        "out_of_sample_test": {
            "count": 240, "brier_gain": gain, "ece_10": .05, "days": 25,
            "brier": .25 - gain, "baseline_brier": .25,
        },
        "research_candidate_passed": eligible,
    }


@pytest.mark.parametrize("eligible", [False, True])
@pytest.mark.asyncio
async def test_qualification_controls_drive_folder_without_losing_artifacts(
    tmp_path, monkeypatch, eligible,
):
    months = [f"2026-{month:02d}" for month in range(5, 11)]
    stream = {
        "venue": "okx", "contract": "BTC-USDT-SWAP", "quote": "USDT",
        "interval_minutes": 5,
    }

    async def restore(*_args, **_kwargs):
        return [object()] * 6

    monkeypatch.setattr(native, "restore_verified_source_months", restore)
    monkeypatch.setattr(native, "audit_catalog", lambda *_args, **_kwargs: {
        "streams": [{**stream, "months_present": 6, "research_grade": True,
                     "missing_months": []}],
        "partitions": [
            {**stream, "month": month, "full_calendar_month_verified": True}
            for month in months
        ],
        "validation_failures": [],
    })
    model = _evaluation(eligible=eligible)
    monkeypatch.setattr(native, "evaluate_logistic",
                        lambda *_args, **_kwargs: [model])
    monkeypatch.setattr(native, "evaluate_boosters",
                        lambda *_args, **_kwargs: [])
    calls = []

    class Drive:
        async def ensure_research_output(self, *, artifact, batch_id, kind):
            assert artifact.sha256 and batch_id
            calls.append((kind, artifact.file_name))
            return SimpleNamespace(file_id=f"verified_{len(calls)}")

    result = await native.run_native_research(
        drive=Drive(), venue="okx", months=months,
        root=tmp_path / "archive", report_dir=tmp_path / "reports",
        publish=True,
    )
    expected = "verified_research_published" if eligible else (
        "verified_research_archived_no_qualified_models"
    )
    assert result["status"] == expected
    assert result["published_models"] == int(eligible)
    assert result["archived_rejected_models"] == int(not eligible)
    assert result["published_research_artifacts"] == 1
    assert [kind for kind, _ in calls] == [
        "model" if eligible else "report", "report",
    ]
    assert result["actual_registry_screening"]["evaluated"] == 1
    assert result["actual_registry_screening"]["retrospective_passed"] == int(eligible)
    row = result["actual_registry_screening"]["source_specific_outcomes"][0]
    assert row["venue"] == "okx"
    assert row["validation_brier_gain"] == (.01 if eligible else .001)
    assert row["holdout_brier_gain"] == (.01 if eligible else .001)
    assert not result["promoted_live"]


def test_zero_qualifiers_report_cannot_claim_imported_models():
    body = render_status({
        "venue": "okx",
        "status": "verified_research_archived_no_qualified_models",
        "published_models": 0,
        "archived_rejected_models": 48,
        "restored_manifests": 24,
        "actual_registry_screening": {
            "evaluated": 48, "retrospective_passed": 0,
            "rejected_by_reason": [{
                "reason": "validation_brier_gain_below_0.005", "count": 48,
            }],
        },
    }, "https://github.com/UglyGameFace/RivaL/actions/runs/123")
    assert "NO QUALIFIED HISTORICAL MODEL" in body
    assert "48 rejected" in body
    assert "0/48 passed" in body
    assert "validation_brier_gain_below_0.005" in body
    assert "NOT published for model import" in body
    assert "NO CALL" in body


def test_archive_only_cli_needs_verified_screening_counts(monkeypatch, capsys):
    class Drive:
        async def aclose(self):
            pass

    monkeypatch.setattr(native, "Settings", lambda: object())
    monkeypatch.setattr(native, "_drive", lambda _: Drive())
    report = {
        "status": "verified_research_archived_no_qualified_models",
        "archived_rejected_models": 48, "published_models": 0,
        "actual_registry_screening": {
            "evaluated": 48, "retrospective_passed": 0,
        },
    }

    async def research(**kwargs):
        assert kwargs["publish"] is True
        return report

    monkeypatch.setattr(native, "run_native_research", research)
    assert native.main(["--venue", "okx", "--publish"]) == 0
    assert json.loads(capsys.readouterr().out)["published_models"] == 0
    report["actual_registry_screening"]["retrospective_passed"] = 1
    assert native.main(["--venue", "okx", "--publish"]) == 2
    report["actual_registry_screening"]["retrospective_passed"] = 0
    report["archived_rejected_models"] = 0
    assert native.main(["--venue", "okx", "--publish"]) == 2
