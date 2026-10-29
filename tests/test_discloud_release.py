"""Guard deployment so live Discloud cannot receive untested or secret-leaking code."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_discloud_release_only_after_verified_main_ci():
    text = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert 'workflows: ["CI"]' in text
    assert "types: [completed]" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event.workflow_run.event == 'push'" in text
    assert "github.event.workflow_run.head_branch == 'main'" in text
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in text
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in text
    assert "cancel-in-progress: false" in text


def test_discloud_release_fails_closed_without_authenticated_existing_bot():
    text = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert "secrets.DISCLOUD_TOKEN" in text
    assert "secrets.DISCLOUD_APP_ID" in text
    assert 'if [ -z "$DISCLOUD_TOKEN" ] || [ -z "$DISCLOUD_APP_ID" ]; then' in text
    assert 'echo "ready=false" >> "$GITHUB_OUTPUT"' in text
    assert "if: steps.access.outputs.ready == 'true'" in text
    assert "uses: discloud/deploy-action@v1" in text
    assert "app_id: ${{ secrets.DISCLOUD_APP_ID }}" in text


def test_discloud_release_preserves_remote_secrets_and_deploys_only_source():
    text = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    action = text.split("uses: discloud/deploy-action@v1", 1)[1].split(
        "      - name:", 1,
    )[0]
    assert "env_file:" not in action
    assert "\n          env:" not in action
    for path in (
        "discloud.config", "main.py", "requirements.txt",
        "pyproject.toml", "src/**",
    ):
        assert path in action
    assert "secrets.ENV" not in text
    config = (ROOT / "discloud.config").read_text()
    assert "TYPE=bot" in config
    assert "MAIN=main.py" in config
    ignore = (ROOT / ".discloudignore").read_text().splitlines()
    assert ".env" in ignore
    assert "tests/" in ignore
