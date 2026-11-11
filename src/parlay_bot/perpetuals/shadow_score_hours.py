"""The 7-day test-model scores, from stored hourly tallies plus recent rows.

The watchdog's challenger check (regression alerts, then early stop) used to
aggregate every graded test-model forecast of the last 7 days in one pass,
about 2 million rows with no index to help, every 10 minutes. On production
it grew to 60 s, hit the statement timeout and was retried on every pass,
holding a pooled connection each time; and while it failed, early stop never
received fresh scores.

Graded test-model rows almost never change: settlement copies only a 0/1
outcome, and the only later edit is the rare no-trade withdrawal, which
clears a row's scores. So each (stream, family, issued hour) is tallied once,
when its rows are graded and at least ``FINAL_AFTER`` old, and stored. A pass
then reads only:

- the stored tallies for whole hours inside the window;
- live rows for the window's first, partial hour (exactly ``issued_at >=
  now - 7 days``, as before) and for the last ``FINAL_AFTER``.

The result is the same rows and the same numbers as the old single query.
A withdrawal invalidates the hours it may touch, so they are tallied again.
The first time, the window is built ``BUILD_HOURS`` hours per exchange per
pass; until it is complete the caller keeps its last verdict.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

WINDOW = timedelta(days=7)
FINAL_AFTER = timedelta(hours=6)
# A tally waits for every row of its hour to be graded, but not forever:
# a row that has not settled 48 hours on will not (no target left to grade).
GIVE_UP_AFTER = timedelta(hours=48)
BUILD_HOURS = 12


class NoVenues(LookupError):
    """No exchange list yet (fresh install): the caller scans the small ledger."""

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS rival_shadow_score_hours (
        venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
        horizon_minutes INTEGER NOT NULL, family TEXT NOT NULL, hour TEXT NOT NULL,
        n INTEGER NOT NULL, model_sum DOUBLE PRECISION NOT NULL,
        incumbent_sum DOUBLE PRECISION NOT NULL, loss_sq_sum DOUBLE PRECISION NOT NULL,
        first_day TEXT NOT NULL, last_day TEXT NOT NULL, last_issued TEXT NOT NULL,
        PRIMARY KEY (venue,contract,quote,horizon_minutes,family,hour))""",
    """CREATE TABLE IF NOT EXISTS rival_shadow_score_built (
        venue TEXT NOT NULL, hour TEXT NOT NULL, built_at TEXT NOT NULL,
        PRIMARY KEY (venue,hour))""",
)

# Same filter as the old aggregate; the hour is the issued_at prefix
# ("2026-11-11T13"), identical on SQLite and PostgreSQL text columns.
_TALLY = """SELECT contract,quote,horizon_minutes,family,
                   SUBSTR(issued_at,1,13) AS hour,
                   COUNT(*) AS n, SUM(model_brier) AS model_sum,
                   SUM(incumbent_brier) AS incumbent_sum,
                   SUM((model_brier-incumbent_brier)*(model_brier-incumbent_brier))
                       AS loss_sq_sum,
                   MIN(SUBSTR(target_opened_at,1,10)) AS first_day,
                   MAX(SUBSTR(target_opened_at,1,10)) AS last_day,
                   MAX(issued_at) AS last_issued
            FROM rival_model_shadow_forecasts
            WHERE venue=? AND issued_at>=? AND issued_at<?
              AND settled_at IS NOT NULL
              AND model_brier IS NOT NULL AND incumbent_brier IS NOT NULL
            GROUP BY contract,quote,horizon_minutes,family,SUBSTR(issued_at,1,13)"""


def initialize(store) -> None:
    if getattr(store, "_shadow_score_hours_ready", False):
        return
    with store.connect() as conn:
        for statement in SCHEMA:
            conn.execute(statement)
    try:
        store._shadow_score_hours_ready = True
    except AttributeError:
        pass


def _hour(stamp: datetime) -> datetime:
    return stamp.replace(minute=0, second=0, microsecond=0)


