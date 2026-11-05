"""RivaL status card: real-state assessment, redacted error digest, post policy."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.error_log import RecentProblems, redact
from parlay_bot.discord_app.status_card import (
    StatusFacts,
    StatusPostPolicy,
    assess,
    build_embed,
)

NOW = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)


def _facts(**overrides) -> StatusFacts:
    base = StatusFacts(
        now=NOW, boot_at=NOW - timedelta(hours=3), ready=True,
        heartbeat={"last_success_at": NOW - timedelta(seconds=30),
                   "last_problem_at": None, "last_problem": None,
                   "consecutive_problems": 0},
        workers=[{"name": "rival-perp-auto-collector", "state": "running",
                  "restarts_last_hour": 0},
                 {"name": "sports", "state": "disabled", "restarts_last_hour": 0}],
        incidents=[],
        feeds=[{"venue": "binance", "asset": "BTC",
                "last_success_at": (NOW - timedelta(minutes=3)).isoformat(),
                "failure_count": 0, "status": "ok"}],
        streams={"binance": {"connected": True, "updates": 1200, "reconnects": 1}},
        liquidity_last_bucket=(NOW - timedelta(minutes=5)).isoformat(),
        memory={"rss_mb": 300, "limit_mb": 512, "pressure": False, "oom_kills": 0},
    )
    return replace(base, **overrides)


def test_healthy_state_has_empty_signature():
    severity, signature = assess(_facts())
    assert (severity, signature) == ("healthy", ())
    embed, _, _ = build_embed(_facts())
    assert "All good" in embed.title
    text = str(embed.to_dict())
    assert "1 of 1 running" in text  # disabled workers are not counted
    assert "1 of 1 up to date" in text


def test_real_problems_raise_severity_and_name_the_cause():
    facts = _facts(
        workers=[{"name": "rival-perp-auto-collector", "state": "stopped",
                  "restarts_last_hour": 3}],
        incidents=[{"issue_key": "settlement_backlog", "detail": "42 targets late",
                    "first_seen_at": (NOW - timedelta(minutes=50)).isoformat()}],
        feeds=[{"venue": "okx", "asset": "XRP",
                "last_success_at": (NOW - timedelta(minutes=41)).isoformat(),
                "failure_count": 3, "status": "timeout"}],
        heartbeat={"last_success_at": NOW - timedelta(minutes=4),
                   "last_problem_at": NOW, "last_problem": "ping not delivered (HTTP 503)",
                   "consecutive_problems": 4},
    )
    severity, signature = assess(facts)
    assert severity == "down"
    assert {"worker:rival-perp-auto-collector", "incident:settlement_backlog",
            "feed:okx/XRP", "heartbeat:failing"} <= set(signature)
    text = str(build_embed(facts)[0].to_dict())
    for expected in ("stopped", "restarted 3×", "settlement_backlog", "42 targets late",
                     "okx/XRP", "3 failures", "HTTP 503", "4 in a row"):
        assert expected in text


def test_unreadable_sources_are_reported_not_guessed():
    facts = _facts(incidents=None, liquidity_last_bucket=None, heartbeat=None)
    severity, signature = assess(facts)
    assert severity == "degraded"
    assert "watchdog:unreadable" in signature and "liquidity:stale" in signature
    text = str(build_embed(facts)[0].to_dict())
    assert "Couldn't read" in text and "Not set up" in text


def test_error_digest_groups_counts_and_redacts():
    clock = [NOW]
    problems = RecentProblems(clock=lambda: clock[0])
    log = logging.getLogger("parlay_bot.test_status_card")
    log.addHandler(problems)
    try:
        for _ in range(3):
            log.warning("OKX timeout for %s", "XRP")
        log.error("DB failed postgresql://user:pw@host/db token=abc123")
        log.info("ignored")
    finally:
        log.removeHandler(problems)
    groups = problems.recent()
    assert groups[0].level == "ERROR" and "[redacted]" in groups[0].text
    assert "pw@host" not in groups[0].text and "abc123" not in groups[0].text
    assert groups[1].count == 3 and groups[1].text == "OKX timeout for XRP"
    assert problems.totals() == {"ERROR": 1, "WARNING": 3}
    clock[0] = NOW + timedelta(hours=25)
    assert problems.recent() == []
    assert redact("worker rival-verified-offline-candidate-import") == (
        "worker rival-verified-offline-candidate-import")


def test_post_policy_posts_on_start_confirmed_change_and_daily():
    policy = StatusPostPolicy()
    assert policy.should_post((), NOW)
    policy.posted((), NOW)
    assert not policy.should_post((), NOW + timedelta(minutes=2))
    blip = ("stream:binance",)
    assert not policy.should_post(blip, NOW + timedelta(minutes=4))  # first sighting
    assert not policy.should_post((), NOW + timedelta(minutes=6))    # blip cleared
    assert not policy.should_post(blip, NOW + timedelta(minutes=8))
    assert policy.should_post(blip, NOW + timedelta(minutes=10))     # confirmed
    policy.posted(blip, NOW + timedelta(minutes=10))
    assert policy.should_post(blip, NOW + timedelta(hours=34, minutes=11))  # daily


def test_prediction_health_alerts_change_the_card_signature():
    from datetime import UTC, datetime

    from parlay_bot.discord_app.status_card import StatusFacts, assess, build_embed

    now = datetime(2026, 11, 3, tzinfo=UTC)
    calm = StatusFacts(now=now, boot_at=now, ready=True, incidents=[], research=[])
    alert = StatusFacts(now=now, boot_at=now, ready=True, incidents=[],
                        research=[("pooled:stale", "⚠️ stale")])
    assert assess(calm)[0] == "healthy"
    severity, signature = assess(alert)
    assert severity == "degraded" and "research:pooled:stale" in signature
    embed = build_embed(alert)[0]
    assert any(f.name == "Predictions" and "stale" in f.value for f in embed.fields)


def test_blocked_provider_is_shown_as_off_not_failing():
    from datetime import UTC, datetime

    from parlay_bot.discord_app.status_card import StatusFacts, assess, build_embed

    now = datetime(2026, 11, 3, tzinfo=UTC)
    facts = StatusFacts(now=now, boot_at=now, ready=True, incidents=[], research=[],
                        feeds=[{"venue": "binance", "asset": "BTC", "status": "blocked",
                                "last_success_at": None, "failure_count": 60},
                               {"venue": "okx", "asset": "BTC", "status": "ok",
                                "last_success_at": now.isoformat()}])
    assert assess(facts)[0] == "healthy"
    field = next(f for f in build_embed(facts)[0].fields if f.name == "Exchange prices")
    assert "1 of 1 up to date" in field.value and "off" in field.value


def test_open_socket_with_no_data_is_not_a_green_check():
    from parlay_bot.discord_app.status_card import _stream_line
    blocked = _stream_line("binance", {"connected": True, "updates": 0, "reconnects": 0,
                                       "fresh_assets": ()})
    assert blocked.startswith("⚪") and "nothing received" in blocked
    stale = _stream_line("binance", {"connected": True, "updates": 50, "reconnects": 2,
                                     "fresh_assets": ()})
    assert stale.startswith("⚠️")
    live = _stream_line("hyperliquid", {"connected": True, "updates": 456, "reconnects": 0,
                                        "fresh_assets": ("BTC",)})
    assert live.startswith("✅")
    assert _stream_line("hyperliquid", {"connected": False}).startswith("🛑")


def test_status_footer_identifies_live_prediction_health_source():
    """Operator can compare the running code with GitHub's exact file blob."""
    from hashlib import sha1
    from pathlib import Path

    from parlay_bot.discord_app import research_alerts
    from parlay_bot.discord_app.status_card import _prediction_health_revision

    content = Path(research_alerts.__file__).read_bytes()
    expected = sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()[:10]
    assert _prediction_health_revision() == expected
    assert f"Check version {expected}" in build_embed(_facts())[0].footer.text


