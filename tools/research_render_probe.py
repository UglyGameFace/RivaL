import base64, gzip, json, os
MARKER = "RIVAL_RENDER_PROBE="
a = os.environ.get("RIVAL_PROBE_ASSETS", "BTC")
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    from parlay_bot.perpetuals.intelligence import IntelligenceLedger
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    led = IntelligenceLedger(store)
    for h in (5, 15, 60):
        out[f"{a}/{h}"] = led.report(asset=a, horizon=h)
    store.close()
except Exception as exc:  # noqa: BLE001
    out[a] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
