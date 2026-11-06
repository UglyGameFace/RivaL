import base64, gzip, json, os, re, time, subprocess
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    st = open("/proc/1/status").read()
    out["rss_mb"] = int(re.search(r"VmRSS:\s+(\d+)", st).group(1)) // 1024
    boot = float(open("/proc/1/stat").read().split()[21]) / os.sysconf("SC_CLK_TCK")
    up = float(open("/proc/uptime").read().split()[0])
    out["bot_running_minutes"] = round((up - boot) / 60, 1)
    src = open("src/parlay_bot/research/source_native.py").read()
    out["gate_fix_present"] = "gatebulk" in src
    out["grading_fix_present"] = "f.issued_at<=c.issued_at" in open("src/parlay_bot/perpetuals/intelligence.py").read()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out).encode())).decode())
