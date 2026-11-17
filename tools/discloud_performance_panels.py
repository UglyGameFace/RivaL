"""Read-only acceptance of the deployed Discord renderer; never sends a message."""
from __future__ import annotations

import base64
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
from urllib import parse, request

MARKER = "RIVAL_PERFORMANCE_PANELS="
FILES = tuple("src/parlay_bot/discord_app/" + name + ".py"
              for name in ("home", "performance_panels", "xmarket_status"))


def host_report():
    import asyncio
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace

    from parlay_bot.config import Settings
    from parlay_bot.discord_app.home import GuidedHome, _cached_page_read, _xmarket_record
    from parlay_bot.discord_app.xmarket_status import read
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
    from parlay_bot.storage.hot import PostgresHotStore
    from parlay_bot.storage.query_scope import statement_timeout

    settings = Settings()
    store = PostgresHotStore(settings.rival_database_url.get_secret_value(), read_only=True)
    market = PerpetualMarketStore(store)
    # Existing production schemas only; readers must not attempt startup DDL.
    market._forward_schema_ready = True
    learner = PolymarketOutcomeLearner(store)
    learner._initialized = True
    output = {"read_only": True, "sends_discord_messages": False, "proves_edge": False}

    async def render():
        dummy = SimpleNamespace()
        panels = {}
        btc_lifecycle = None
        for section, horizon in (("overview", 5), ("models", 15)):
            home = GuidedHome(owner_id=1, location=dummy, builder=dummy, collector=None,
                market=market, targets=dummy, paper=dummy, quotes=dummy, profit=None,
                polymarket_status=lambda a, h: learner.summary(asset=a, horizon=h))
            home.navigate("performance")
            home.performance_section, home.horizon = section, horizon
            try:
                embeds = await home.render_embeds()
                lifecycle = _cached_page_read(_xmarket_record, market, "BTC", 0,
                                               max_age_seconds=60)
                btc_lifecycle = lifecycle
                text = "\n".join(f.value for e in embeds for f in e.fields
                                 if f.name.startswith("XMarket"))
                scopes = lifecycle.get("scopes", [])
                panels[section] = {
                    "embeds": len(embeds), "characters": sum(map(len, embeds)),
                    "all_timeframes_visible": len(scopes) == 3 and all(
                        f"**{s['horizon_minutes']}m**" in text for s in scopes),
                    "ledger_counts_match": lifecycle.get("status") == "recorded" and all(
                        f"{s['issued_24h']:,} issued / {s['settled_24h']:,} settled" in text
                        for s in scopes if s["issued_24h"]),
                }
            finally:
                home.stop()
        return panels, btc_lifecycle

    try:
        files = tuple("src/parlay_bot/discord_app/" + name + ".py"
                      for name in ("home", "performance_panels", "xmarket_status"))
        output["source_sha256"] = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                    for p in files}
        with statement_timeout(8_000):
            # Render the real Discord views first. Their own 60-second
            # lifecycle cache already contains a fresh BTC read; re-querying
            # BTC before rendering needlessly doubles the acceptance workload.
            output["panels"], btc = asyncio.run(render())
            if not isinstance(btc, dict) or btc.get("status") != "recorded":
                raise ValueError("BTC renderer lifecycle unavailable")
            output["scopes"] = [
                {"asset": "BTC", **scope} for scope in btc.get("scopes", [])
            ]
            # The three other assets have independent read-only queries;
            # concurrent source observations avoid multiplying the worst-case
            # remote-console wall time. asyncio.to_thread propagates the
            # existing statement_timeout context without relaxing its bound.
            async def other_assets():
                return await asyncio.gather(*(
                    asyncio.to_thread(read, store, asset=asset)
                    for asset in ("ETH", "SOL", "XRP")
                ))

            for asset, result in zip(
                    ("ETH", "SOL", "XRP"), asyncio.run(other_assets())):
                if not isinstance(result, dict) or result.get("status") != "recorded":
                    raise ValueError("XMarket lifecycle unavailable")
                output["scopes"].extend(
                    {"asset": asset, **scope} for scope in result.get("scopes", [])
                )
        return output
    finally:
        store.close()


def checked_report(stdout: str, expected: dict) -> dict:
    """Accept exact source and real rendered views; print only allowlisted aggregates."""
    lines = [line[len(MARKER):] for line in stdout.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise ValueError("missing or ambiguous panel receipt")
    report = json.loads(lines[0])
    if (report.get("source_sha256") != expected or report.get("read_only") is not True
            or report.get("sends_discord_messages") is not False
            or report.get("proves_edge") is not False):
        raise ValueError("unverified renderer source or probe contract")
    panels = report["panels"]
    for key in ("overview", "models"):
        part = panels[key]
        if (part["embeds"] != 1 or not 0 < part["characters"] <= 5900
                or part["all_timeframes_visible"] is not True
                or part["ledger_counts_match"] is not True):
            raise ValueError("rendered XMarket evidence mismatch")
    scopes = []
    for scope in report["scopes"]:
        asset, horizon = scope["asset"], scope["horizon_minutes"]
        if asset not in ("BTC", "ETH", "SOL", "XRP") or horizon not in (5, 15, 60):
            raise ValueError("unknown scope")
        counts = {k: scope[k] for k in ("usable_imports", "issued_24h", "settled_24h")}
        if any(type(v) is not int or v < 0 for v in counts.values()):
            raise ValueError("invalid lifecycle counts")
        scopes.append({"asset": asset, "horizon_minutes": horizon, **counts})
    if len({(s["asset"], s["horizon_minutes"]) for s in scopes}) != 12:
        raise ValueError("incomplete lifecycle read")
    return {"read_only": True, "proves_edge": False, "scopes": scopes,
            "panels": {k: {n: panels[k][n] for n in (
                "embeds", "characters", "all_timeframes_visible", "ledger_counts_match")}
                for k in ("overview", "models")}}


def main() -> int:
    token, app_id = os.environ.get("DISCLOUD_TOKEN"), os.environ.get("DISCLOUD_APP_ID")
    if not token or not app_id:
        print("Performance renderer acceptance credentials unavailable")
        return 1
    root = Path(__file__).resolve().parents[1]
    expected = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in FILES}
    program = "import json\n" + inspect.getsource(host_report) + (
        f"\nprint({MARKER!r}+json.dumps(host_report()))\n")
    encoded = base64.b64encode(gzip.compress(program.encode())).decode()
    command = ("python -c \"import base64,gzip,sys;sys.path.insert(0,'src');"
               f"exec(gzip.decompress(base64.b64decode('{encoded}')))\"")
    try:
        req = request.Request("https://api.discloud.app/v2/app/"
            + parse.quote(app_id, safe="") + "/console",
            data=json.dumps({"command": command}).encode(),
            headers={"api-token": token, "Content-Type": "application/json"}, method="PUT")
        with request.urlopen(req, timeout=110) as response:
            shell = json.load(response).get("apps", {}).get("shell", {})
        result = checked_report(shell.get("stdout", ""), expected)
    except Exception as exc:  # noqa: BLE001 - never echo API errors, SQL, or credentials
        print("Performance renderer acceptance unavailable:", type(exc).__name__)
        return 1
    print("Deployed performance panel receipt:", json.dumps(result, sort_keys=True))
    print("Rendered deployed files in a read-only probe; no Discord message was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
