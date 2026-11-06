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
        for t in ("perp_auto_ai_forecasts", "rival_intelligence_calls", "rival_llm_hypothesis_trials"):
            cols = [r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?", (t,)).fetchall()]
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            out[t] = {"cols": cols, "n": int(n)}
        out["auto_ai_sample"] = [dict(r) for r in conn.execute("SELECT * FROM perp_auto_ai_forecasts ORDER BY 1 DESC LIMIT 2").fetchall()]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