def _key(stamp: datetime) -> str:
    return stamp.isoformat()[:13]


def invalidate(conn, *, since: str | None) -> None:
    """Mark hours that a withdrawal may have changed for a fresh tally.

    ``since`` is the withdrawal's settled_at cutoff (None: the whole ledger).
    A row settles after it is issued, so every changed row was issued at or
    after ``since`` minus the longest wait to settle; a day covers it. Runs
    in the withdrawal's own transaction (call ``initialize`` first), so it
    never swallows an error that would roll that transaction back unseen.
    """
    if since is None:
        conn.execute("DELETE FROM rival_shadow_score_built")
        return
    start = datetime.fromisoformat(since) - timedelta(days=1)
    conn.execute("DELETE FROM rival_shadow_score_built WHERE hour>=?",
                 (_key(start.astimezone(UTC)),))


def _venues(store) -> list[str]:
    """Every exchange with test-model rows: collectors, scored streams, tallies.

    Each source on its own connection: on PostgreSQL a failed statement
    aborts its transaction, so a missing optional table must not share one.
    """
    names: set[str] = set()
    for sql in ("SELECT DISTINCT venue FROM perp_feed_health",
                "SELECT DISTINCT venue FROM rival_family_promotion",
                "SELECT DISTINCT venue FROM rival_shadow_score_built"):
        try:
            with store.connect() as conn:
                names |= {str(r["venue"]) for r in conn.execute(sql).fetchall()}
        except Exception:  # noqa: BLE001, S112 - an optional source, not a fault
            continue
    return sorted(names)


