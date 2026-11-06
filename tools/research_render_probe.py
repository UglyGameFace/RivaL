import base64, gzip, json, os
MARKER = "RIVAL_RENDER_PROBE="
if os.environ.get("RIVAL_PROBE_ASSETS", "BTC") != "BTC":
    print(MARKER + base64.b64encode(gzip.compress(b"{}")).decode()); raise SystemExit(0)
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    s = Settings()
    out["owner_ids_configured"] = bool(str(getattr(s, "rival_owner_user_ids", "") or "").strip())
    store = PostgresHotStore(s.rival_database_url.get_secret_value())
    with store.connect() as conn:
        rows = conn.execute("""SELECT hour, logger, level, template, count, last_at, text FROM rival_problem_log
            WHERE hour>='2026-11-06T07' AND (logger ILIKE '%live_board%' OR logger ILIKE '%loop_watch%' OR template ILIKE '%interaction%') ORDER BY last_at DESC LIMIT 40""").fetchall()
        out["rows"] = [[str(r["last_at"])[11:19], r["level"], r["logger"][-28:], int(r["count"]), str(r["text"])[:230]] for r in rows]
        tr = conn.execute("SELECT user_id, asset, horizon, message_id, updated_at FROM rival_polymarket_live_board WHERE enabled=1").fetchall()
        out["trackers"] = [[r["asset"], int(r["horizon"]), str(r["message_id"])[-6:], str(r["updated_at"])[11:19]] for r in tr]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
