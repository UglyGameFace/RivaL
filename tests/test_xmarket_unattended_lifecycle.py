"""Unattended XMarket source to candidate must fail closed."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tools import xmarket_unattended_lifecycle as lifecycle


def _sources():
    return {
        "verification": {
            "polymarket": {"status": "verified"},
            "kalshi": {"status": "verified"},
        },
        "receipts": [{"file_id": "PRIVATE-ID", "sha256": "a" * 64}],
    }


def _mock_input(monkeypatch, *, sources=None):
    monkeypatch.setattr(lifecycle, "load_frames_manifests",
        lambda *args, **kwargs: ({"verified": object()},
                                  {"provider_sha256_by_month": {"okx:2026-10": "a" * 64}}))

    async def native(_asset):
        return _sources() if sources is None else sources

    monkeypatch.setattr(lifecycle, "_read_originals", native)


def _inner(days=3, *, measured=False):
    return {"publish_kind": "report", "artifact": None, "report": {
        "training_status": "research_only",
        "outer_validation_test_scored": False,
        "train_only_feature_ablation": {
            "status": "measured_train_only" if measured else "insufficient_train_only_fold",
            "uses_outer_validation_or_test": False, "can_publish_model": False,
            "fold": {"eval_days": days, "eval_targets": 110, "fit_rows": 900},
        },
    }}


def test_canonical_source_clock_keeps_purged_outer_holdouts():
    train, test, through = lifecycle.source_clock(date(2026, 11, 17))
    assert train.isoformat() == "2026-09-22T00:00:00+00:00"
    assert test.isoformat() == "2026-10-20T00:00:00+00:00"
    assert through.isoformat() == "2026-11-16T23:55:00+00:00"
    assert test-train == timedelta(days=28)
    assert through > test
    with pytest.raises(ValueError):
        lifecycle.source_clock(datetime(2026, 11, 17, tzinfo=UTC))


def test_incomplete_inner_folds_cannot_read_outer_holdout_or_publish(monkeypatch, tmp_path):
    _mock_input(monkeypatch)
    calls = []

    def research(**kwargs):
        calls.append((kwargs["horizon"], kwargs.get("research_only")))
        if not kwargs.get("research_only"):
            raise AssertionError("sealed holdout must not be opened")
        return _inner()

    async def forbidden(*_args):
        raise AssertionError("no model may be published")

    monkeypatch.setattr(lifecycle, "train_run", research)
    monkeypatch.setattr(lifecycle, "_publish", forbidden)
    result = lifecycle.evaluate(
        asset="ETH", manifests=tmp_path, as_of=date(2026, 11, 17),
        publish_passing=True,
    )
    assert calls == [(5, True), (15, True), (60, True)]
    assert result["status"] == "awaiting_inner_fold"
    assert result["models_published"] == 0
    assert all(s["missing_inner_days"] == 4 and not s["published"]
               for s in result["scopes"])
    assert "PRIVATE-ID" not in json.dumps(result)
    assert result["outer_holdouts_used_for_feature_selection"] is False


def test_fidelity_failure_prevents_even_inner_training(monkeypatch, tmp_path):
    sources = _sources()
    sources["verification"]["kalshi"]["status"] = "unverified"
    _mock_input(monkeypatch, sources=sources)
    monkeypatch.setattr(lifecycle, "train_run",
                        lambda **kwargs: pytest.fail("unverified native data admitted"))
    result = lifecycle.evaluate(
        asset="BTC", manifests=tmp_path, as_of=date(2026, 11, 17),
        publish_passing=True,
    )
    assert result["status"] == "awaiting_verified_native_sources"
    assert len(result["scopes"]) == 3
    assert result["models_published"] == 0


def test_passed_inner_research_can_evaluate_but_rejected_outer_does_not_publish(
        monkeypatch, tmp_path):
    _mock_input(monkeypatch)
    calls = []

    def train(**kwargs):
        calls.append(bool(kwargs.get("research_only")))
        if kwargs.get("research_only"):
            return _inner(days=7, measured=True)
        return {"publish_kind": "report", "artifact": None,
                "report": {"gate_failures": ["validation_brier_gain<=0.005"]}}

    monkeypatch.setattr(lifecycle, "train_run", train)
    monkeypatch.setattr(lifecycle, "validate",
                        lambda *a: pytest.fail("rejected artifact cannot validate"))
    monkeypatch.setattr(lifecycle, "_publish",
                        lambda *a: pytest.fail("rejected artifact cannot publish"))
    result = lifecycle.evaluate(
        asset="BTC", manifests=tmp_path, as_of=date(2026, 11, 17),
        publish_passing=True,
    )
    assert calls == [True, False] * 3
    assert result["models_published"] == 0
    assert result["status"] == "offline_candidates_rejected"
    assert all(s["status"] == "offline_gates_failed" for s in result["scopes"])


def test_only_validated_offline_artifact_publishes_privately(monkeypatch, tmp_path):
    _mock_input(monkeypatch)
    passed, private_path = [], []

    def train(**kwargs):
        if kwargs.get("research_only"):
            return _inner(days=7, measured=True)
        return {"publish_kind": "model",
                "artifact": {"weights": [0.1, 0.2], "private": "NEVER-IN-REPORT"}}

    async def publish(kind, path):
        assert kind == "model"
        private_path.append(str(path))
        assert "NEVER-IN-REPORT" in Path(path).read_text()
        passed.append(kind)
        return {"kind": "model", "file_id": "PRIVATE-ID"}

    monkeypatch.setattr(lifecycle, "train_run", train)
    monkeypatch.setattr(lifecycle, "validate", lambda doc: doc)
    monkeypatch.setattr(lifecycle, "_publish", publish)
    result = lifecycle.evaluate(
        asset="XRP", manifests=tmp_path, as_of=date(2026, 11, 17),
        publish_passing=True,
    )
    assert len(passed) == 3
    assert result["models_published"] == 3
    assert all(s["published"] and s["offline_gate_passed"] for s in result["scopes"])
    assert all(not Path(p).exists() for p in private_path)
    assert "PRIVATE-ID" not in json.dumps(result)
    assert "NEVER-IN-REPORT" not in json.dumps(result)
    assert result["real_money_orders"] == 0


def test_candidate_without_publish_flag_stays_private(monkeypatch, tmp_path):
    _mock_input(monkeypatch)
    monkeypatch.setattr(lifecycle, "train_run", lambda **kwargs: (
        _inner(days=7, measured=True) if kwargs.get("research_only")
        else {"publish_kind": "model", "artifact": {"weights": [1]}}
    ))
    monkeypatch.setattr(lifecycle, "validate", lambda doc: doc)

    async def forbidden(*args):
        raise AssertionError("no implicit publication")

    monkeypatch.setattr(lifecycle, "_publish", forbidden)
    result = lifecycle.evaluate(
        asset="SOL", manifests=tmp_path, as_of=date(2026, 11, 17),
        publish_passing=False,
    )
    assert result["models_published"] == 0
    assert result["status"] == "offline_candidates_eligible"


def test_misleading_inner_research_artifact_is_rejected(monkeypatch, tmp_path):
    _mock_input(monkeypatch)
    monkeypatch.setattr(lifecycle, "train_run",
                        lambda **kwargs: {
                            **_inner(days=7, measured=True),
                            "artifact": {"weights": [1]},
                        })
    with pytest.raises(ValueError, match="inner research attempted"):
        lifecycle.evaluate(asset="BTC", manifests=tmp_path, as_of=date(2026, 11, 17))


def test_command_reports_only_error_class(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lifecycle, "evaluate",
                        lambda **kwargs: (_ for _ in ()).throw(
                            RuntimeError("private-token source quote contract")))
    path = tmp_path / "safe.json"
    assert lifecycle.main([
        "--asset", "BTC", "--manifests-dir", str(tmp_path),
        "--as-of", "2026-11-16", "--out", str(path),
    ]) == 1
    serialized = path.read_text() + capsys.readouterr().out
    assert "RuntimeError" in serialized
    assert "private-token" not in serialized
    assert "source quote contract" not in serialized
