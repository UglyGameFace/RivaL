"""RivaL status card for an operator Discord channel.

Built only from real runtime state: the Healthchecks.io heartbeat outcome,
background worker tasks and restarts, persisted watchdog incidents, per-feed
collection freshness, websocket feeds, the liquidity record, memory, and the
recent warning/error log digest. Posted when the problem set changes and
once a day, never on a timer that floods the channel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path

import discord

from parlay_bot.discord_app.answer_cards import rename_text
from parlay_bot.discord_app.embed_text import fit_embed_field_value

STALE_FEED_AFTER = timedelta(minutes=20)
HEARTBEAT_PROBLEM_CHARS = 300
STALE_LIQUIDITY_AFTER = timedelta(minutes=15)
# A live book stream that has delivered nothing for this long is a problem
# (counted in the status, so the card reposts when it stops and recovers).
STREAM_STALLED_AFTER = timedelta(minutes=2)
# Streams need a few minutes to connect after a restart before "stalled" counts.
STREAM_STARTUP_GRACE = timedelta(minutes=5)
HEARTBEAT_FAILING_AFTER = 3  # consecutive failed pings
DAILY = timedelta(hours=24)
# A logged problem counts as "still happening" only if it repeated within
# this window; startup noise is the burst while every worker starts at once.
ACTIVE_WINDOW = timedelta(minutes=60)
STARTUP_GRACE = timedelta(minutes=10)
COLORS = {"healthy": discord.Color.green(), "degraded": discord.Color.orange(),
          "down": discord.Color.red()}
LABELS = {"healthy": "🟢 All good", "degraded": "🟡 Something needs a look",
          "down": "🔴 Not working right"}


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
    # Whether the Intelligence review check (check 4 in research_alerts)
    # actually ran. False when the Intelligence background task was never
    # created (background opt-in off): an empty research list then proves
    # only the other three checks, and the card must not claim a review.
    # Default True keeps every existing constructor meaning "check 4 ran".
    intelligence_checked: bool = True


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


# One test model's clock, as research_alerts writes it (tests pin that form).
_PROGRESS = re.compile(
    r"^📊 (?P<family>\S+): day (?P<days>\d+)/(?P<min_days>\d+) · "
    r"(?P<targets>\d+)/(?P<min_targets>\d+) targets"
    r"(?: · gain (?P<gain>[+-]?\d+(?:\.\d+)?))?$")
# Below this many graded results a score is not a verdict (audit rule 6).
TOO_FEW_TO_JUDGE = 20
# A score difference smaller than this reads as level, not ahead or behind.
LEVEL_WITHIN = 0.0005
SHOWN_CLOSEST = 3
# Test models the shared name table does not cover yet.
_FAMILY_WORDS = {
    "calib_incumbent_v1": "calibrated main model",
    "liq_v1": "liquidity test",
    "flow_v1": "order-flow test 1",
    "flow_v2": "order-flow test 2",
    "flow_v3": "order-flow test 3",
    "archive_v1": "archive test",
    "gbm_v1": "boosted-trees test",
    "regime_v2": "trend test 2",
}


def family_words(family: str) -> str:
    """A test model's everyday name; each 6-year model version folds into one."""
    per_exchange = family.endswith("_venue")
    base = family.removesuffix("_venue")
    pooled = re.fullmatch(r"pooled_(\d+)m_[0-9a-f]+", base)
    if pooled:
        name = f"6-year history model ({pooled.group(1)}-min)"
    else:
        name = _FAMILY_WORDS.get(base) or rename_text(base)
    return name + (", per exchange" if per_exchange else "")


def _verdict(targets: int, gain: str | None) -> str:
    if targets < TOO_FEW_TO_JUDGE:
        return "too few results to judge"
    if gain is None:
        return "no score yet"
    value = float(gain)
    if value > LEVEL_WITHIN:
        return "ahead of the main model so far"
    if value < -LEVEL_WITHIN:
        return "behind the main model"
    return "level with the main model"


