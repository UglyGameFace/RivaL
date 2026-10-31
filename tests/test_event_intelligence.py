"""Official-event intake, point-in-time history, and signal veto regression tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.perpetuals.event_intelligence import (
    SOURCES,
    EventIntelligence,
    _ics,
    _indexed_news,
    _rss,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def _rss_body(title: str, *, source: str, when: datetime) -> bytes:
    url = ("https://www.sec.gov/newsroom/press-releases/test"
           if source == "sec_releases" else
           "https://www.federalreserve.gov/newsevents/pressreleases/monetary-test.htm")
    return (
        "<rss version='2.0'><channel><item>"
        f"<title>{title}</title><link>{url}</link><guid>{url}</guid>"
        f"<pubDate>{format_datetime(when)}</pubDate>"
        "</item></channel></rss>"
    ).encode()


def _calendar(at: datetime) -> bytes:
    # The release is issued in actual Eastern time (DST-aware).
    from zoneinfo import ZoneInfo

    stamp = at.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%dT%H%M%S")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:bls-employment-20261106\r\n"
        "SUMMARY:Employment Situation for October 2026\r\n"
        f"DTSTART;TZID=America/New_York:{stamp}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


def _client(now: datetime, release: datetime, *, bad_sec: bool = False):
    def handler(request):
        url = str(request.url)
        if request.url.host == "api.gdeltproject.org":
            return httpx.Response(200, json={"articles": []})
        if url == SOURCES["bls_calendar"][0]:
            return httpx.Response(200, content=_calendar(release))
        if url == SOURCES["fed_monetary"][0]:
            return httpx.Response(
                200, content=_rss_body("Federal Reserve issues FOMC statement",
                                       source="fed_monetary", when=now-timedelta(hours=4)),
            )
        if url == SOURCES["sec_releases"][0] and bad_sec:
            return httpx.Response(503)
        if url == SOURCES["sec_releases"][0]:
            return httpx.Response(
                200, content=_rss_body("SEC proposes crypto asset custody rule",
                                       source="sec_releases", when=now-timedelta(minutes=4)),
            )
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_untrusted_feed_tags_links_and_stale_headlines_fail_closed():
    stamp = datetime(2026, 11, 6, 13, tzinfo=UTC)
    bad = _rss_body("SEC crypto decision", source="sec_releases", when=stamp)
    assert len(_rss(bad, "sec_releases", stamp)) == 1
    bad_url = bad.replace(b"https://www.sec.gov/", b"https://malicious.example/")
    with pytest.raises(ValueError, match="unverified"):
        _rss(bad_url, "sec_releases", stamp)
    with pytest.raises(ValueError, match="DOCTYPE"):
        _rss(b"<!DOCTYPE foo><rss/>", "sec_releases", stamp)
    stale = _rss_body("SEC crypto decision", source="sec_releases",
                      when=stamp-timedelta(days=91))
    assert _rss(stale, "sec_releases", stamp) == []
    irrelevant = _rss_body("SEC opens staff picnic", source="sec_releases", when=stamp)
    assert _rss(irrelevant, "sec_releases", stamp) == []


def test_calendar_times_are_aware_and_not_assumed_for_all_day_releases():
    at = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    items = _ics(_calendar(at), at-timedelta(minutes=20))
    assert len(items) == 1
    assert items[0]["scheduled_at"] == at.isoformat()
    assert items[0]["published_at"] is None
    assert _ics(_calendar(at).replace(
        b"DTSTART;TZID=America/New_York:20261106T083000",
        b"DTSTART;VALUE=DATE:20261106",
    ), at-timedelta(minutes=20)) == []


def test_durable_first_seen_revisions_and_no_call_calendar_window(tmp_path):
    store = SQLiteHotStore(tmp_path / "macro.sqlite")
    intel = EventIntelligence(store)
    stamp = datetime(2026, 11, 6, 13, 15, tzinfo=UTC)
    release = stamp + timedelta(minutes=15)
    with _client(stamp, release) as http:
        first = intel.refresh(now=stamp, client=http)
    assert first["sources_checked"] == 4
    assert first["sources_ok"] == 4
    state = intel.context(asset="BTC", horizon=5, now=stamp)
    assert state["decision"] == "pause"
    assert state["event_risk_cleared"] is False
    assert any(e["source"] == "bls_calendar" and e["risk_window_active"]
               for e in state["events"])
    assert all(e["market_direction_inferred"] is False for e in state["events"])
    with store.connect() as db:
        n = db.execute("SELECT COUNT(*) AS n FROM rival_macro_events").fetchone()["n"]
        original = db.execute(
            "SELECT first_seen_at FROM rival_macro_events WHERE source='bls_calendar'",
        ).fetchone()["first_seen_at"]
    assert n == 3
    # The refresh is throttled across process restarts by the shared database.
    with _client(stamp, release) as http:
        second = EventIntelligence(store).refresh(
            now=stamp+timedelta(minutes=10), client=http,
        )
    assert second["sources_checked"] == 0
    with _client(stamp, release) as http:
        third = intel.refresh(now=stamp+timedelta(minutes=31), client=http)
    assert third["sources_ok"] == 4
    with store.connect() as db:
        assert db.execute(
            "SELECT first_seen_at FROM rival_macro_events WHERE source='bls_calendar'",
        ).fetchone()["first_seen_at"] == original
        assert db.execute("SELECT COUNT(*) AS n FROM rival_macro_events").fetchone()["n"] == n
    assert intel.context(
        asset="ETH", horizon=5, now=stamp+timedelta(minutes=46),
    )["decision"] != "pause"


def test_one_source_outage_is_isolated_and_does_not_invent_all_clear(tmp_path):
    store = SQLiteHotStore(tmp_path / "partial.sqlite")
    intel = EventIntelligence(store)
    stamp = datetime(2026, 11, 6, 13, 15, tzinfo=UTC)
    with _client(stamp, stamp+timedelta(minutes=15), bad_sec=True) as http:
        report = intel.refresh(now=stamp, client=http)
    assert report["sources_checked"] == 4
    assert report["sources_ok"] == 3
    assert report["source_failures"][0]["source"] == "sec_releases"
    state = intel.context(asset="SOL", horizon=15, now=stamp)
    assert state["sources"]["sec_releases"] == "unavailable_or_stale"
    assert not state["event_risk_cleared"]
    assert state["decision"] == "pause"
    never = EventIntelligence(SQLiteHotStore(tmp_path / "new.sqlite")).context(
        asset="SOL", horizon=15, now=stamp,
    )
    assert never["status"] == "unavailable"
    assert not never["event_risk_cleared"]


@pytest.mark.asyncio
async def test_signal_is_vetoed_before_market_books_or_llm_calls(tmp_path):
    store = SQLiteHotStore(tmp_path / "signal.sqlite")
    market = PerpetualMarketStore(store)
    intel = EventIntelligence(store)
    stamp = datetime(2026, 11, 6, 13, 15, tzinfo=UTC)
    with _client(stamp, stamp+timedelta(minutes=15)) as http:
        intel.refresh(now=stamp, client=http)

    class NotUsedQuotes:
        def __getattr__(self, name):
            raise AssertionError("Event risk must stop before quote fetch")

    verdict = {}
    response = await signal_embed(
        market, NotUsedQuotes(), asset="BTC", horizon_minutes=5,
        now=stamp, decision_out=verdict, decision_only=True,
    )
    assert "NO LIVE SIGNAL" in response.title
    assert "Employment Situation" in response.description
    assert verdict["is_watch"] is False
    assert verdict["event_risk"] == "official_release_window"


def test_indexed_world_news_is_research_only_and_never_claims_publication(tmp_path):
    now = datetime(2026, 11, 6, 13, 15, tzinfo=UTC)
    seen = (now-timedelta(minutes=4)).strftime("%Y%m%dT%H%M%SZ")
    valid = [
        {"title": "Bitcoin exchange hack draws scrutiny",
         "url": "https://www.reuters.com/world/crypto/hack-report/",
         "seendate": seen},
        {"title": "Government sanctions could affect financial markets",
         "url": "https://apnews.com/article/sanctions-financial-markets",
         "seendate": seen},
        {"title": "World markets are fine, trust us",
         "url": "https://untrusted.example/potentially-fake-story",
         "seendate": seen},
        {"title": "Celebrity cooking show",
         "url": "https://www.bbc.com/food/cooking/",
         "seendate": seen},
    ]
    records = _indexed_news(json.dumps({"articles": valid}).encode(), now)
    assert len(records) == 2
    assert all(x["published_at"] is None for x in records)
    assert all(x["upstream_index_seen_at"] == (
        now-timedelta(minutes=4)).isoformat() for x in records)
    assert {x["category"] for x in records} == {
        "indexed_crypto_incident", "indexed_global_geopolitics",
    }

    def handler(request):
        if request.url.host == "api.gdeltproject.org":
            assert request.url.params["timespan"] == "1h"
            return httpx.Response(200, json={"articles": valid})
        return httpx.Response(503)

    intel = EventIntelligence(SQLiteHotStore(tmp_path / "index.sqlite"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        status = intel.refresh(now=now, client=http)
    assert status["sources_ok"] == 1
    assert len(status["source_failures"]) == 3
    state = intel.context(asset="BTC", horizon=15, now=now)
    assert state["status"] == "unavailable"  # no verified official source
    assert state["decision"] == "no_verified_active_pause"
    assert not state["event_risk_cleared"]
    indexed = [x for x in state["events"] if x["source"] == "global_news_index"]
    assert len(indexed) == 2
    assert all(e["provenance"] == "indexed_secondary_unverified" for e in indexed)
    assert all(e["source_published_at"] is None for e in indexed)
    assert all(e["market_direction_inferred"] is False for e in indexed)
    assert all(e["upstream_index_seen_at"] is not None for e in indexed)

