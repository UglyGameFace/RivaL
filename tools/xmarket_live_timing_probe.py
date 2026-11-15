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
settings = Settings()
assert settings.rival_storage_backend == "postgres"
clock = datetime.now(UTC)
store = PostgresHotStore(settings.rival_database_url.get_secret_value())
out = []
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
finally:
    store.close()
print("RIVAL_XMARKET_TIMING="+json.dumps({"observed_at":clock.isoformat(),"read_only":True,"targets":out},sort_keys=True))
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
