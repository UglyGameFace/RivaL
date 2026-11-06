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
        rows = conn.execute("""SELECT asset, horizon_minutes h,
              CASE WHEN family LIKE 'boosted_%' THEN 'boosted' WHEN family LIKE 'historical_%' THEN 'historical'
                   WHEN family LIKE 'pooled_%' THEN 'pooled' WHEN family LIKE 'flow_%' THEN 'flow'
                   WHEN family LIKE 'calib_%' THEN 'calib' ELSE family END fam,
              COUNT(*) n, AVG(model_brier) m, AVG(incumbent_brier) i, AVG(baseline_brier) b,
              AVG(CASE WHEN (probability_up>0.5)=(outcome_up=1) THEN 1.0 ELSE 0.0 END) hit
            FROM rival_model_shadow_forecasts
            WHERE settled_at IS NOT NULL AND issued_at>='2026-10-30' AND model_brier IS NOT NULL
            GROUP BY 1,2,3 ORDER BY 1,2,5""").fetchall()
        out["rows"] = [[r["asset"], int(r["h"]), r["fam"], int(r["n"]), round(float(r["m"]), 4), round(float(r["i"]), 4), round(float(r["b"]), 4), round(float(r["hit"]), 3)] for r in rows]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
