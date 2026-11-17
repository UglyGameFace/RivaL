"""Guard against independent workflows racing the same Drive-backed source cursor."""

from datetime import datetime
from pathlib import Path

from parlay_bot.research.xmarket_training import HOLDOUT_DAYS

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_xmarket_has_one_scheduled_native_source_writer():
    native = WORKFLOWS / "xmarket-source-recovery.yml"
    redundant = WORKFLOWS / "xmarket-native-recovery.yml"
    assert native.is_file()
    assert not redundant.exists(), "duplicate source acquisition would race the Drive cursor"
    source = native.read_text()
    assert 'cron: "43 2,8,14,20 * * *"' in source
    assert "max_windows" in source and "--publish" in source


def test_native_source_and_archive_training_share_live_writer_lock():
    source = (WORKFLOWS / "xmarket-source-recovery.yml").read_text()
    trainer = (WORKFLOWS / "archive-model-training.yml").read_text()
    assert "rival-archive-model-training" in source
    assert "group: rival-archive-model-training" in trainer
    assert "rival-xmarket-validation-{0}" in source
    assert "cancel-in-progress: false" in source
    assert "cancel-in-progress: false" in trainer


def test_merge_verifies_original_admission_after_source_publication_without_another_writer():
    source = (WORKFLOWS / "xmarket-source-recovery.yml").read_text()
    collect = source.index("--max-windows \"$WINDOW_BUDGET\" --publish")
    proof = source.index("python tools/verify_recorded_partial_restore.py")
    assert proof > collect
    collector_only = source.split("\n  research_after_source:", 1)[0]
    stage = collector_only.split("- name: Verify recovered chronological admission", 1)[1]
    assert "if: github.event_name == 'push'" in stage
    assert "--lookback-days 90 --xmarket-admission" in stage
    assert '--root "data/recorded-private-originals/$ASSET"' in stage
    assert "--publish" not in stage
    assert "xmarket_training" not in stage
    assert "sklearn" not in stage and "lightgbm" not in stage
    assert "if: always() && github.event_name == 'push'" in stage
    upload = stage.split("- name: Upload concise chronological admission", 1)[1]
    assert "path: data/native-reports/recorded-partial-proof-" in upload
    assert "*.json" not in upload, "never upload private originals or input bundles"


def test_verified_four_asset_train_only_pass_keeps_frozen_holdouts_and_no_writes():
    source = (WORKFLOWS / "xmarket-train-only-research.yml").read_text()
    train_end = datetime.fromisoformat("2026-09-21T00:00:00+00:00")
    test_start = datetime.fromisoformat("2026-10-19T00:00:00+00:00")
    assert (test_start - train_end).days == HOLDOUT_DAYS == 28
    assert '--train-end "2026-09-21T00:00:00+00:00"' in source
    assert '--test-start "2026-10-19T00:00:00+00:00"' in source
    assert '--trained-through "2026-11-15T23:55:00+00:00"' in source
    assert "43975283077" in source
    assert "asset: [BTC, ETH, SOL, XRP]" in source
    assert 'paths:' in source and '".github/workflows/xmarket-train-only-research.yml"' in source
    assert "schedule:" not in source
    assert "--publish" not in source
    assert "--out " in source
    assert "models_published" not in source
    assert "workflow_dispatch:" in source
    assert "Research XMarket feature subsets without publishing models" in source
    assert "Upload only sanitized research results" in source
    assert "--max-windows" not in source


def test_source_to_offline_model_handoff_runs_only_after_verified_source():
    source = (WORKFLOWS / "xmarket-source-recovery.yml").read_text()
    assert "needs: [anchor, source]" in source
    assert "needs.source.result == 'success'" in source
    assert "github.ref == 'refs/heads/main'" in source
    assert "github.event.schedule == '43 20 * * *'" in source
    assert "inputs.evaluate_models == true" in source
    assert "INPUT_PUBLISH: ${{ inputs.publish_qualified_offline_candidates }}" in source
    assert "EVENT_SCHEDULE: ${{ github.event.schedule }}" in source
    assert "arguments+=(--publish-passing)" in source
    assert '--as-of "$AS_OF"' in source
    assert "persist-credentials: false" in source
    assert "rival-archive-model-training" in source
    assert "cancel-in-progress: false" in source
    handoff = source.split("\n  research_after_source:", 1)[1]
    assert "--max-windows" not in handoff
    assert "xmarket_unattended_lifecycle.py" in handoff
    assert "restore_archive_months.py" in handoff
    assert "if-no-files-found: warn" in handoff
    assert "xmarket-native-to-model-" in handoff
    assert "data/native-reports/model-lifecycle-" in handoff
    assert "--publish" not in handoff.replace("--publish-passing", "")
