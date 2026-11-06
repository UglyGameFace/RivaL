import base64, gzip, json, os
MARKER = "RIVAL_RENDER_PROBE="
if os.environ.get("RIVAL_PROBE_ASSETS", "BTC") != "BTC":
    print(MARKER + base64.b64encode(gzip.compress(b"{}")).decode()); raise SystemExit(0)
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        rows = conn.execute("""SELECT logger, level, template, SUM(count) AS n, MAX(last_at) AS last, MAX(text) AS text
            FROM rival_problem_log WHERE hour>='2026-11-05T00' AND (logger ILIKE '%research_dashboard%' OR logger ILIKE '%home%'
              OR logger ILIKE '%opportunit%' OR template ILIKE '%interaction%' OR template ILIKE '%panel%' OR template ILIKE '%dashboard%'
              OR logger ILIKE '%discord%')
            GROUP BY logger, level, template ORDER BY n DESC LIMIT 40""").fetchall()
        out["problems"] = [[r["logger"][-30:], r["level"], str(r["template"])[:120], int(r["n"]), str(r["last"])[5:16], str(r["text"])[:220]] for r in rows]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
