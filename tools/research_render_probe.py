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
    since = "2026-11-06T00"
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT hour, template, text, count, last_at FROM rival_problem_log WHERE hour>=?
               AND (template LIKE '%memory%' OR template LIKE '%event loop blocked%')
               ORDER BY last_at""", (since,)).fetchall()
        out["texts"] = [[str(r["last_at"])[:19], int(r["count"]), str(r["text"])[:400]] for r in rows]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
