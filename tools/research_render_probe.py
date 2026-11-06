import base64, gzip, json
MARKER = "RIVAL_RENDER_PROBE="
out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        rows = conn.execute("""
          SELECT v.slug, v.observed_at, v.expires_at, f.issued_at AS f_issued, f.probability_up AS p,
                 v.condition_id IS NOT NULL AS has_cond
          FROM rival_polymarket_observations v
          LEFT JOIN rival_polymarket_model_forward f
            ON f.slug=v.slug AND f.model_version='polymarket-terminal-book-logistic-v1'
          WHERE v.asset='BTC' AND v.horizon=5 AND v.expires_at>='2026-11-06T03:30'
          ORDER BY v.expires_at""").fetchall()
        out["btc5"] = [[str(r["slug"])[-10:], str(r["observed_at"])[11:19], str(r["expires_at"])[11:19],
                        str(r["f_issued"])[11:19] if r["f_issued"] else None,
                        round(float(r["p"]), 3) if r["p"] is not None else None] for r in rows]
        allh = conn.execute("""
          SELECT v.asset, v.horizon, COUNT(*) AS obs, COUNT(f.slug) AS fwd,
                 SUM(CASE WHEN f.probability_up IS NOT NULL AND ABS(f.probability_up-0.5)<0.05 THEN 1 ELSE 0 END) AS near
          FROM rival_polymarket_observations v
          LEFT JOIN rival_polymarket_model_forward f ON f.slug=v.slug AND f.model_version='polymarket-terminal-book-logistic-v1'
          WHERE v.expires_at>='2026-11-05T00:00' GROUP BY v.asset, v.horizon ORDER BY 1,2""").fetchall()
        out["since_nov5"] = [[r["asset"], int(r["horizon"]), int(r["obs"]), int(r["fwd"]), int(r["near"] or 0)] for r in allh]
        probs = conn.execute("""SELECT template, SUM(count) AS n, MAX(last_at) AS last FROM rival_problem_log
            WHERE hour>='2026-11-06T03' AND (template ILIKE '%polymarket%') GROUP BY template ORDER BY n DESC LIMIT 12""").fetchall()
        out["problems"] = [[str(r["template"])[:110], int(r["n"]), str(r["last"])[11:19]] for r in probs]
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