def prediction_lines(research) -> list[str]:
    """The Predictions field: a count, the closest few, then the rest as before.

    One line per test model listed every internal id with its raw clock
    (e.g. "pooled_15m_1a10acfb6945: day 8/21 · 649/150 targets · gain
    +0.008"). This answers how many are being tested and how far the
    leaders are, in plain words, and keeps the other research lines.
    """
    progress, others = [], []
    for key, text in research:
        match = _PROGRESS.match(text) if str(key).startswith("shadow-progress:") else None
        if match:
            progress.append(match)
        else:
            others.append(text)
    if not progress:
        return list(others)
    groups: dict[str, list] = {}
    for match in progress:
        groups.setdefault(family_words(match["family"]), []).append(match)
    leaders = sorted(
        ((name, len(matches), max(matches, key=lambda m: (int(m["days"]),
                                                          int(m["targets"]))))
         for name, matches in groups.items()),
        key=lambda entry: (-int(entry[2]["days"]), -int(entry[2]["targets"]), entry[0]),
    )
    min_days = int(leaders[0][2]["min_days"])
    furthest = int(leaders[0][2]["days"])
    count = len(leaders)
    summary = (f"📊 {count} test model{'s' if count != 1 else ''} being tested. "
               f"The furthest along is on day {furthest} of {min_days}.")
    lines = [summary]
    for name, versions, lead in leaders[:SHOWN_CLOSEST]:
        targets, needed = int(lead["targets"]), int(lead["min_targets"])
        results = ("enough results" if targets >= needed
                   else f"{targets} of {needed} results")
        label = name + (f" ({versions} versions)" if versions > 1 else "")
        lines.append(f"• {label}: day {lead['days']} of {lead['min_days']}, "
                     f"{results}, {_verdict(targets, lead['gain'])}")
    if count > SHOWN_CLOSEST:
        lines.append(f"• and {count - SHOWN_CLOSEST} more, further behind")
    return [*lines, *others]


def _job(text: str) -> str:
    """Which part of the bot a database line came from, in plain words."""
    owner = _field(text, "owner")
    if not owner:
        return ""
    module, _, function = owner.split(":", 1)[0].rpartition(".")
    words = " ".join(part.replace("_", " ").strip() for part in (module, function) if part)
    return f" ({words})" if words else ""


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
    reconnects = s.get("reconnects", 0)
    counts = (f"{s.get('updates', 0):,} updates, {reconnects} "
              f"reconnect{'' if reconnects == 1 else 's'}")
    if not s.get("connected"):
        return f"🛑 {venue}: disconnected ({counts})"
    age = s.get("last_update_age_s")
    if s.get("updates") and age is not None:
        # Judge the stream by when it last delivered, not by whether every
        # coin's 10 s quote happens to be fresh at this instant.
        if _stream_stalled(s):
            return f"⚠️ {venue}: connected but nothing new for {int(age // 60)} min ({counts})"
        return f"✅ {venue}: live, last update {int(age)}s ago ({counts})"
    if not s.get("fresh_assets"):
        return (f"⚠️ {venue}: connected but nothing recent ({counts})"
                if s.get("updates") else
                f"⚪ {venue}: connected but nothing received (the exchange may block this server)")
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
            elif _stream_stalled(state) and now - facts.boot_at > STREAM_STARTUP_GRACE:
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


def _sub(lines) -> list[str]:
    """Problem detail under its check line, indented so the check stays scannable."""
    return [f" {line}" for line in lines]


def _predictions_line(facts: StatusFacts) -> str:
    """One line that claims only what research_alerts' checks actually proved.

    Check 4 (a fresh RivaL Intelligence review) is claimed only when it ran;
    otherwise the reviews are said to be off, never given an invented time.
    """
    review = ("AI reviewed within the last 2 hours" if facts.intelligence_checked
              else "AI reviews off (background opt-in off)")
    return ("✅ **Predictions:** new ones in the last 20 min · 6-year model passed its "
            "gates and covers the last finished month · no in-use 6-year version "
            "significantly worse than a simple guess over 3 days (needs 300+ graded "
            f"results for a verdict) · {review}")