def test_error_lines_say_what_is_still_happening_and_what_has_stopped():
    from parlay_bot.discord_app.error_log import ProblemGroup
    from parlay_bot.discord_app.status_card import problem_lines, problem_state

    now = datetime(2026, 11, 4, 19, 30, tzinfo=UTC)
    boot = now - timedelta(hours=2)

    def group(text, last, level="ERROR"):
        return ProblemGroup("parlay_bot.x", level, 3, last - timedelta(hours=1), last, text)

    fixed = group("deadlock", boot - timedelta(minutes=30))   # before the restart
    startup = group("gate timeout", boot + timedelta(minutes=4))
    quiet = group("slow query", now - timedelta(minutes=90), "WARNING")
    live = group("stale quote", now - timedelta(minutes=5))
    assert [problem_state(g, now=now, boot_at=boot) for g in (fixed, startup, quiet, live)] == [
        "stopped", "startup", "quiet", "active"]
    lines = problem_lines([fixed, startup, quiet, live], now=now, boot_at=boot,
                          totals={"ERROR": 9, "WARNING": 3})
    assert lines[0].startswith("🔴 1 happening now • 🟢 1 calmed down (none in the last "
                               "hour) • ⚪ 1 only while starting up • ✅ 1 stopped")
    assert lines[2].startswith("🔴") and "stale quote" in lines[2]  # live problems lead
    assert lines[-1].startswith("✅") and "deadlock" in lines[-1]
    assert problem_lines([], now=now, boot_at=boot, totals={})[-1] == "None"


