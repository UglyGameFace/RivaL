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
        rows = conn.execute("""SELECT v.asset, v.horizon, f.model_version mv, f.probability_up p, f.market_baseline m, o.direction y
          FROM rival_polymarket_model_forward f JOIN rival_polymarket_observations v ON v.slug=f.slug
          JOIN rival_polymarket_ai_outcomes o ON o.slug=f.slug AND o.condition_id=v.condition_id
          WHERE f.issued_at>='2026-11-01' AND o.direction IN ('UP','DOWN')""").fetchall()
        fams = conn.execute("""SELECT family, COUNT(*) n FROM rival_model_shadow_forecasts WHERE issued_at>='2026-11-04' GROUP BY family ORDER BY n DESC LIMIT 30""").fetchall()
        out["perp_families"] = [[r["family"], int(r["n"])] for r in fams]
    store.close()
    agg = {}
    for r in rows:
        mv = str(r["mv"]); mv = "book" if mv.startswith("polymarket-terminal") else mv[:40]
        k = f'{r["asset"]}|{r["horizon"]}|{mv}'
        a = agg.setdefault(k, [0, 0., 0.]); p, m, y = float(r["p"]), float(r["m"]), int(r["y"] == "UP")
        a[0] += 1; a[1] += (p-y)**2; a[2] += (m-y)**2
    out["pm"] = {k: [a[0], round(a[1]/a[0], 4), round(a[2]/a[0], 4), round((a[2]-a[1])/a[0], 4)] for k, a in sorted(agg.items())}
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