def build_embed(facts: StatusFacts) -> tuple[discord.Embed, str, tuple[str, ...]]:
    """One Checks list (a line per system, detail only under what is wrong),
    then predictions and errors only when they have something to say."""
    severity, signature = assess(facts)
    now = facts.now
    head = [(f"Up {_ago(facts.boot_at, now).replace(' ago', '')} "
             f"(since {discord.utils.format_dt(facts.boot_at, 'f')})"),
            "Connected to Discord" if facts.ready else "⚠️ NOT connected to Discord"]
    if facts.memory:
        memory = facts.memory
        text = f"memory {memory['rss_mb']}/{memory['limit_mb']} MB"
        if memory.get("pressure"):
            text = "⚠️ " + text + " (running low: extra work paused)"
        if memory.get("oom_kills"):
            text += f", {memory['oom_kills']} out-of-memory crashes since the server started"
        head.append(text)
    embed = discord.Embed(
        title=f"RivaL Status • {LABELS[severity]}", description=" · ".join(head),
        color=COLORS[severity], timestamp=now,
    )
    checks: list[str] = []

    hb = facts.heartbeat
    if hb is None:
        checks.append("⚪ **Uptime ping:** not set up (set RIVAL_HEALTHCHECKS_PING_URL)")
    else:
        ok = hb.get("consecutive_problems", 0) == 0
        checks.append(f"{'✅' if ok else '⚠️'} **Uptime ping:** Healthchecks.io check-in "
                      f"{_ago(_parse(hb.get('last_success_at')), now)}")
        if hb.get("last_problem"):
            # The provider's message can be long: cut it at a word, never mid-word.
            said = fit_embed_field_value(str(hb["last_problem"]), budget=HEARTBEAT_PROBLEM_CHARS)
            checks += _sub([
                f"last failed {_ago(_parse(hb.get('last_problem_at')), now)}: {said}"
                + (f" ({hb['consecutive_problems']} in a row)"
                   if hb.get("consecutive_problems") else "")])

    active = [w for w in facts.workers if w["state"] != "disabled"]
    running = sum(w["state"] == "running" for w in active)
    checks.append(f"{'✅' if running == len(active) else '⚠️'} **Jobs:** "
                  f"{running} of {len(active)} running")
    checks += _sub([f"🛑 {w['name']}: stopped" for w in active if w["state"] == "stopped"])
    checks += _sub([f"↻ {w['name']}: restarted {w['restarts_last_hour']}× in the last hour"
                    for w in active if w.get("restarts_last_hour")])

    if facts.incidents is None:
        checks.append("⚠️ **Alerts:** couldn't read the alert list (database slow or down)")
    elif not facts.incidents:
        checks.append("✅ **Alerts:** none open")
    else:
        checks.append(f"⚠️ **Alerts:** {len(facts.incidents)} open")
        checks += _sub([
            f"• `{i['issue_key']}` since {_ago(_parse(i.get('first_seen_at')), now)}: "
            f"{fit_embed_field_value(str(i.get('detail') or ''), budget=120)}" for i in facts.incidents])

    if facts.feeds is None:
        checks.append("⚪ **Prices:** collection is off or couldn't be read")
    else:
        stale, off = [], []
        for feed in facts.feeds:
            last = _parse(feed.get("last_success_at"))
            if feed.get("status") == "blocked":
                off.append(f"{feed['venue']}/{feed['asset']}")
                continue
            if last is None or now - last > STALE_FEED_AFTER:
                stale.append(
                    f"• {feed['venue']}/{feed['asset']}: last prices {_ago(last, now)}"
                    + (f", {feed['failure_count']} failures" if feed.get("failure_count")
                       else "")
                    + (f" ({feed['status']})" if feed.get("status") else ""))
        live = len(facts.feeds) - len(off)
        checks.append(f"{'✅' if not stale else '⚠️'} **Prices:** {live - len(stale)} of "
                      f"{live} up to date (new prices in the last 20 min)")
        checks += _sub(stale)
        if off:
            checks += _sub([f"⚪ off, the exchange blocks this server: {', '.join(off)}"])

    if facts.streams:
        lines = [_stream_line(venue, s) for venue, s in facts.streams.items()]
        good = sum(line.startswith("✅") for line in lines)
        # ⚪ = the exchange sends nothing to this server: off, not failing.
        off = sum(line.startswith("⚪") for line in lines)
        last = _parse(facts.liquidity_last_bucket)
        saved = last is not None and now - last <= STALE_LIQUIDITY_AFTER
        healthy = good == len(lines) - off and saved
        checks.append(f"{'✅' if healthy else '⚠️'} **Order books:** "
                      f"{good} of {len(lines) - off} live · trading activity saved "
                      f"{_ago(last, now)}")
        checks += _sub([line for line in lines if not line.startswith("✅")])

    if facts.research is not None and not facts.research:
        checks.append(_predictions_line(facts))

    totals = facts.problem_totals or {}
    open_incidents = (None if facts.incidents is None
                      else {str(i["issue_key"]) for i in facts.incidents})
    if not facts.problems:
        checks.append(f"✅ **Errors:** none in the last 24 h "
                      f"({totals.get('ERROR', 0)} errors, {totals.get('WARNING', 0)} warnings)")
    embed.add_field(name="Checks", value=_clip(checks), inline=False)

    if facts.research:
        embed.add_field(name="Predictions", value=_clip(prediction_lines(facts.research)),
                        inline=False)
    if facts.problems:
        lines = problem_lines(facts.problems, now=now, boot_at=facts.boot_at,
                              totals=totals, open_incidents=open_incidents)
        embed.add_field(name="Errors and warnings (last 24 h)", value=_clip(lines),
                        inline=False)
    embed.set_footer(text="Posts when something changes, and once a day • "
                          f"Check version {_prediction_health_revision()}")
    return embed, severity, signature


_KV = re.compile(r"\b(\w+)=(\S+)")


def _field(text: str, key: str) -> str | None:
    for k, v in _KV.findall(text):
        if k == key:
            return v.strip(",;")
    return None


