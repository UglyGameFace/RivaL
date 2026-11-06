"""Read-only: render Opportunities, Math & History and Track Next exactly as users open them."""
import asyncio, base64, gzip, json, os, time
MARKER = "RIVAL_RENDER_PROBE="
PART = {"BTC": [("opportunities", "overview"), ("opportunities", "proof")],
        "ETH": [("math", "overview"), ("math", "pulse"), ("math", "spikes")],
        "SOL": [("math", "history"), ("math", "recovery"), ("math", "learning")],
        "XRP": [("accuracy", "tracker"), ("accuracy", "overview")]}[os.environ.get("RIVAL_PROBE_ASSETS", "BTC")]
def dump(e):
    return {"title": e.title, "description": e.description,
            "fields": [[f.name, f.value] for f in e.fields], "footer": e.footer.text if e.footer else None}
async def main():
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore
    store = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    out = {}
    try:
        market = PerpetualMarketStore(store)
        with store.connect() as conn:
            venues = tuple(sorted({str(r["venue"]) for r in conn.execute(
                "SELECT DISTINCT venue FROM perp_forward_audit WHERE issued_at>=?", ("2026-11-04",)).fetchall()}))
        out["venues"] = venues
        for screen, section in PART:
            view = ResearchDashboard(owner_id=629459300854661120, market=market, ai=None, venues=venues, shadow_enabled=True)
            view.asset, view.screen, view.section = "BTC", screen, section
            view._rebuild()
            t = time.monotonic()
            try:
                e = await asyncio.wait_for(view.render(), timeout=28)
                out[f"{screen}/{section}"] = dump(e) | {"secs": round(time.monotonic()-t, 1),
                    "controls": [getattr(c, "label", None) or getattr(c, "placeholder", None) for c in view.children]}
            except Exception as exc:  # noqa: BLE001
                out[f"{screen}/{section}"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}", "secs": round(time.monotonic()-t, 1)}
            finally:
                view.stop()
    finally:
        store.close()
    return out
try:
    report = asyncio.run(main())
except Exception as exc:  # noqa: BLE001
    report = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
print(MARKER + base64.b64encode(gzip.compress(json.dumps(report, default=str).encode())).decode())
