"""RivaL status card: real-state assessment, redacted error digest, post policy."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.error_log import RecentProblems, redact
from parlay_bot.discord_app.status_card import (
    StatusFacts,
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
    # Healthy is one short Checks list, not a section per system.
    assert [f.name for f in embed.fields] == ["Checks"]


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
    assert "couldn't read the alert list" in text and "not set up" in text


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
    field = next(f for f in build_embed(facts)[0].fields if f.name == "Checks")
    assert "**Prices:** 1 of 1 up to date" in field.value
    assert "off, the exchange blocks this server: binance/BTC" in field.value


def test_open_socket_with_no_data_is_not_a_green_check():
    from parlay_bot.discord_app.status_card import _stream_line
    blocked = _stream_line("binance", {"connected": True, "updates": 0, "reconnects": 0,
                                       "fresh_assets": ()})
    assert blocked.startswith("⚠️") and "no usable books received" in blocked
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
    assert "1 active/recent" in lines[0] and "1 seen only before this boot" in lines[0]
    assert "0 watchdog incidents resolved" in lines[0]
    assert lines[2].startswith("🔴") and "stale quote" in lines[2]  # live problems lead
    assert "recovery unverified" in lines[-1] and "deadlock" in lines[-1]
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


def _predictions(embed) -> str:
    checks = next(f for f in embed.fields if f.name == "Checks").value
    return next(line for line in checks.splitlines() if "**Predictions:**" in line)


def test_empty_prediction_health_states_only_what_the_checks_prove():
    from datetime import UTC, datetime

    from parlay_bot.discord_app.status_card import StatusFacts, build_embed

    now = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
    embed, _, _ = build_embed(StatusFacts(
        now=now, boot_at=now, ready=True, heartbeat=None, workers=[], incidents=[],
        feeds=None, streams=None, liquidity_last_bucket=None, memory=None, research=[]))
    line = _predictions(embed)
    # No blanket "no problems" and no unqualified "isn't doing worse": the
    # checks prove less than that.
    assert "No problems" not in line
    assert "isn't doing worse" not in line
    assert "last 20 min" in line
    assert "last finished month" in line
    assert "needs 300+ graded results for a verdict" in line
    # Check 4 ran (the default), so the verified review is claimed.
    assert "AI reviewed within the last 2 hours" in line


def test_predictions_field_does_not_claim_a_review_that_never_ran(tmp_path):
    """Check 4 skipped (Intelligence task absent / background opt-in off):
    the card must not claim a review happened within the last 2 hours."""
    from parlay_bot.discord_app.research_alerts import research_alerts
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "card_intelligence_off.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE perp_forward_audit "
                         "(asset TEXT, horizon_minutes INTEGER, issued_at TEXT)")
            conn.execute("INSERT INTO perp_forward_audit VALUES ('BTC', 5, ?)",
                         ((NOW - timedelta(minutes=3)).isoformat(),))
            conn.execute("CREATE TABLE rival_pooled_models "
                         "(trained_through TEXT, gates_passed INTEGER)")
            conn.execute("INSERT INTO rival_pooled_models VALUES (?,1)",
                         (str(NOW - timedelta(days=2)).replace("T", " "),))
            # No rival_intelligence_calls rows at all: no review ever happened.
            conn.execute("CREATE TABLE rival_intelligence_calls (issued_at TEXT)")
            conn.execute("""CREATE TABLE rival_model_shadow_forecasts (
                            family TEXT, horizon_minutes INTEGER, settled_at TEXT,
                            model_brier REAL, baseline_brier REAL, venue TEXT,
                            training_sha256 TEXT, issued_at TEXT,
                            target_opened_at TEXT)""")
        # The task was never created, so check 4 never ran: no alert either way.
        assert research_alerts(store, now=NOW, intelligence_enabled=False) == []
        embed, _, _ = build_embed(StatusFacts(
            now=NOW, boot_at=NOW, ready=True, heartbeat=None, workers=[],
            incidents=[], feeds=None, streams=None, liquidity_last_bucket=None,
            memory=None, research=[], intelligence_checked=False))
        line = _predictions(embed)
        assert "reviewed within the last 2 hours" not in line
        assert "AI reviews off (background opt-in off)" in line
        # The three checks that did run are still reported honestly.
        assert "last 20 min" in line and "last finished month" in line
    finally:
        store.close()


def test_predictions_field_claims_a_review_only_after_a_real_recent_one(tmp_path):
    """Check 4 ran and saw a recent rival_intelligence_calls row: the claim
    is then a verified fact, not an assumption."""
    from parlay_bot.discord_app.research_alerts import research_alerts
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "card_intelligence_on.sqlite")
    try:
        with store.connect() as conn:
            conn.execute("CREATE TABLE perp_forward_audit "
                         "(asset TEXT, horizon_minutes INTEGER, issued_at TEXT)")
            conn.execute("INSERT INTO perp_forward_audit VALUES ('BTC', 5, ?)",
                         ((NOW - timedelta(minutes=3)).isoformat(),))
            conn.execute("CREATE TABLE rival_pooled_models "
                         "(trained_through TEXT, gates_passed INTEGER)")
            conn.execute("INSERT INTO rival_pooled_models VALUES (?,1)",
                         (str(NOW - timedelta(days=2)).replace("T", " "),))
            conn.execute("CREATE TABLE rival_intelligence_calls (issued_at TEXT)")
            conn.execute("INSERT INTO rival_intelligence_calls VALUES (?)",
                         ((NOW - timedelta(minutes=10)).isoformat(),))
            conn.execute("""CREATE TABLE rival_model_shadow_forecasts (
                            family TEXT, horizon_minutes INTEGER, settled_at TEXT,
                            model_brier REAL, baseline_brier REAL, venue TEXT,
                            training_sha256 TEXT, issued_at TEXT,
                            target_opened_at TEXT)""")
        assert research_alerts(store, now=NOW, intelligence_enabled=True) == []
        embed, _, _ = build_embed(StatusFacts(
            now=NOW, boot_at=NOW, ready=True, heartbeat=None, workers=[],
            incidents=[], feeds=None, streams=None, liquidity_last_bucket=None,
            memory=None, research=[], intelligence_checked=True))
        assert "AI reviewed within the last 2 hours" in _predictions(embed)
    finally:
        store.close()


def test_a_blocked_order_book_is_off_not_a_warning():
    facts = _facts(streams={
        "hyperliquid": {"connected": True, "updates": 50, "reconnects": 0,
                        "last_update_age_s": 2},
        "binance": {"connected": False, "updates": 0, "reconnects": 0,
                    "blocked": True, "blocked_status": 451}})
    checks = next(f.value for f in build_embed(facts)[0].fields if f.name == "Checks")
    line = next(x for x in checks.splitlines() if "**Order books:**" in x)
    assert line.startswith("✅ **Order books:** 1 of 1 live")
    assert "binance: access blocked (HTTP 451)" in checks


def test_problem_detail_sits_under_its_own_check():
    facts = _facts(workers=[{"name": "settlement", "state": "stopped",
                             "restarts_last_hour": 2}])
    checks = next(f.value for f in build_embed(facts)[0].fields
                  if f.name == "Checks").splitlines()
    jobs = next(i for i, x in enumerate(checks) if "**Jobs:**" in x)
    assert checks[jobs].startswith("⚠️ **Jobs:** 0 of 1 running")
    assert checks[jobs + 1].strip() == "🛑 settlement: stopped"
    assert checks[jobs + 2].strip() == "↻ settlement: restarted 2× in the last hour"
