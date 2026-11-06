"""Read-only: time every Research section render, as users open them."""
import asyncio, base64, gzip, json, os, time
MARKER = "RIVAL_RENDER_PROBE="
from parlay_bot.discord_app.research_dashboard import _SECTIONS
ALL = [(s, k) for s in ("opportunities", "math", "accuracy") for k, _ in _SECTIONS[s] if (s, k) != ("math", "ai")]
part = {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3}[os.environ.get("RIVAL_PROBE_ASSETS", "BTC")]
PART = ALL[part::4]
async def main():
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    out = {}
    try:
        market = PerpetualMarketStore(store)
        venues = ('bitget', 'deribit', 'dydx', 'gate', 'hyperliquid', 'kucoin', 'lighter', 'okx', 'phemex')
        for screen, section in PART:
            view = ResearchDashboard(owner_id=1, market=market, ai=None, venues=venues, shadow_enabled=True)
            view.asset, view.screen, view.section = "BTC", screen, section
            view._rebuild()
            t = time.monotonic()
            try:
                e = await asyncio.wait_for(view.render(), timeout=20)
                out[f"{screen}/{section}"] = [round(time.monotonic()-t, 1), len(e.fields)]
            except Exception as exc:  # noqa: BLE001
                out[f"{screen}/{section}"] = [round(time.monotonic()-t, 1), type(exc).__name__]
            finally:
                view.stop()
        with store.connect() as conn:
            r = conn.execute("SELECT template, SUM(count) AS n, MAX(last_at) AS l FROM rival_problem_log WHERE hour>='2026-11-05T00' AND template ILIKE '%render timed out%' GROUP BY template").fetchall()
            out[f"timeouts{part}"] = [[x["template"][:60], int(x["n"]), str(x["l"])[5:16]] for x in r]
    finally:
        store.close()
    return out
try:
    report = asyncio.run(main())
except Exception as exc:  # noqa: BLE001
    report = {f"error{part}": f"{type(exc).__name__}: {str(exc)[:200]}"}
print(MARKER + base64.b64encode(gzip.compress(json.dumps(report, default=str).encode())).decode())
