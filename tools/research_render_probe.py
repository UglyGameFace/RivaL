"""Read-only: render every Research page on the live host and print its text.

Used by performance-truth-audit.yml (mode=render) to check each displayed
number against the database. Prints gzip+base64 JSON on one marker line.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import json

MARKER = "RIVAL_RENDER_PROBE="
SKIP = {("math", "ai"), ("accuracy", "tracker")}


async def _render_all() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.research_dashboard import _SECTIONS, ResearchDashboard
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    out: dict = {}
    try:
        market = PerpetualMarketStore(store)
        with store.connect() as conn:
            venues = tuple(sorted({str(r["venue"]) for r in conn.execute(
                "SELECT DISTINCT venue FROM perp_forward_audit WHERE issued_at>=?",
                ("2026-11-01",)).fetchall()}))
        out["venues"] = venues
        assets = ("BTC", "ETH", "SOL", "XRP")
        for screen in ("accuracy", "math", "opportunities"):
            for section, _label in _SECTIONS[screen]:
                if (screen, section) in SKIP:
                    continue
                for asset in assets if (screen, section) in (
                        ("accuracy", "lineage"), ("accuracy", "overview")) else ("BTC",):
                    view = ResearchDashboard(owner_id=1, market=market, ai=None,
                                             venues=venues, shadow_enabled=True)
                    view.asset, view.screen, view.section = asset, screen, section
                    key = f"{screen}/{section}/{asset}"
                    try:
                        embed = await asyncio.wait_for(view._render_full(), timeout=60)
                        out[key] = {
                            "title": embed.title, "description": embed.description,
                            "fields": [[f.name, f.value] for f in embed.fields],
                            "footer": embed.footer.text if embed.footer else None,
                        }
                    except Exception as exc:  # noqa: BLE001
                        out[key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                    finally:
                        view.stop()
    finally:
        store.close()
    return out


def main() -> int:
    try:
        report = asyncio.run(_render_all())
    except Exception as exc:  # noqa: BLE001 - never print DSN or SQL
        report = {"error": type(exc).__name__}
    blob = base64.b64encode(gzip.compress(json.dumps(report, default=str).encode())).decode()
    print(MARKER + blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
