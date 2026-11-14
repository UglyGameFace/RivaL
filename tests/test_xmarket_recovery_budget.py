"""Catch up native comparison pairs without increasing the acquisition budget."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import xmarket_history as recovery
from tests.xmarket_fixtures import NOW, point


def live_bundle(contracts):
    close = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC)
    samples = [
        point(source, close - timedelta(minutes=15 * i) - timedelta(minutes=15 - minute))
        for source in ("polymarket", "kalshi")
        for i in range(contracts)
        for minute in (5, 10, 15)
    ]
    return {"schema": recovery.BUNDLE_SCHEMA, "asset": "BTC", "samples": samples}


@pytest.fixture
def native_provider(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(recovery, "datetime", Clock)
    collected, lookups = [], []

    def identity(source, opened):
        row = point(source, opened + timedelta(minutes=5))
        return {key: row[key] for key in (
            "asset", "contract_id", "opens_at", "closes_at", "token_id", "slug"
        ) if key in row}

    def gamma(*args, **kwargs):
        lookups.append("polymarket")
        return {}

    def kalshi(client, url, params, **kwargs):
        lookups.append("kalshi")
        assert "/historical/" not in url
        close = datetime.fromtimestamp(params["min_close_ts"] + 1, UTC)
        opened = close - timedelta(minutes=15)
        return {"markets": [{"ticker": identity("kalshi", opened)["contract_id"],
                             "open_time": opened.isoformat(), "close_time": close.isoformat()}]}

    def collect(client, *, source, identity, root, now, fidelity=1):
        opened = recovery.timestamp(identity["opens_at"])
        collected.append((source, recovery.timestamp(identity["closes_at"])))
        return [point(source, opened + timedelta(minutes=minute), origin="backfill")
                for minute in (5, 10, 15)], SimpleNamespace()

    monkeypatch.setattr(recovery, "_json_get", gamma)
    monkeypatch.setattr(recovery, "_identity_pm",
                        lambda event, *, asset, opens: identity("polymarket", opens))
    monkeypatch.setattr(recovery, "kalshi_public_json", kalshi)
    monkeypatch.setattr(recovery, "collect_contract", collect)
    before = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC) - timedelta(days=5)
    return SimpleNamespace(root=tmp_path, before=before, collected=collected,
                           lookups=lookups, collect=collect)


def test_bulk_export_crosses_the_unchanged_fidelity_gate_within_the_old_budget(native_provider):
    bundle = live_bundle(64)
    original = json.dumps(bundle, sort_keys=True)
    # With the original 32-contract allocation, only half the observed
    # feature bars receive independent twins, below the unchanged 80% gate.
    old_pairs = []
    for source in ("polymarket", "kalshi"):
        selected = recovery.missing_overlap_identities(
            [bundle], asset="BTC", source=source, now=NOW, limit=32,
        )
        identities = {row["contract_id"] for row in selected}
        old_pairs.extend({**row, "origin": "backfill"} for row in bundle["samples"]
                         if row["source"] == source and row["contract_id"] in identities)
    old_checks = recovery.compare_history(bundle["samples"], old_pairs)
    assert all(v["overlap_coverage"] == .5 and v["status"] == "unverified"
               for v in old_checks.values())

    budget = {}
    history, publications, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[bundle], root=native_provider.root,
        before=native_provider.before, max_windows=96, retry_gaps=0, budget_report=budget,
    )
    checks = recovery.compare_history(bundle["samples"], history)
    assert all(v["overlap_coverage"] == 1 and v["status"] == "verified"
               and v["mismatches"] == 0 for v in checks.values())
    assert budget["overlap_contracts_selected"] == {"polymarket": 64, "kalshi": 64}
    assert budget["allocated_new_window_budget"] == 64
    assert cursor == native_provider.before - timedelta(minutes=15 * 64)
    assert budget["contract_attempts"] == len(native_provider.collected) == 256
    assert budget["contract_attempts"] <= budget["contract_attempt_limit"] == 2 * (96 + 32)
    assert len(publications) == 256 and len(history) == 3 * len(publications)
    assert not errors
    assert json.dumps(bundle, sort_keys=True) == original, "original observations stay immutable"


@pytest.mark.parametrize("max_windows,live_contracts,overlap,older", [
    (0, 150, 32, 0),
    (1, 150, 32, 1),
    (24, 150, 32, 24),
    (48, 150, 56, 24),
    (96, 150, 96, 32),
    (200, 150, 96, 136),
    (96, 0, 0, 96),
    (96, 12, 12, 96),
])
def test_budget_ceiling_and_reserved_older_history_progress(
    native_provider, max_windows, live_contracts, overlap, older,
):
    budget = {}
    _, _, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[live_bundle(live_contracts)], root=native_provider.root,
        before=native_provider.before, max_windows=max_windows, retry_gaps=0,
        budget_report=budget,
    )
    assert budget["overlap_contracts_selected"] == {
        "polymarket": overlap, "kalshi": overlap,
    }
    assert budget["allocated_new_window_budget"] == older
    assert older >= min(max_windows, 24)
    assert cursor == native_provider.before - timedelta(minutes=15 * older)
    assert len(native_provider.collected) == budget["contract_attempts"] == 2 * (older + overlap)
    assert budget["contract_attempts"] <= budget["contract_attempt_limit"] == 2 * (max_windows + 32)
    assert not errors


def test_one_verified_contract_is_not_downloaded_again_for_gap_and_cursor(native_provider):
    bundle = live_bundle(1)
    close = recovery.timestamp(bundle["samples"][0]["closes_at"])
    pending = {s: {close.isoformat()} for s in ("polymarket", "kalshi")}
    budget = {}
    rows, publications, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[bundle], root=native_provider.root, before=close,
        max_windows=1, pending=pending, retry_gaps=2, budget_report=budget,
    )
    assert len(native_provider.collected) == len(publications) == 2
    assert len(rows) == 6 and not native_provider.lookups
    assert cursor == close - timedelta(minutes=15)
    assert pending == {"polymarket": set(), "kalshi": set()}
    assert budget["contract_attempts"] == 2 and not errors
    # Reusing within a pass does not fabricate additional independent bars.
    assert all(v["paired_points"] == 3 and v["status"] == "unverified"
               for v in recovery.compare_history(bundle["samples"], rows).values())


def test_gap_rechecks_still_fit_the_original_total_attempt_ceiling(native_provider):
    pending = {source: {
        (native_provider.before - timedelta(days=10, minutes=15 * i)).isoformat()
        for i in range(6)
    } for source in ("polymarket", "kalshi")}
    budget = {}
    _, _, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[live_bundle(150)], root=native_provider.root,
        before=native_provider.before, max_windows=96, pending=pending,
        retry_gaps=12, budget_report=budget,
    )
    assert pending == {"polymarket": set(), "kalshi": set()}
    assert budget["contract_attempts"] == len(native_provider.collected) == 268
    assert budget["contract_attempts"] == budget["contract_attempt_limit"]
    assert sum(budget["contract_attempts_by_stage"]["gap_repair"].values()) == 12
    assert cursor == native_provider.before - timedelta(minutes=15 * 32)
    assert not errors


@pytest.mark.asyncio
async def test_actual_weekly_source_entry_preserves_budget_receipt_and_cursor(native_provider):
    result = await recovery.weekly_inputs(
        None, asset="BTC", through=NOW, cache=native_provider.root / "cache", max_windows=2,
    )
    progress = json.loads(json.dumps(result))["backfill_progress"]
    assert progress["contract_attempt_limit"] == 2 * (32 + 2) + 12
    assert progress["contract_attempts"] == len(native_provider.collected) == 4
    assert progress["allocated_new_window_budget"] == progress["new_window_budget"] == 2
    assert progress["windows_advanced"] == 2
    assert progress["contract_attempts_by_stage"]["backfill"] == {
        "polymarket": 2, "kalshi": 2,
    }
    # Actual native source publication/checkpoints still cannot mint a fit.
    assert progress["all_history_verified"] is False
    assert all(v["status"] == "unverified" for v in result["verification"].values())
    assert "artifact" not in result


def test_overlap_throttle_pauses_that_provider_in_later_phases(native_provider, monkeypatch):
    throttled = []

    def collect(client, *, source, **kwargs):
        if source == "polymarket":
            throttled.append(source)
            response = httpx.Response(429, request=httpx.Request("GET", "https://source.test"))
            raise httpx.HTTPStatusError("private-response-never-print", request=response.request,
                                        response=response)
        return native_provider.collect(client, source=source, **kwargs)

    monkeypatch.setattr(recovery, "collect_contract", collect)
    pending = {s: {native_provider.before.isoformat()} for s in ("polymarket", "kalshi")}
    budget = {}
    _, _, cursor, errors = recovery.backfill(
        None, asset="BTC", bundles=[live_bundle(3)], root=native_provider.root,
        before=native_provider.before, max_windows=2, pending=pending,
        retry_gaps=2, budget_report=budget,
    )
    assert throttled == ["polymarket"]
    assert "polymarket" not in native_provider.lookups
    assert budget["paused_sources"] == ["polymarket"]
    assert budget["contract_attempts_by_stage"]["overlap"]["polymarket"] == 1
    assert budget["contract_attempts_by_stage"]["gap_repair"]["polymarket"] == 0
    assert budget["contract_attempts_by_stage"]["backfill"]["polymarket"] == 0
    assert cursor == native_provider.before, "do not advance over an unavailable source"
    assert pending["polymarket"] == {native_provider.before.isoformat()}
    assert errors[0]["failure_code"] == "http_429"
    assert "private-response" not in json.dumps(errors)


@pytest.mark.parametrize("invalid", [-1, 8641, True, 1.5])
def test_invalid_budget_fails_before_any_provider_access(native_provider, invalid):
    with pytest.raises(ValueError, match="max-windows must be 0..8640"):
        recovery.backfill(None, asset="BTC", bundles=[], root=native_provider.root,
                          before=NOW, max_windows=invalid)
    assert not native_provider.collected and not native_provider.lookups


def test_actual_workflow_summary_reports_budget_and_fidelity_without_private_rows(
    monkeypatch, tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/xmarket-source-recovery.yml").read_text()
    script = workflow.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    script = textwrap.dedent(script)
    data = tmp_path / "data/native-reports"
    data.mkdir(parents=True)
    (data / "xmarket-source-BTC.json").write_text(json.dumps({
        "samples": [{"private": "private-price-and-token"}],
        "backfill_progress": {"contract_attempt_limit": 268, "contract_attempts": 200,
                              "allocated_new_window_budget": 64,
                              "private_file_id": "private-drive-id"},
        "verification": {"polymarket": {"status": "unverified", "paired_points": 157,
                         "live_feature_bars": 278, "overlap_coverage": 157 / 278,
                         "blocked_reason": "insufficient_overlap_coverage",
                         "private_raw_response": "never-publish-response"}},
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASSET", "BTC")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    program = tmp_path / "workflow_summary.py"
    program.write_text(script)
    stdout = subprocess.run(
        [sys.executable, str(program)], check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    result = json.loads(stdout.split("XMARKET_RECOVERY_SUMMARY=", 1)[1])
    assert result["contract_budget"]["contract_attempt_limit"] == 268
    assert result["source_comparison"]["polymarket"]["paired_points"] == 157
    assert result["source_comparison"]["polymarket"]["overlap_coverage"] == 157 / 278
    assert result["model_approved"] is False
    assert "private-" not in stdout + summary.read_text()
    assert "never-publish-response" not in stdout + summary.read_text()
