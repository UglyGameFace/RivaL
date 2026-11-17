"""Bounded Evidence Board collection and read-only security contract."""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/evidence-board-receipt.yml"


def test_evidence_board_cadence_preserves_signed_read_only_collection():
    content = WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "7,17,27,37,47,57 * * * *"' in content
    assert "concurrency:" in content
    assert "cancel-in-progress: false" in content
    assert "id-token: write" in content
    assert "permissions:" in content and "contents: read" in content
    assert "Verify Cloudflare receiver reachability and anonymous-write rejection" in content
    assert "python tools/evidence_board_receipt.py" in content
    assert "DISCLOUD_TOKEN: ${{ secrets.DISCLOUD_TOKEN }}" in content
    assert "RIVAL_DRIVE_OAUTH_REFRESH_TOKEN: ${{ secrets.RIVAL_DRIVE_OAUTH_REFRESH_TOKEN }}" in content
    assert "workflow_dispatch:" in content
    assert "workflow_run:" in content
    # The trigger list and concurrency group are owned by the Delivery 1
    # scheduler patch (~/workspace/your_files/evidence-board-scheduler-
    # workflow.patch): the Production Release trigger is dropped and the
    # group becomes per-event when Felix places it. This test pins only the
    # invariants that hold before and after placement.
    assert '"RivaL independent Discloud uptime"' in content
    collector = (WORKFLOW.parents[2] / "web/evidence-board/collector.js").read_text(encoding="utf-8")
    assert "'schedule','workflow_dispatch','push','workflow_run'" in collector
    assert "github.event.workflow_run.conclusion == 'success'" in content
    assert "github.event.workflow_run.head_branch == 'main'" in content
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in content
    assert "ref: main" in content
    assert '"web/evidence-board/**"' in content
    assert '"src/**"' in content
