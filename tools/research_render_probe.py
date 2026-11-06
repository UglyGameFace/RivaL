import base64, gzip, json
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        rows = conn.execute("""SELECT horizon, resolution_source, COUNT(*) AS n, MIN(observed_at) AS first,
                               MAX(observed_at) AS last FROM rival_polymarket_observations
                               GROUP BY horizon, resolution_source ORDER BY horizon, n DESC""").fetchall()
        out["sources"] = [[int(r["horizon"]), str(r["resolution_source"])[:120], int(r["n"]),
                           str(r["first"])[:16], str(r["last"])[:16]] for r in rows]
        sample = conn.execute("""SELECT horizon, evidence_json FROM rival_polymarket_observations
                                 WHERE horizon IN (5,15) ORDER BY observed_at DESC LIMIT 2""").fetchall()
        out["rules_sample"] = [[int(r["horizon"]), str(json.loads(r["evidence_json"]).get("rules", ""))[:600]] for r in sample]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