def test_health_alert_shows_recovered_once_its_incident_closes():
    from parlay_bot.discord_app.error_log import ProblemGroup
    from parlay_bot.discord_app.status_card import problem_state

    now = datetime(2026, 11, 4, 19, 30, tzinfo=UTC)
    alert = ProblemGroup("parlay_bot.perpetuals.runtime_health", "ERROR", 13,
                         now - timedelta(hours=3), now - timedelta(minutes=20),
                         "RivaL HEALTH ALERT settlement_backlog: 14 recoverable outcomes")
    boot = now - timedelta(hours=2)
    assert problem_state(alert, now=now, boot_at=boot, open_incidents=set()) == "recovered"
    assert problem_state(alert, now=now, boot_at=boot,
                         open_incidents={"settlement_backlog"}) == "active"
    feed = ProblemGroup("parlay_bot.perpetuals.runtime_health", "ERROR", 2,
                        now - timedelta(hours=1), now - timedelta(minutes=5),
                        "RivaL HEALTH ALERT feed:hyperliquid:ETH: no fresh candle")
    assert problem_state(feed, now=now, boot_at=boot,
                         open_incidents={"feed:hyperliquid:ETH"}) == "active"
    assert problem_state(feed, now=now, boot_at=boot,
                         open_incidents={"feed:hyperliquid:BTC"}) == "recovered"
    # Unknown incident state (watchdog unreadable) never claims recovery.
    assert problem_state(alert, now=now, boot_at=boot, open_incidents=None) == "active"


def test_stream_is_judged_by_its_last_delivery_not_one_instant():
    from parlay_bot.discord_app.status_card import _stream_line

    flowing = _stream_line("hyperliquid", {"connected": True, "updates": 276, "reconnects": 0,
                                           "fresh_assets": (), "last_update_age_s": 4.0})
    assert flowing.startswith("✅ hyperliquid: live, last update 4s ago")
    stalled = _stream_line("hyperliquid", {"connected": True, "updates": 276, "reconnects": 0,
                                           "fresh_assets": (), "last_update_age_s": 400.0})
    assert stalled.startswith("⚠️ hyperliquid: connected but nothing new for 6 min")


def test_a_stalled_stream_counts_after_startup_and_clears_when_it_recovers():
    from datetime import UTC, datetime, timedelta

    from parlay_bot.discord_app.status_card import StatusFacts, assess

    now = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)

    def facts(age, boot_minutes=30):
        return StatusFacts(
            now=now, boot_at=now - timedelta(minutes=boot_minutes), ready=True,
            heartbeat=None, workers=[], incidents=[], feeds=None,
            streams={"hyperliquid": {"connected": True, "updates": 500, "reconnects": 0,
                                     "fresh_assets": (), "last_update_age_s": age}},
            liquidity_last_bucket=now.isoformat(), memory=None, research=[],
        )

    assert "stream_stalled:hyperliquid" in assess(facts(400))[1]
    assert assess(facts(3)) == ("healthy", ())
    assert "stream_stalled:hyperliquid" not in assess(facts(400, boot_minutes=2))[1]


def test_empty_prediction_health_says_no_problems():
    from datetime import UTC, datetime

    from parlay_bot.discord_app.status_card import StatusFacts, build_embed

    now = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
    embed, _, _ = build_embed(StatusFacts(
        now=now, boot_at=now, ready=True, heartbeat=None, workers=[], incidents=[],
        feeds=None, streams=None, liquidity_last_bucket=None, memory=None, research=[]))
    field = next(f for f in embed.fields if f.name == "Predictions")
    assert field.value.startswith("✅ No problems")
