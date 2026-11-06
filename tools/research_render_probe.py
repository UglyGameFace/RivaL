"""Read-only: tracked boards, logged Polymarket problems, and 6 min of board samples across a rollover."""
import asyncio, base64, gzip, json, time
from datetime import UTC, datetime
MARKER = "RIVAL_RENDER_PROBE="
out = {}
async def sample(store):
    from parlay_bot.discord_app.polymarket_watch import PolymarketReader
    reader = PolymarketReader(); rows = []
    keys = sorted({(a, h) for a, h in out.get("trackers_keys", [])} | {("BTC", 5)})
    end = time.monotonic() + 75
    try:
        while time.monotonic() < end:
            for a, h in keys:
                t = time.monotonic()
                try:
                    b = await asyncio.wait_for(reader.window_board(a, h), 12)
                    q = b.current_quote; slug = b.current.slug if b.current else None
                    with store.connect() as c:
                        f = c.execute("SELECT issued_at, probability_up FROM rival_polymarket_model_forward WHERE slug=? AND model_version='polymarket-terminal-book-logistic-v1'", (slug,)).fetchone() if slug else None
                    rows.append([datetime.now(UTC).strftime("%H:%M:%S"), a, h, round(time.monotonic()-t,1), slug[-10:] if slug else None,
                                 q is not None, b.quote_unavailable_reason, round(q.up_ask,2) if q else None,
                                 str(f["issued_at"])[11:19] if f else None, round(float(f["probability_up"]),3) if f else None])
                except Exception as exc:  # noqa: BLE001
                    rows.append([datetime.now(UTC).strftime("%H:%M:%S"), a, h, "ERR", type(exc).__name__])
            await asyncio.sleep(4)
    finally:
        await reader.aclose()
    return rows
import os
if os.environ.get("RIVAL_PROBE_ASSETS","BTC")!="BTC":
    print(MARKER + base64.b64encode(gzip.compress(b"{}")).decode()); raise SystemExit(0)
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    with store.connect() as conn:
        tr = conn.execute("SELECT asset, horizon, enabled, updated_at FROM rival_polymarket_live_board ORDER BY updated_at DESC").fetchall()
        out["trackers"] = [[r["asset"], int(r["horizon"]), int(r["enabled"]), str(r["updated_at"])[:19]] for r in tr]
        out["trackers_keys"] = [[r["asset"], int(r["horizon"])] for r in tr if int(r["enabled"]) == 1]
        cols = [r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='rival_problem_log'").fetchall()]
        out["problem_cols"] = cols
        probs = conn.execute("""SELECT * FROM rival_problem_log WHERE hour>='2026-11-05T12' AND
              (template ILIKE '%polymarket%' OR template ILIKE '%board%') ORDER BY hour DESC LIMIT 40""").fetchall()
        out["problems"] = [{k: str(r[k])[:160] for k in cols} for r in probs]
    out["samples"] = asyncio.run(sample(store))
    store.close()
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
