"""RivaL status card for an operator Discord channel.

Built only from real runtime state: the Healthchecks.io heartbeat outcome,
background worker tasks and restarts, persisted watchdog incidents, per-feed
collection freshness, websocket feeds, the liquidity record, memory, and the
recent warning/error log digest. Posted when the problem set changes and
once a day, never on a timer that floods the channel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path

import discord

STALE_FEED_AFTER = timedelta(minutes=20)
STALE_LIQUIDITY_AFTER = timedelta(minutes=15)
# A live book stream that has delivered nothing for this long is a problem
# (counted in the status, so the card reposts when it stops and recovers).
STREAM_STALLED_AFTER = timedelta(minutes=2)
STARTUP_GRACE = timedelta(minutes=5)
HEARTBEAT_FAILING_AFTER = 3  # consecutive failed pings
DAILY = timedelta(hours=24)
# A logged problem counts as "still happening" only if it repeated within
# this window; startup noise is the burst while every worker starts at once.
ACTIVE_WINDOW = timedelta(minutes=60)
STARTUP_GRACE = timedelta(minutes=10)
COLORS = {"healthy": discord.Color.green(), "degraded": discord.Color.orange(),
          "down": discord.Color.red()}
LABELS = {"healthy": "🟢 Healthy", "degraded": "🟡 Degraded", "down": "🔴 Problem"}


@dataclass
class StatusFacts:
    now: datetime
    boot_at: datetime
    ready: bool
    heartbeat: dict | None = None  # None = not configured
    workers: list[dict] = field(default_factory=list)
    incidents: list[dict] | None = None  # None = watchdog DB unreadable
    feeds: list[dict] | None = None  # None = collector disabled or unreadable
    streams: dict | None = None
    liquidity_last_bucket: str | None = None
    memory: dict | None = None
    problems: list = field(default_factory=list)  # error_log.ProblemGroup
    problem_totals: dict = field(default_factory=dict)
    # Prediction health: [(stable key, text)]; None = not checked.
    research: list | None = None


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m ago"
    return f"{seconds // 86400}d ago"


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=UTC)


def _clip(lines: list[str], limit: int = 1000) -> str:
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "—"


def _stream_stalled(s: dict) -> bool:
    """Connected, has delivered before, but nothing accepted for a while."""
    age = s.get("last_update_age_s")
    return bool(s.get("connected") and s.get("updates") and age is not None
                and age > STREAM_STALLED_AFTER.total_seconds())


def _stream_line(venue: str, s: dict) -> str:
    """A socket can stay open while the provider sends nothing usable
    (Binance on a blocked host): that is not a healthy stream."""
    counts = f"{s.get('updates', 0):,} book updates, {s.get('reconnects', 0)} reconnects"
    if not s.get("connected"):
        return f"🛑 {venue}: disconnected ({counts})"
    age = s.get("last_update_age_s")
    if s.get("updates") and age is not None:
        # Judge the stream by when it last delivered, not by whether every
        # coin's 10 s quote happens to be fresh at this instant.
        if _stream_stalled(s):
            return f"⚠️ {venue}: connected but no book update for {int(age // 60)}m ({counts})"
        return f"✅ {venue}: streaming, last update {int(age)}s ago ({counts})"
    if not s.get("fresh_assets"):
        return (f"⚠️ {venue}: connected but no fresh book data ({counts})"
                if s.get("updates") else
                f"⚪ {venue}: connected, no book data received (provider may block this server)")
    return f"✅ {venue}: {counts}"


def _prediction_health_revision() -> str:
    """Git blob ID of the actual deployed health checks, not a CI-upload claim.

    The running file's fingerprint can be compared with GitHub's blob SHA,
    exposing stale Discloud code without disclosing credentials or logs.
    """
    from parlay_bot.discord_app import research_alerts

    try:
        content = Path(research_alerts.__file__).read_bytes()
    except (OSError, TypeError):
        return "unavailable"
    return sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()[:10]


def assess(facts: StatusFacts) -> tuple[str, tuple[str, ...]]:
    """Severity and a stable signature of current problems (not counters)."""
    now = facts.now
    keys: list[str] = []
    down = not facts.ready
    if not facts.ready:
        keys.append("discord:not_ready")
    hb = facts.heartbeat
    if hb is not None and hb.get("consecutive_problems", 0) >= HEARTBEAT_FAILING_AFTER:
        keys.append("heartbeat:failing")
        down = True
    for worker in facts.workers:
        if worker["state"] == "stopped":
            keys.append(f"worker:{worker['name']}")
            down = True
    if facts.incidents is None:
        keys.append("watchdog:unreadable")
    else:
        keys.extend(f"incident:{i['issue_key']}" for i in facts.incidents)
    if facts.feeds is not None:
        for feed in facts.feeds:
            last = _parse(feed.get("last_success_at"))
            if feed.get("status") == "blocked":
                continue  # intentionally off (provider refuses this server)
            if last is None or now - last > STALE_FEED_AFTER:
                keys.append(f"feed:{feed['venue']}/{feed['asset']}")
    if facts.streams:
        for venue, state in facts.streams.items():
            if not state.get("connected"):
                keys.append(f"stream:{venue}")
            elif _stream_stalled(state) and now - facts.boot_at > STARTUP_GRACE:
                keys.append(f"stream_stalled:{venue}")
    if facts.liquidity_last_bucket is not None or facts.streams:
        last = _parse(facts.liquidity_last_bucket)
        if last is None or now - last > STALE_LIQUIDITY_AFTER:
            keys.append("liquidity:stale")
    if facts.memory and facts.memory.get("pressure"):
        keys.append("memory:pressure")
    keys.extend(f"research:{key}" for key, _ in facts.research or ())
    severity = "down" if down else ("degraded" if keys else "healthy")
    return severity, tuple(sorted(keys))


def build_embed(facts: StatusFacts) -> tuple[discord.Embed, str, tuple[str, ...]]:
    severity, signature = assess(facts)
    now = facts.now
    embed = discord.Embed(
        title=f"RivaL Status • {LABELS[severity]}",
        description=(
            f"Process up since {discord.utils.format_dt(facts.boot_at, 'f')} "
            f"({_ago(facts.boot_at, now).replace(' ago', '')}) • Discord "
            f"{'connected' if facts.ready else 'NOT connected'}"
        ),
        color=COLORS[severity], timestamp=now,
    )
    hb = facts.heartbeat
    if hb is None:
        heartbeat = "⚪ Not configured (set RIVAL_HEALTHCHECKS_PING_URL)"
    else:
        ok = hb.get("consecutive_problems", 0) == 0
        heartbeat = (f"{'✅' if ok else '⚠️'} Healthchecks.io • last good ping "
                     f"{_ago(_parse(hb.get('last_success_at')), now)}")
        if hb.get("last_problem"):
            heartbeat += (f"\nLast problem {_ago(_parse(hb.get('last_problem_at')), now)}: "
                          f"{hb['last_problem']}"
                          + (f" ({hb['consecutive_problems']} in a row)"
                             if hb.get("consecutive_problems") else ""))
    embed.add_field(name="External heartbeat", value=heartbeat[:1024], inline=False)

    active = [w for w in facts.workers if w["state"] != "disabled"]
    running = sum(w["state"] == "running" for w in active)
    lines = [f"{'✅' if running == len(active) else '⚠️'} {running}/{len(active)} running"]
    lines += [f"🛑 `{w['name']}` stopped" for w in active if w["state"] == "stopped"]
    lines += [f"↻ `{w['name']}` restarted {w['restarts_last_hour']}× in the last hour"
              for w in active if w.get("restarts_last_hour")]
    embed.add_field(name="Background workers", value=_clip(lines), inline=False)

    if facts.incidents is None:
        incidents = "⚠️ Watchdog incident table unreadable (database slow or down)"
    elif not facts.incidents:
        incidents = "✅ None open"
    else:
        incidents = _clip([
            f"• `{i['issue_key']}` since {_ago(_parse(i.get('first_seen_at')), now)}: "
            f"{str(i.get('detail') or '')[:140]}"
            for i in facts.incidents
        ])
    embed.add_field(name="Open watchdog incidents", value=incidents, inline=False)

    if facts.feeds is None:
        feeds = "⚪ Market collection disabled or unreadable"
    else:
        stale, off = [], []
        for feed in facts.feeds:
            last = _parse(feed.get("last_success_at"))
            if feed.get("status") == "blocked":
                off.append(f"⚪ {feed['venue']}/{feed['asset']}: off (provider blocks this server)")
                continue
            if last is None or now - last > STALE_FEED_AFTER:
                stale.append(
                    f"• {feed['venue']}/{feed['asset']}: last data {_ago(last, now)}"
                    + (f", {feed['failure_count']} failures" if feed.get("failure_count")
                       else "")
                    + (f" ({feed['status']})" if feed.get("status") else ""))
        active = len(facts.feeds) - len(off)
        fresh = active - len(stale)
        summary = (f"{'✅' if not stale else '⚠️'} {fresh}/{active} "
                   "exchange feeds fresh (<20 min)")
        feeds = _clip([summary, *stale, *off])
    embed.add_field(name="Market data", value=feeds, inline=False)

    if facts.streams:
        stream_lines = [_stream_line(venue, s) for venue, s in facts.streams.items()]
        last = _parse(facts.liquidity_last_bucket)
        stream_lines.append(
            f"{'✅' if last and now - last <= STALE_LIQUIDITY_AFTER else '⚠️'} "
            f"Liquidity record: newest saved 5-min bucket began {_ago(last, now)}")
        embed.add_field(name="Live streams", value=_clip(stream_lines), inline=False)

    if facts.research is not None:
        embed.add_field(
            name="Prediction health",
            # _clip() of an empty list returns "—", which hid this message.
            value=(_clip([text for _, text in facts.research]) if facts.research
                   else "✅ No problems: forecasts flowing, six-year model fresh, "
                        "accuracy above the up-rate, RivaL Intelligence reviewing"),
            inline=False)

    if facts.memory:
        memory = facts.memory
        text = f"{memory['rss_mb']} / {memory['limit_mb']} MB"
        if memory.get("pressure"):
            text = "⚠️ " + text + " (pressure: optional work paused)"
        if memory.get("oom_kills"):
            text += f" • {memory['oom_kills']} OOM kills since host start"
        embed.add_field(name="Memory", value=text, inline=True)

    totals = facts.problem_totals or {}
    open_incidents = (None if facts.incidents is None
                      else {str(i["issue_key"]) for i in facts.incidents})
    lines = problem_lines(facts.problems, now=now, boot_at=facts.boot_at, totals=totals,
                          open_incidents=open_incidents)
    embed.add_field(name="Recent errors and warnings", value=_clip(lines), inline=False)
    embed.set_footer(text="Posted when problems change and once a day • "
                          f"Prediction checks {_prediction_health_revision()}")
    return embed, severity, signature


def problem_state(group, *, now: datetime, boot_at: datetime,
                  open_incidents: set[str] | None = None) -> str:
    """recovered | active | startup | stopped | quiet.

    recovered: a watchdog HEALTH ALERT whose incident has since closed.

    stopped: not seen since this process (and its code) started.
    startup: seen only while workers were starting after the restart.
    quiet:   seen after startup but not within ACTIVE_WINDOW.
    active:  repeated within ACTIVE_WINDOW after startup.
    """
    # Keys may contain colons (feed:hyperliquid:ETH), so match the prefix.
    if (open_incidents is not None and group.text.startswith("RivaL HEALTH ALERT ")
            and not any(group.text.startswith(f"RivaL HEALTH ALERT {key}: ")
                        for key in open_incidents)):
        return "recovered"
    last = group.last_at
    if last < boot_at:
        return "stopped"
    if last <= boot_at + STARTUP_GRACE:
        return "startup"
    return "active" if now - last <= ACTIVE_WINDOW else "quiet"


def problem_lines(problems, *, now: datetime, boot_at: datetime, totals: dict,
                  open_incidents: set[str] | None = None) -> list[str]:
    """Card lines: what still needs attention first, then what has stopped."""
    states: dict[str, list] = {"active": [], "quiet": [], "startup": [], "recovered": [],
                               "stopped": []}
    for group in problems:
        states[problem_state(group, now=now, boot_at=boot_at,
                             open_incidents=open_incidents)].append(group)
    started = discord.utils.format_dt(boot_at, "t")
    lines = [
        # A warning seen only in the minutes after a restart comes back after
        # the next restart: it is restart noise, not "fixed". Counted apart.
        (f"🔴 {len(states['active'])} still happening • 🟢 {len(states['quiet'])} quiet 1h+ • "
         f"⚪ {len(states['startup'])} only right after the restart • "
         f"✅ {len(states['stopped']) + len(states['recovered'])} not seen since"),
        (f"({totals.get('ERROR', 0)} errors • {totals.get('WARNING', 0)} warnings in the last "
         "24 h, all restarts; ×counts are 24 h totals)"),
    ]
    for g in states["active"]:
        lines.append(f"🔴 `{g.count}×` **{g.level}** {g.logger.rsplit('.', 1)[-1]}: "
                     f"{g.text} ({_ago(g.last_at, now)})")
    for g in states["quiet"]:
        lines.append(f"🟢 `{g.count}×` {g.logger.rsplit('.', 1)[-1]}: {g.text[:90]} "
                     f"(last {_ago(g.last_at, now)})")
    for g in states["startup"]:
        lines.append(f"⚪ `{g.count}×` {g.logger.rsplit('.', 1)[-1]}: {g.text[:70]} "
                     f"(only in the first minutes after the {started} restart)")
    for g in states["recovered"]:
        lines.append(f"✅ `{g.count}×` {g.logger.rsplit('.', 1)[-1]}: {g.text[:70]} "
                     f"(recovered: incident closed; last alert {_ago(g.last_at, now)})")
    for g in states["stopped"]:
        lines.append(f"✅ `{g.count}×` {g.logger.rsplit('.', 1)[-1]}: {g.text[:70]} "
                     f"(not since restart at {started})")
    if not problems:
        lines.append("None")
    return lines


class StatusPostPolicy:
    """Post on a confirmed change of problem set, or daily."""

    def __init__(self) -> None:
        self.last_posted_signature: tuple[str, ...] | None = None
        self.last_posted_at: datetime | None = None
        self._pending: tuple[str, ...] | None = None

    def should_post(self, signature: tuple[str, ...], now: datetime) -> bool:
        if self.last_posted_at is None:
            return True
        if now - self.last_posted_at >= DAILY:
            return True
        if signature == self.last_posted_signature:
            self._pending = None
            return False
        # Require the same new state on two consecutive checks so a single
        # slow query or reconnect blip does not post twice.
        if self._pending == signature:
            return True
        self._pending = signature
        return False

    def posted(self, signature: tuple[str, ...], now: datetime) -> None:
        self.last_posted_signature, self.last_posted_at = signature, now
        self._pending = None
