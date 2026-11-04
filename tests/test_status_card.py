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
    assert "Healthy" in embed.title
    text = str(embed.to_dict())
    assert "1/1 running" in text  # disabled workers are not counted
    assert "1/1 exchange feeds fresh" in text


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
    assert "unreadable" in text and "Not configured" in text


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
    assert any(f.name == "Prediction health" and "stale" in f.value for f in embed.fields)


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
    field = next(f for f in build_embed(facts)[0].fields if f.name == "Market data")
    assert "1/1 exchange feeds fresh" in field.value and "off" in field.value
