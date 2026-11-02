"""Browser evidence must not leak signed URLs or claim archive completion."""
from __future__ import annotations

from tools.bitget_portal_browser_probe import sanitized_url


def test_strip_query_values_and_preserve_public_route():
    x = sanitized_url("https://www.bitget.com/api/data-download?a=secret&b=4&a=other")
    assert x == {
        "host": "www.bitget.com", "path": "/api/data-download",
        "query_keys": ["a", "b"],
    }
    assert "secret" not in str(x)
    assert "other" not in str(x)


def test_dont_record_credentials_from_userinfo_or_fragment():
    x = sanitized_url("https://any:private@www.bitget.com/market#token=secret")
    assert "private" not in str(x) and "secret" not in str(x)
