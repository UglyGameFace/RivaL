"""Guard against independent workflows racing the same Drive-backed source cursor."""

from pathlib import Path

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
    stage = source.split("- name: Verify recovered chronological admission", 1)[1]
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
    from datetime import datetime
    from parlay_bot.research.xmarket_training import HOLDOUT_DAYS

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
    assert "source-only" not in source or "no source-only" not in source
