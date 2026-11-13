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
