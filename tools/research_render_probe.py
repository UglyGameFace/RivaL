import base64, gzip, json, time
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        t = time.monotonic()
        out["shadow_rows"] = int(conn.execute("SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts").fetchone()["n"])
        r = conn.execute("""SELECT s.family, COUNT(*) AS n FROM rival_model_shadow_forecasts s
            WHERE s.outcome_up IS NOT NULL AND EXISTS (
              SELECT 1 FROM perp_forward_audit f
              WHERE f.venue=s.venue AND f.contract=s.contract AND f.quote=s.quote
                AND f.horizon_minutes=s.horizon_minutes
                AND f.model_version='prospective-logistic-persistent-v3'
                AND f.reference_opened_at=s.reference_opened_at
                AND f.asset=s.asset AND f.issued_at=s.issued_at
                AND f.target_opened_at=s.target_opened_at
                AND f.settled_at IS NOT NULL AND f.outcome_up IS NULL)
            GROUP BY s.family ORDER BY n DESC""").fetchall()
        out["stale_labels_by_family"] = [[str(x["family"]), int(x["n"])] for x in r]
        out["query_seconds"] = round(time.monotonic() - t, 1)
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out).encode())).decode())
