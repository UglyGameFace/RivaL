"""One-off bounded read-only native source timing probe through the existing console."""
import json
import os
import sys
from urllib import parse, request

REMOTE = r'''
import json
import sys
from datetime import UTC, datetime, timedelta
sys.path.insert(0, "src")
from parlay_bot.config import Settings
from parlay_bot.storage.hot import PostgresHotStore
from parlay_bot.research.xmarket_features import sampled_bars, timestamp
from parlay_bot.research.xmarket_features import live_vector
from parlay_bot.perpetuals.features import persisted_candles
from parlay_bot.perpetuals.pooled_features import bar_close, contiguous
from parlay_bot.perpetuals.markets import asset_for_contract
settings = Settings()
assert settings.rival_storage_backend == "postgres"
clock = datetime.now(UTC)
store = PostgresHotStore(settings.rival_database_url.get_secret_value())
out = []
windows = []
try:
    for asset in ("BTC", "SOL", "XRP"):
        with store.connect() as conn:
            targets = conn.execute("SELECT reference_opened_at,issued_at FROM perp_forward_audit "
                "WHERE asset=? AND horizon_minutes=15 AND issued_at>=? "
                "ORDER BY issued_at DESC LIMIT 30", (asset, (clock-timedelta(hours=1)).isoformat())).fetchall()
        seen = set()
        for target in targets:
            at = timestamp(target["reference_opened_at"]) + timedelta(minutes=5)
            if at in seen or len(seen) >= 3:
                continue
            seen.add(at)
            issued = timestamp(target["issued_at"])
            with store.connect() as conn:
                rows = conn.execute("SELECT sample_json FROM rival_xmarket_observations "
                    "WHERE asset=? AND source='polymarket' AND at>=? AND at<=? "
                    "AND observed_at<=? ORDER BY at LIMIT 20",
                    (asset,(at-timedelta(minutes=2)).isoformat(),(at+timedelta(minutes=2)).isoformat(),
                     clock.isoformat())).fetchall()
            points = [json.loads(r["sample_json"]) for r in rows]
            earlier = sampled_bars(points,asset=asset,as_of=issued)["polymarket"]
            later = sampled_bars(points,asset=asset,as_of=clock)["polymarket"]
            out.append({"asset":asset,"required_bar":at.isoformat(),"incumbent_issued_at":issued.isoformat(),
                "pm_current_at_issuance":at in earlier,"pm_current_available_now":at in later,
                "nearby_source_points":[{"at":p["at"],"observed_at":p["observed_at"],
                    "fidelity_minutes":p["fidelity_minutes"]} for p in points]})
    with store.connect() as conn:
        fits = conn.execute("SELECT asset,horizon_minutes,document FROM rival_xmarket_models "
            "WHERE gates_passed=1 ORDER BY trained_through DESC LIMIT 12").fetchall()
        streams = conn.execute("SELECT DISTINCT venue,contract,quote FROM perp_market_candles "
            "WHERE interval_minutes=5 AND venue IN ('bitget','okx','hyperliquid')").fetchall()
    for fit in fits:
        asset, horizon = fit["asset"], int(fit["horizon_minutes"])
        doc = json.loads(fit["document"])
        with store.connect() as conn:
            target = conn.execute("SELECT venue,contract,quote,reference_opened_at,issued_at "
                "FROM perp_forward_audit WHERE asset=? AND horizon_minutes=? "
                "AND issued_at>=? ORDER BY issued_at DESC LIMIT 1",
                (asset,horizon,(clock-timedelta(hours=1)).isoformat())).fetchone()
        if target is None:
            continue
        issued = timestamp(target["issued_at"])
        ref = timestamp(target["reference_opened_at"])
        at = ref + timedelta(minutes=5)
        selected = {(r["venue"],r["contract"],r["quote"]) for r in streams
            if asset_for_contract(r["contract"]) == asset}
        selected.add((target["venue"],target["contract"],target["quote"]))
        by_venue, coverage = {}, []
        for venue, contract, quote in sorted(selected):
            with store.connect() as conn:
                rows = conn.execute("SELECT * FROM perp_market_candles "
                    "WHERE venue=? AND contract=? AND quote=? AND interval_minutes=5 "
                    "AND first_observed_at<=? AND opened_at<=? AND closed_at<? "
                    "ORDER BY opened_at DESC LIMIT 500",
                    (venue,contract,quote,issued.isoformat(),ref.isoformat(),issued.isoformat())).fetchall()
            candles = persisted_candles([dict(r) for r in reversed(rows)])
            by_venue[venue] = candles
            gaps = [{"before":bar_close(a).isoformat(),"after":bar_close(b).isoformat()}
                for a,b in zip(candles[-420:],candles[-420:][1:])
                if bar_close(b)-bar_close(a) != timedelta(minutes=5)]
            coverage.append({"venue":venue,"rows":len(candles),
                "last_bar":bar_close(candles[-1]).isoformat() if candles else None,
                "primary_420_contiguous":len(candles)>=420 and contiguous(candles[-420:]),
                "dispersion_40_contiguous":len(candles)>=40 and contiguous(candles[-40:]),
                "gap_count_420":len(gaps),"first_gaps":gaps[:3]})
        with store.connect() as conn:
            raw = conn.execute("SELECT sample_json FROM rival_xmarket_observations "
                "WHERE asset=? AND at>=? AND at<=? AND observed_at<=? ORDER BY at LIMIT 600",
                (asset,(at-timedelta(minutes=61)).isoformat(),at.isoformat(),issued.isoformat())).fetchall()
        samples = [json.loads(r["sample_json"]) for r in raw]
        vector = live_vector(by_venue.get(target["venue"],()),asset=asset,samples=samples,
            venue_candles=by_venue,venues=tuple(doc["dispersion_venues"]),now=issued,
            pm_fidelity=doc["pm_fidelity_minutes"])
        windows.append({"asset":asset,"horizon":horizon,"primary_venue":target["venue"],
            "issued_at":issued.isoformat(),"required_bar":at.isoformat(),
            "pm_fidelity_minutes":doc["pm_fidelity_minutes"],"vector_available":vector is not None,
            "candle_coverage":coverage})
finally:
    store.close()
print("RIVAL_XMARKET_TIMING="+json.dumps({"observed_at":clock.isoformat(),"read_only":True,"targets":out,
    "feature_windows":windows},sort_keys=True))
'''


def main():
    command = "python -c " + "'" + REMOTE.replace("'", "'\"'\"'") + "'"
    req = request.Request("https://api.discloud.app/v2/app/" +
        parse.quote(os.environ["DISCLOUD_APP_ID"], safe="") + "/console",
        data=json.dumps({"command": command}).encode(),
        headers={"api-token":os.environ["DISCLOUD_TOKEN"],"Content-Type":"application/json"}, method="PUT")
    try:
        with request.urlopen(req, timeout=110) as response:
            payload = json.load(response)
        stdout = str(payload.get("apps", {}).get("shell", {}).get("stdout", ""))
        for line in stdout.splitlines():
            if line.startswith("RIVAL_XMARKET_TIMING="):
                data = json.loads(line.split("=", 1)[1])
                assert data.get("read_only") is True
                print("RIVAL_XMARKET_TIMING=" + json.dumps(data, sort_keys=True))
                return 0
        print("XMarket timing unavailable: missing marker")
    except Exception as exc:
        print("XMarket timing unavailable:", type(exc).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
