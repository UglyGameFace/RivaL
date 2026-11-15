"""The native Discloud deploy observer never sends a deployment or makes claims from CI alone."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools import discloud_native_release as observer

ROOT = Path(__file__).resolve().parents[1]


def _stage(root: Path) -> None:
    for name in observer.REQUIRED:
        (root / name).write_text(name, encoding="utf-8")
    (root / "src" / "parlay_bot").mkdir(parents=True)
    (root / "src" / "parlay_bot" / "one.py").write_text("a = 1\n")


def test_remote_command_and_local_hash_use_identical_source_contract(tmp_path):
    _stage(tmp_path)
    expected = observer.source_sha256(tmp_path)
    run = subprocess.run(
        ["python", "-c", observer.REMOTE_PYTHON], cwd=tmp_path,
        capture_output=True, text=True, check=True, timeout=15,
    )
    assert run.stdout.strip() == observer.MARKER + expected
    (tmp_path / "src" / "parlay_bot" / "one.py").write_text("a = 2\n")
    assert observer.source_sha256(tmp_path) != expected


def test_missing_source_fails_closed(tmp_path):
    _stage(tmp_path)
    (tmp_path / "main.py").unlink()
    with pytest.raises(ValueError, match="missing required"):
        observer.source_sha256(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["python", "-c", observer.REMOTE_PYTHON],
                       cwd=tmp_path, check=True, capture_output=True)


def test_remote_fingerprint_requires_exact_single_valid_marker():
    digest = hashlib.sha256(b"test").hexdigest()
    payload = {"apps": {"shell": {"stdout": "ignored\n" + observer.MARKER + digest}}}
    assert observer.parse_remote_sha(payload) == digest
    for stdout in ("", observer.MARKER + "bad", observer.MARKER + digest
                   + "\n" + observer.MARKER + digest):
        with pytest.raises(ValueError):
            observer.parse_remote_sha({"apps": {"shell": {"stdout": stdout}}})
    with pytest.raises(TypeError):
        observer.parse_remote_sha({"apps": {}})


def test_observer_calls_only_read_only_discloud_console(monkeypatch):
    digest = hashlib.sha256(b"test").hexdigest()
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, *_args):
            return json.dumps({"apps": {"shell": {
                "stdout": observer.MARKER + digest,
            }}}).encode()
    def opener(req, timeout):
        assert req.full_url == "https://api.discloud.app/v2/app/app-id/console"
        assert req.get_method() == "PUT"
        assert req.get_header("Api-token") == "secret"
        assert json.loads(req.data) == {"command": observer.REMOTE_COMMAND}
        assert timeout == 110
        assert "deploy" not in observer.REMOTE_COMMAND.lower()
        return Response()
    monkeypatch.setattr(observer.request, "urlopen", opener)
    assert observer.host_source_sha256("secret", "app-id") == digest


def test_observer_missing_credentials_or_old_host_cannot_pass(monkeypatch, capsys):
    monkeypatch.delenv("DISCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("DISCLOUD_APP_ID", raising=False)
    assert observer.main() == 1
    monkeypatch.setenv("DISCLOUD_TOKEN", "secret")
    monkeypatch.setenv("DISCLOUD_APP_ID", "app-id")
    monkeypatch.setattr(observer, "source_sha256", lambda _root: "a" * 64)
    monkeypatch.setattr(observer, "host_source_sha256", lambda *_: "b" * 64)
    monkeypatch.setattr(observer.time, "sleep", lambda _: None)
    assert observer.main() == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert "source_not_yet_matching" in output


def test_release_is_only_an_observer_of_native_autodeploy():
    content = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert "discloud/deploy-action" not in content
    assert "sleep 900" not in content
    assert "python tools/discloud_native_release.py" in content
    assert "workflows: [\"CI\"]" in content
    assert "Verify native Discloud source parity" in content
    assert "DISCLOUD_TOKEN: ${{ secrets.DISCLOUD_TOKEN }}" in content