def _build(conn, venue: str, start: datetime, end: datetime, now: datetime) -> None:
    """Tally [start, end) for one exchange and mark those hours final.

    The caller passes only hours whose rows are all graded (or past
    ``GIVE_UP_AFTER``), so the stored tally never changes afterwards
    except through ``invalidate``.
    """
    rows = conn.execute(_TALLY, (venue, start.isoformat(), end.isoformat())).fetchall()
    conn.execute(
        "DELETE FROM rival_shadow_score_hours WHERE venue=? AND hour>=? AND hour<?",
        (venue, _key(start), _key(end)))
    for r in rows:
        conn.execute(
            """INSERT INTO rival_shadow_score_hours (venue,contract,quote,horizon_minutes,
                 family,hour,n,model_sum,incumbent_sum,loss_sq_sum,first_day,last_day,
                 last_issued) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, r["contract"], r["quote"], int(r["horizon_minutes"]), r["family"],
             r["hour"], int(r["n"]), float(r["model_sum"]), float(r["incumbent_sum"]),
             float(r["loss_sq_sum"]), r["first_day"], r["last_day"], r["last_issued"]))
    hour = start
    while hour < end:
        conn.execute(
            """INSERT INTO rival_shadow_score_built (venue,hour,built_at) VALUES (?,?,?)
               ON CONFLICT (venue,hour) DO UPDATE SET built_at=excluded.built_at""",
            (venue, _key(hour), now.isoformat()))
        hour += timedelta(hours=1)


def _hours(start: datetime, end: datetime):
    hour = start
    while hour < end:
        yield hour
        hour += timedelta(hours=1)


def window_scores(store, now: datetime) -> list[dict] | None:
    """Per stream, horizon and family: n, mean scores, loss square, days, last issued.

    Exactly the rows ``issued_at >= now - 7 days`` that are graded with both
    scores, as the old single aggregate. None while the stored tallies do
    not yet cover the window (first run, or after a full-ledger withdrawal):
    the caller keeps its last verdict.
    """
    now = now.astimezone(UTC)
    cutoff = now - WINDOW
    first_whole = _hour(cutoff) + timedelta(hours=1)
    final_cut = _hour(now - FINAL_AFTER)
    initialize(store)
    venues = _venues(store)
    if not venues:
        raise NoVenues("no exchange is known yet; the ledger is new and small")
    with store.connect() as conn:
        return _window(conn, venues, now, cutoff, first_whole, final_cut)


def _window(conn, venues, now, cutoff, first_whole, final_cut) -> list[dict] | None:
    # Hours that still have ungraded rows: read live until graded. One read
    # on the unsettled index (settled_at IS NULL is a small set).
    grading = {(str(r["venue"]), str(r["hour"])) for r in conn.execute(
        """SELECT DISTINCT venue, SUBSTR(issued_at,1,13) AS hour
           FROM rival_model_shadow_forecasts
           WHERE settled_at IS NULL AND target_opened_at>=? AND issued_at>=?""",
        # target_opened_at bounds the (settled_at, target_opened_at) index
        # range: never-settling rows (no-trade targets) pile up forever, so
        # "settled_at IS NULL" alone read all of them (7 s, Nov 11). A
        # target opens at most an hour before its forecast is issued.
        ((cutoff - timedelta(hours=1)).isoformat(), cutoff.isoformat())).fetchall()}

    def waiting(venue: str, hour: datetime) -> bool:
        return (venue, _key(hour)) in grading and now - hour < GIVE_UP_AFTER

    def built_hours(venue: str) -> set[str]:
        return {str(r["hour"]) for r in conn.execute(
            "SELECT hour FROM rival_shadow_score_built WHERE venue=? AND hour>=? AND hour<?",
            (venue, _key(first_whole), _key(final_cut))).fetchall()}

    complete = True
    for venue in venues:
        built = built_hours(venue)
        todo = [h for h in _hours(first_whole, final_cut)
                if _key(h) not in built and not waiting(venue, h)]
        if not todo:
            continue
        # The oldest contiguous run of hours to tally, at most BUILD_HOURS.
        end = todo[0]
        for hour in todo:
            if hour != end or hour - todo[0] >= timedelta(hours=BUILD_HOURS):
                break
            end = hour + timedelta(hours=1)
        _build(conn, venue, todo[0], end, now)
        if any(h >= end for h in todo):
            complete = False
    if not complete:
        return None

    groups: dict[tuple, dict] = {}

    def add(venue, row) -> None:
        key = (venue, str(row["contract"]), str(row["quote"]),
               int(row["horizon_minutes"]), str(row["family"]))
        g = groups.setdefault(key, {"n": 0, "model": 0.0, "incumbent": 0.0, "loss": 0.0,
                                    "days": set(), "last": ""})
        g["n"] += int(row["n"])
        g["model"] += float(row["model_sum"])
        g["incumbent"] += float(row["incumbent_sum"])
        g["loss"] += float(row["loss_sq_sum"])
        g["days"] |= {str(row["first_day"]), str(row["last_day"])}
        g["last"] = max(g["last"], str(row["last_issued"]))

    for venue in venues:
        built = built_hours(venue)
        for row in conn.execute(
                """SELECT * FROM rival_shadow_score_hours
                   WHERE venue=? AND hour>=? AND hour<?""",
                (venue, _key(first_whole), _key(final_cut))).fetchall():
            if str(row["hour"]) in built:
                add(venue, row)
        # Live: the window's partial first hour (the exact cutoff), any hour
        # still grading, and everything from the last FINAL_AFTER on.
        spans = [(cutoff.isoformat(), first_whole.isoformat())]
        spans += [(h.isoformat(), (h + timedelta(hours=1)).isoformat())
                  for h in _hours(first_whole, final_cut) if _key(h) not in built]
        spans.append((final_cut.isoformat(), "9999"))
        for start, end in spans:
            for row in conn.execute(_TALLY, (venue, start, end)).fetchall():
                add(venue, row)

    return [{"venue": v, "contract": c, "quote": q, "horizon_minutes": h, "family": f,
             "n": g["n"], "model_score": g["model"] / g["n"],
             "incumbent_score": g["incumbent"] / g["n"],
             "loss_square": g["loss"] / g["n"], "days": len(g["days"]),
             "last_issued": g["last"]}
            for (v, c, q, h, f), g in sorted(groups.items()) if g["n"]]
