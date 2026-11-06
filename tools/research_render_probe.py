import base64, gzip, json
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        tables = [r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE '%live%polymarket%' OR table_name LIKE '%polymarket%board%' OR table_name LIKE '%tracker%'").fetchall()]
        out["tables"] = tables
        for t in tables:
            cols = [r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?", (t,)).fetchall()]
            rows = conn.execute(f"SELECT * FROM {t} ORDER BY 1 DESC LIMIT 5").fetchall()
            out[t] = {"cols": cols, "rows": [{k: str(r[k])[:40] for k in cols if 'token' not in k} for r in rows]}
        probs = conn.execute("""SELECT template, SUM(count) AS n, MAX(last_at) AS last, MAX(text) AS text FROM rival_problem_log
            WHERE hour>='2026-11-06T00' AND (template LIKE '%Polymarket%DM%' OR template LIKE '%tracker%'
              OR template LIKE '%board update failed%' OR template LIKE '%Live Polymarket%')
            GROUP BY template""").fetchall()
        out["problems"] = [[str(r["template"])[:90], int(r["n"]), str(r["last"])[:19], str(r["text"])[:160]] for r in probs]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
