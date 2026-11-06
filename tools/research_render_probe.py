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
        rows = conn.execute("""SELECT v.asset, v.horizon, v.expires_at, v.observed_at, v.features_json, o.direction
            FROM rival_polymarket_observations v JOIN rival_polymarket_ai_outcomes o
              ON o.slug=v.slug AND o.condition_id=v.condition_id
            WHERE o.direction IN ('UP','DOWN') ORDER BY v.expires_at""").fetchall()
    store.close()
    from datetime import datetime, timedelta
    agg = {}
    for r in rows:
        k = f'{r["asset"]}|{r["horizon"]}'
        a = agg.setdefault(k, {"n": 0, "up": 0, "first": str(r["expires_at"])[:16], "seq": "", "cal": {}, "early": 0})
        y = r["direction"] == "UP"; a["n"] += 1; a["up"] += y; a["seq"] = (a["seq"] + ("U" if y else "D"))[-20:]
        f = json.loads(r["features_json"]); m = float(f[2])
        exp = datetime.fromisoformat(str(r["expires_at"])); obs = datetime.fromisoformat(str(r["observed_at"]))
        frac = (exp - obs).total_seconds() / (int(r["horizon"]) * 60)
        if frac >= 0.8: a["early"] += 1
        b = min(9, int(m * 10)); c = a["cal"].setdefault(b, [0, 0]); c[0] += 1; c[1] += y
        a["last"] = str(r["expires_at"])[:16]
    out["agg"] = agg
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
