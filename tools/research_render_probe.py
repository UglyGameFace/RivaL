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
        rows = conn.execute("""SELECT c.model, c.direction, c.evidence_json, c.issued_at, c.horizon, o.direction AS y
          FROM rival_polymarket_ai_calls c LEFT JOIN rival_polymarket_ai_outcomes o ON o.slug=c.slug AND o.condition_id=c.condition_id""").fetchall()
        tables = conn.execute("""SELECT table_name FROM information_schema.tables WHERE table_name ILIKE '%intelligence%' OR table_name ILIKE '%ai_research%' OR table_name ILIKE '%llm%' OR table_name ILIKE '%ai_%'""").fetchall()
        out["tables"] = sorted(r["table_name"] for r in tables)
    store.close()
    agg = {}
    from parlay_bot.perpetuals.polymarket_learning import polymarket_fee
    for r in rows:
        k = f'{r["model"]}'[:40]
        a = agg.setdefault(k, {"calls": 0, "settled": 0, "picks": 0, "right": 0, "stake": 0., "pnl": 0., "first": str(r["issued_at"])[:10], "last": ""})
        a["calls"] += 1; a["last"] = max(a["last"], str(r["issued_at"])[:10])
        if r["y"] not in ("UP", "DOWN"): continue
        a["settled"] += 1
        if r["direction"] not in ("UP", "DOWN"): continue
        a["picks"] += 1; win = r["direction"] == r["y"]; a["right"] += win
        ev = json.loads(r["evidence_json"]); ask = ev.get("up_ask" if r["direction"] == "UP" else "down_ask")
        if isinstance(ask, (int, float)) and 0 < ask < 1:
            c = ask + polymarket_fee(ask); a["stake"] += c; a["pnl"] += (1. if win else 0.) - c
    out["ai"] = agg
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
