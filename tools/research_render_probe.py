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
        rows = conn.execute("""SELECT v.horizon, f.issued_at, f.probability_up p, f.market_baseline m, o.direction y
          FROM rival_polymarket_model_forward f JOIN rival_polymarket_observations v ON v.slug=f.slug
          JOIN rival_polymarket_ai_outcomes o ON o.slug=f.slug AND o.condition_id=v.condition_id
          WHERE f.model_version='polymarket-terminal-book-logistic-v1' AND f.issued_at>='2026-10-30'
            AND o.direction IN ('UP','DOWN')""").fetchall()
        st = conn.execute("SELECT asset,horizon,updates,trained_at FROM rival_polymarket_model_state WHERE model_version='polymarket-terminal-book-logistic-v1' ORDER BY asset,horizon").fetchall()
        out["state"] = [[r["asset"], int(r["horizon"]), int(r["updates"]), str(r["trained_at"])[:16]] for r in st]
    store.close()
    agg = {}
    for r in rows:
        day = str(r["issued_at"])[:10]
        # ET-ish bucket by UTC day; also split today's last 6 hours
        k = f'{r["horizon"]}|{day}'
        a = agg.setdefault(k, [0, 0., 0., 0, 0, 0, 0])
        p, m, y = float(r["p"]), float(r["m"]), int(r["y"] == "UP")
        a[0] += 1; a[1] += (p-y)**2; a[2] += (m-y)**2
        if abs(p-.5) >= .05: a[3] += 1; a[4] += int((p > .5) == (y == 1))
        if abs(m-.5) >= .05: a[5] += 1; a[6] += int((m > .5) == (y == 1))
    out["days"] = {k: [a[0], round(a[1]/a[0], 4), round(a[2]/a[0], 4), a[3], round(a[4]/a[3], 3) if a[3] else None, round(a[6]/a[5], 3) if a[5] else None] for k, a in sorted(agg.items())}
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
