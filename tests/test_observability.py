"""Sentry reporting: credentials never leave the process; repeats are rate-limited."""
from __future__ import annotations

from parlay_bot import observability


def _event(message="boom", line=10):
    return {
        "logger": "parlay_bot.x",
        "logentry": {"message": message},
        "exception": {"values": [{"type": "RuntimeError",
                                  "stacktrace": {"frames": [{"lineno": line}]}}]},
        "extra": {"discord_token": "abc", "nested": {"db": "postgres://u:p@h/db x"}},
        # Built at runtime: a fake token, shaped like a Discord token.
        "breadcrumbs": {"values": [{"message": "token " + ".".join(
            ("A" * 26, "B" * 6, "C" * 30)) + " seen"}]},
    }


def test_credentials_are_scrubbed_before_sending():
    observability._recent.clear()
    sent = observability.before_send(_event(), {})
    assert sent["extra"]["discord_token"] == "[scrubbed]"
    assert sent["extra"]["nested"]["db"] == "[scrubbed] x"
    assert "AAAA" not in sent["breadcrumbs"]["values"][0]["message"]


def test_same_error_is_sent_once_per_window_but_new_errors_still_go():
    observability._recent.clear()
    assert observability.before_send(_event(), {}) is not None
    assert observability.before_send(_event(), {}) is None
    assert observability.before_send(_event(line=11), {}) is not None


def test_disabled_under_tests_and_by_empty_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert observability.init_sentry() is False
    monkeypatch.setenv("SENTRY_DSN", "")
    assert observability.init_sentry() is False


def test_release_is_the_stamped_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "__file__", str(tmp_path / "observability.py"))
    assert observability.release() is None
    (tmp_path / "RELEASE_SHA").write_text("63b91ab" + "0" * 33 + "\n")
    assert observability.release() == "63b91ab" + "0" * 33
