"""Read-only follow-up check: problem log, cycles, Intelligence saves, bot RSS."""
import base64, gzip, json, os, re
from datetime import UTC, datetime, timedelta
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    rss = {}
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()[:80]
                st = open(f"/proc/{pid}/status").read()
                m = re.search(r"VmRSS:\s+(\d+)", st); h = re.search(r"VmHWM:\s+(\d+)", st)
                if m and int(m.group(1)) > 20000:
                    rss[pid] = {"cmd": cmd, "rss_mb": int(m.group(1)) // 1024,
                                "peak_mb": int(h.group(1)) // 1024 if h else None}
            except OSError:
                pass
    out["processes"] = rss
    for p in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.events"):
        try:
            out[p] = open(p).read().strip()[:300]
        except OSError:
            pass
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    since = "2026-11-05T19:22"
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT template, level, SUM(count) AS n, MAX(last_at) AS last, MIN(hour) AS first_hour
               FROM rival_problem_log WHERE hour>=? GROUP BY template, level ORDER BY n DESC""",
            (since[:13],)).fetchall()
        out["problems_since_boot"] = [{k: str(r[k])[:160] for k in ("template", "level", "n", "last", "first_hour")} for r in rows[:40]]
        hourly = conn.execute(
            """SELECT hour, template, SUM(count) AS n FROM rival_problem_log WHERE hour>=?
               AND (template LIKE '%event loop blocked%' OR template LIKE '%cycle took%'
                    OR template LIKE '%memory%') GROUP BY hour, template ORDER BY hour""",
            (since[:13],)).fetchall()
        out["hourly"] = [[str(r["hour"]), str(r["template"])[:70], int(r["n"])] for r in hourly]
        ic = conn.execute(
            """SELECT substr(issued_at,1,13) AS h, COUNT(*) AS n FROM rival_intelligence_calls
               WHERE issued_at>=? GROUP BY h ORDER BY h""", (since,)).fetchall()
        out["intelligence_saves_per_hour"] = [[str(r["h"]), int(r["n"])] for r in ic]
        cyc = conn.execute(
            """SELECT substr(issued_at,1,13) AS h, COUNT(DISTINCT substr(issued_at,1,16)) AS cycles,
                      COUNT(*) AS n FROM perp_forward_audit WHERE issued_at>=? GROUP BY h ORDER BY h""",
            (since,)).fetchall()
        out["forecast_cycles_per_hour"] = [[str(r["h"]), int(r["cycles"]), int(r["n"])] for r in cyc]
        try:
            inc = conn.execute(
                "SELECT issue_key, state, first_seen_at, last_seen_at, resolved_at, occurrence_count, detail FROM rival_runtime_health_incidents WHERE last_seen_at>=? ORDER BY last_seen_at",
                (since,)).fetchall()
            out["incidents"] = [{k: str(r[k])[:120] for k in r.keys()} for r in inc][:20]
        except Exception as exc:  # noqa: BLE001
            out["incidents"] = f"unreadable: {type(exc).__name__}"
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