def plain(text: str) -> str:
    """Log text in everyday words, keeping which exchange/coin it was about.

    The exact message (with its error type) stays in the bot's logs; this
    never adds a cause the message does not state.
    """
    words = _plain(text)
    t = text.removeprefix("RivaL ")
    venue = _field(t, "venue") or _field(t, "source")
    asset = _field(t, "asset")
    where = "/".join(x for x in (venue, asset) if x)
    if where and (venue or "").lower() not in words.lower():
        words += f" ({where})"
    return words


def _plain(text: str) -> str:
    t = text.removeprefix("RivaL ").strip()
    low = t.lower()
    venue = _field(t, "venue") or _field(t, "source")
    m = re.match(r"(\w+) (liquidity )?stream", t)
    name = venue or (m.group(1) if m else None)
    if low.startswith("health alert"):
        key, _, detail = t[len("HEALTH ALERT "):].partition(": ")
        return f"Health alert ({key}): {detail or key}"
    if "event loop blocked" in low:
        return "The bot froze for over a second while busy"
    if "postgresql connection held" in low:
        return "A database job took a long time" + _job(t)
    if "postgresql slow operation" in low:
        return "A database query took over 2 seconds" + _job(t)
    if "rate limited" in low:
        return f"{name or 'An exchange'} asked the bot to slow down"
    if "blocked" in low and ("region" in low or "451" in low) or "refuses this host" in low:
        return f"{name or 'An exchange'} blocks this server's location"
    if "stream" in low and "disconnected" in low:
        return f"{name or 'A live feed'} dropped and reconnected"
    if "historical recovery failed" in low:
        h = re.match(r"(\w+) historical", t)
        return f"{h.group(1) if h else 'An exchange'}: couldn't download older price history this time"
    if "perp collection failed" in low:
        asset = _field(t, "asset")
        return f"{venue or 'An exchange'}{'/' + asset if asset else ''}: couldn't get prices this time"
    if "intelligence review failed" in low:
        return "RivaL Intelligence couldn't finish a review"
    if low.startswith("guided render timed out"):
        return "A /rival menu took too long to load"
    if "timeout" in low or "timed out" in low:
        return f"{name or 'An outside service'} didn't answer in time"
    if "memory pressure" in low:
        return "Memory ran low, so extra work was paused"
    for needle, words in (
        ("streaming forward proof failed", "After-fee check failed"),
        ("candidate import from drive", "Couldn't load a new model from Google Drive"),
        ("sports settlement snapshot", "Couldn't check finished sports results"),
        ("slash command failed", "A slash command hit an error"),
        ("command sync failed", "Couldn't update the bot's slash commands"),
        ("cold archive", "Old-data archiving hit a problem"),
        ("polymarket", "A Polymarket check hit a problem"),
        ("kalshi", "A Kalshi check hit a problem"),
    ):
        if needle in low:
            return words
    # Drop key=value details and the technical "[ErrorType: ...]" tail.
    words = re.sub(r"\s*\[.*", "", _KV.sub("", t)).split(" | ")[0]
    cleaned = re.sub(r"\s+", " ", words).strip(" ;:,")
    return fit_embed_field_value(cleaned, budget=120) or fit_embed_field_value(t, budget=120)


def _said(g) -> str:
    """Error vs warning stays visible, plus the plain-English message."""
    return ("Error: " if g.level in ("ERROR", "CRITICAL") else "Warning: ") + plain(g.text)


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
        (f"🔴 {len(states['active'])} happening now • 🟢 {len(states['quiet'])} calmed "
         f"down (none in the last hour) • ⚪ {len(states['startup'])} only while starting "
         f"up • ✅ {len(states['stopped']) + len(states['recovered'])} stopped"),
        (f"({totals.get('ERROR', 0)} errors and {totals.get('WARNING', 0)} warnings in "
         "the last 24 h, counting restarts; the × number is how many times each happened)"),
    ]
    for g in states["active"]:
        lines.append(f"🔴 `{g.count}×` **{_said(g)}** ({_ago(g.last_at, now)})")
    for g in states["quiet"]:
        lines.append(f"🟢 `{g.count}×` {fit_embed_field_value(_said(g), budget=100)} (last {_ago(g.last_at, now)})")
    for g in states["startup"]:
        lines.append(f"⚪ `{g.count}×` {fit_embed_field_value(_said(g), budget=80)} (only right after the "
                     f"{started} restart)")
    for g in states["recovered"]:
        lines.append(f"✅ `{g.count}×` {fit_embed_field_value(_said(g), budget=80)} (fixed itself; last alert "
                     f"{_ago(g.last_at, now)})")
    for g in states["stopped"]:
        lines.append(f"✅ `{g.count}×` {fit_embed_field_value(_said(g), budget=80)} (not since the {started} "
                     "restart)")
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
