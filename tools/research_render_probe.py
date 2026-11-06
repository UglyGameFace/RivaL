"""Read-only: how good are the frozen first calls, vs the market price, after fees?"""
import base64, gzip, json, os
MARKER = "RIVAL_RENDER_PROBE="
if os.environ.get("RIVAL_PROBE_ASSETS", "BTC") != "BTC":
    print(MARKER + base64.b64encode(gzip.compress(b"{}")).decode()); raise SystemExit(0)
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    from parlay_bot.perpetuals.polymarket_learning import polymarket_fee
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        rows = conn.execute("""
          SELECT f.model_version AS mv, v.asset, v.horizon, f.probability_up AS p, f.market_baseline AS m,
                 o.direction AS y, v.features_json AS fj, f.issued_at, v.expires_at
          FROM rival_polymarket_model_forward f
          JOIN rival_polymarket_observations v ON v.slug=f.slug
          JOIN rival_polymarket_ai_outcomes o ON o.slug=f.slug AND o.condition_id=v.condition_id
          WHERE f.issued_at>='2026-11-04T03:15' AND o.direction IN ('UP','DOWN')""").fetchall()
    store.close()
    agg = {}
    for r in rows:
        mv = "book" if not str(r["mv"]).startswith("pooled") else "pooled"
        k = f'{mv}|{r["horizon"]}'
        a = agg.setdefault(k, {"n":0,"bm":0.,"bk":0.,"dir_n":0,"dir_ok":0,"bets":0,"wins":0,"stake":0.,"pnl":0.,"follow_n":0,"follow_ok":0})
        p, m, y = float(r["p"]), float(r["m"]), int(r["y"] == "UP")
        f = json.loads(r["fj"]); up, down = float(f[0]), float(f[1])
        a["n"] += 1; a["bm"] += (p-y)**2; a["bk"] += (m-y)**2
        if abs(p-.5) >= .05:
            a["dir_n"] += 1; a["dir_ok"] += int((p > .5) == (y == 1))
        if abs(m-.5) >= .05:
            a["follow_n"] += 1; a["follow_ok"] += int((m > .5) == (y == 1))
        for sp, ask, win in ((p, up, y == 1), (1-p, down, y == 0)):
            cost = ask + polymarket_fee(ask)
            if 0 < ask < 1 and sp - cost >= .02:
                a["bets"] += 1; a["wins"] += int(win); a["stake"] += cost; a["pnl"] += (1. if win else 0.) - cost
    for k, a in agg.items():
        n = a["n"]
        out[k] = {"n": n, "model_brier": round(a["bm"]/n, 4), "market_brier": round(a["bk"]/n, 4),
                  "directional": [a["dir_n"], round(a["dir_ok"]/a["dir_n"], 3) if a["dir_n"] else None],
                  "market_lean": [a["follow_n"], round(a["follow_ok"]/a["follow_n"], 3) if a["follow_n"] else None],
                  "edge_bets_2c": [a["bets"], a["wins"], round(a["pnl"]/a["stake"], 3) if a["stake"] else None]}
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
