"""Read-only: do the stored six-year models pass the stricter tree check? + RSS."""
import base64, gzip, json, os, re
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    st = open("/proc/1/status").read()
    out["rss_mb"] = int(re.search(r"VmRSS:\s+(\d+)", st).group(1)) // 1024
    out["peak_mb"] = int(re.search(r"VmHWM:\s+(\d+)", st).group(1)) // 1024
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        rows = conn.execute("SELECT family, horizon_minutes, model_sha256, document FROM rival_pooled_models").fetchall()
        mem = conn.execute("""SELECT last_at, text FROM rival_problem_log WHERE template LIKE '%memory receipt%'
                              ORDER BY last_at DESC LIMIT 3""").fetchall()
        out["receipts"] = [[str(r["last_at"])[:19], str(r["text"])[:150]] for r in mem]
    store.close()
    models = []
    for r in rows:
        doc = json.loads(r["document"])
        bad = 0
        for tree in doc["trees"]:
            for pos, node in enumerate(tree):
                if len(node) != 1:
                    left, right = node[4], node[5]
                    if not (type(left) is int and type(right) is int and pos < left < len(tree) and pos < right < len(tree)):
                        bad += 1
        models.append({"family": r["family"], "h": r["horizon_minutes"], "sha": str(r["model_sha256"])[:12],
                       "trees": len(doc["trees"]), "bad_splits": bad})
    out["models"] = models
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
