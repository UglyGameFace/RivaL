"""Read-only: render every Research page on the live host and print its text.

Used by performance-truth-audit.yml (mode=render) to check each displayed
number against the database. Prints gzip+base64 JSON on one marker line.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os

MARKER = "RIVAL_RENDER_PROBE="
SKIP = {("math", "ai"), ("accuracy", "tracker")}


def _dump(embed) -> dict:
    return {"title": embed.title, "description": embed.description,
            "fields": [[f.name, f.value] for f in embed.fields],
            "footer": embed.footer.text if embed.footer else None}


async def _render_home(out, store, market, venues, assets) -> None:
    """The Bot Performance page exactly as GuidedHome renders it."""
    from types import SimpleNamespace

    from parlay_bot.discord_app.home import GuidedHome
    from parlay_bot.perpetuals.polymarket_research import PolymarketResearch

    poly = PolymarketResearch(store, ai=None)
    poly.initialize()
    dummy = SimpleNamespace()
    for asset in assets:
        for horizon in (5, 15, 60):
            home = GuidedHome(
                owner_id=1, location=dummy, builder=dummy, collector=None,
                market=market, targets=dummy, paper=dummy, quotes=dummy, profit=None,
                venues=venues,
                polymarket_status=lambda a, h: poly.learner.summary(asset=a, horizon=h),
            )
            home.coin, home.horizon = asset, horizon
            home.navigate("performance")
            key = f"performance/{asset}/{horizon}"
            try:
                out[key] = _dump(await asyncio.wait_for(home.render(), timeout=60))
                # Second render: the improvement check is cached after a slow first run.
                out[key + "/second"] = _dump(await asyncio.wait_for(home.render(), timeout=60))
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            finally:
                home.stop()


async def _render_pages(out, store, market, venues, assets) -> None:
    """Every other Home page, as GuidedHome renders it (dummy services: a
    page that needs a live service reports the error instead)."""
    from types import SimpleNamespace

    from parlay_bot.discord_app.home import GuidedHome

    dummy = SimpleNamespace()
    for page in ("home", "crypto-desk", "hunt", "practice", "results", "advanced", "help",
                 "sports", "live", "crypto", "forecast"):
        home = GuidedHome(owner_id=1, location=dummy, builder=dummy, collector=None,
                          market=market, targets=dummy, paper=dummy, quotes=dummy,
                          profit=None, venues=venues)
        home.coin, home.horizon = assets[0], 5
        try:
            home.navigate(page)
            embed = await asyncio.wait_for(home.render(), timeout=60)
            out[f"page/{page}"] = _dump(embed)
            out[f"page/{page}/buttons"] = [getattr(c, "label", None) for c in home.children]
        except Exception as exc:  # noqa: BLE001
            out[f"page/{page}"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        finally:
            home.stop()


def _render_models(out, store, assets) -> None:
    """Every model page and the compare card for the probed coin."""
    from parlay_bot.discord_app.model_detail import model_embed, model_report, trial_models
    from parlay_bot.discord_app.model_watch import compare_embed, reports_for

    for asset in assets:
        try:
            names = trial_models(store, asset)
            out[f"models/{asset}"] = names
            for name in names:
                try:
                    out[f"model/{asset}/{name}"] = _dump(model_embed(
                        model_report(store, asset, name), asset=asset, name=name))
                except Exception as exc:  # noqa: BLE001
                    out[f"model/{asset}/{name}"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            out[f"compare/{asset}"] = _dump(compare_embed(
                reports_for(store, asset, names[:5]), asset=asset))
        except Exception as exc:  # noqa: BLE001
            out[f"models/{asset}"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def _render_all() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.discord_app.opportunities import crypto_desk_embed
    from parlay_bot.discord_app.research_dashboard import _SECTIONS, ResearchDashboard
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    out: dict = {}
    try:
        market = PerpetualMarketStore(store)
        # The Model Lab line on Bot Performance, without running its DDL.
        from parlay_bot.perpetuals.model_lab import ModelLab

        market.model_lab = ModelLab(store)
        market.model_lab._ready = True
        market.model_lab.learners._ready = True
        with store.connect() as conn:
            venues = tuple(sorted({str(r["venue"]) for r in conn.execute(
                "SELECT DISTINCT venue FROM perp_forward_audit WHERE issued_at>=?",
                ("2026-11-01",)).fetchall()}))
        out["venues"] = venues
        assets = tuple(a for a in os.environ.get("RIVAL_PROBE_ASSETS", "BTC").split(",") if a)
        if os.environ.get("RIVAL_PROBE_PAGES"):
            # A separate console call: every other page, model page and
            # compare card (the research loop below is skipped).
            await _render_pages(out, store, market, venues, assets)
            _render_models(out, store, assets)
            return out
        await _render_home(out, store, market, venues, assets)
        for asset in assets:
            for proof in (False, True):
                try:
                    embed = crypto_desk_embed(market, asset=asset, proof=proof)
                    out[f"desk/{'proof' if proof else 'watch'}/{asset}"] = _dump(embed)
                except Exception as exc:  # noqa: BLE001
                    out[f"desk/{proof}/{asset}"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        for screen in ("accuracy", "math"):
            for section, _label in _SECTIONS[screen]:
                if (screen, section) in SKIP:
                    continue
                for asset in assets:
                    view = ResearchDashboard(owner_id=1, market=market, ai=None,
                                             venues=venues, shadow_enabled=True)
                    view.asset, view.screen, view.section = asset, screen, section
                    key = f"{screen}/{section}/{asset}"
                    try:
                        embed = await asyncio.wait_for(view.render(), timeout=60)
                        out[key] = _dump(embed)
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
