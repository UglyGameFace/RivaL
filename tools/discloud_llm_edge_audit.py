"""Read-only production LFM3 usefulness audit: no inference or trading."""
import argparse
import base64
import json
import os
from pathlib import Path
from urllib import request, parse, error

MARKER = "RIVAL_LLM_EDGE_AUDIT="
MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
REMOTE = """
import json, os, sys
sys.path.insert(0, os.path.abspath("src"))
from parlay_bot.config import Settings
from parlay_bot.storage.hot import PostgresHotStore
s = Settings()
r = {"schema":"llm-edge-v1", "model":"LiquidAI/LFM3-1.7B-GGUF:Q4_K_M",
     "provider":s.rival_ai_provider, "background_opt_in":s.rival_ai_background_opt_in,
     "rows":[], "cycle":None}
if s.rival_storage_backend != "postgres" or s.rival_database_url is None:
    r["error"] = "postgres_unavailable"
else:
    store = PostgresHotStore(s.rival_database_url.get_secret_value())
    try:
        with store.connect() as db:
            rows = db.execute(
                "SELECT asset,direction,COUNT(*) AS n,MAX(issued_at) AS latest "
                "FROM perp_ai_research WHERE model=? "
                "GROUP BY asset,direction ORDER BY asset,direction",
                (r["model"],)).fetchall()
            r["rows"] = [{"asset":x["asset"],"direction":x["direction"],
                          "count":int(x["n"]),"latest":str(x["latest"])} for x in rows]
            c = db.execute("SELECT completed_at,math_issued,math_settled "
                           "FROM perp_learning_cycle_health "
                           "ORDER BY completed_at DESC LIMIT 1").fetchone()
            if c:
                r["cycle"] = {"completed_at":str(c["completed_at"]),
                              "math_issued":int(c["math_issued"]),
                              "math_settled":int(c["math_settled"])}
    except Exception as exc:
        r["error"] = "database_read_failed"
        r["error_class"] = type(exc).__name__
    finally:
        store.close()
print("RIVAL_LLM_EDGE_AUDIT=" + json.dumps(r,sort_keys=True))
"""

def parse_receipt(stdout):
    records = [line[len(MARKER):] for line in stdout.splitlines()
               if line.startswith(MARKER)]
    if len(records) != 1:
        raise ValueError("missing or duplicate structured receipt")
    data = json.loads(records[0])
    if (not isinstance(data, dict) or data.get("schema") != "llm-edge-v1"
            or data.get("model") != MODEL):
        raise ValueError("untrusted model receipt")
    return data

def evaluate(data):
    if data.get("schema") != "llm-edge-v1" or data.get("model") != MODEL:
        raise ValueError("untrusted model")
    rows = data.get("rows")
    if not isinstance(rows,list) or len(rows) > 32:
        raise ValueError("invalid evidence rows")
    count = directional = 0
    for x in rows:
        if (not isinstance(x,dict) or x.get("asset") not in ("BTC","ETH","SOL","XRP")
                or x.get("direction") not in ("up","down","uncertain")
                or type(x.get("count")) is not int or not 0 < x["count"] < 1000000):
            raise ValueError("invalid evidence")
        count += x["count"]
        directional += x["count"] if x["direction"] in ("up","down") else 0
    reason = ("database_read_failed" if data.get("error") else
              "no_saved_llm_research" if not count else
              "all_outputs_abstained" if not directional else
              "research_directions_have_no_immutable_future_target")
    return {"schema":"llm-edge-v1",
            "status":"unverified" if data.get("error") else "not_testable",
            "reason":reason,"saved_research_count":count,
            "directional_research_count":directional,
            "background_ai_enabled":bool(data.get("background_opt_in"))
              and data.get("provider") not in ("local","cloudflare"),
            "last_mathematical_learning_cycle":data.get("cycle"),
            "paired_llm_forecast_count":None,"llm_forward_brier":None,
            "llm_after_cost_edge_verified":False,
            "halt_historical_backfill":False,
            "explanation":"Current LFM3 research records have no immutable forecast target, matching price/quote, or calibrated probability; they cannot prove a future trading edge."}

def audit():
    token,app = os.getenv("DISCLOUD_TOKEN",""),os.getenv("DISCLOUD_APP_ID","")
    if not token or not app:
        raise ValueError("production credentials unavailable")
    encoded = base64.b64encode(REMOTE.encode()).decode("ascii")
    command = "python -c 'exec(__import__(\"base64\").b64decode(\"" + encoded + "\"))'"
    req = request.Request(
        "https://api.discloud.app/v2/app/" + parse.quote(app,safe="") + "/console",
        method="PUT",data=json.dumps({"command":command}).encode(),
        headers={"api-token":token,"Content-Type":"application/json"})
    with request.urlopen(req,timeout=100) as response:
        raw = response.read(100001)
    if len(raw) > 100000:
        raise ValueError("oversized console response")
    body = json.loads(raw)
    stdout = str(body.get("apps",{}).get("shell",{}).get("stdout",""))
    return evaluate(parse_receipt(stdout))

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out",type=Path,default=Path("data/llm-edge-audit.json"))
    args = parser.parse_args(argv)
    try:
        result = audit()
    except (error.URLError,OSError,TimeoutError,TypeError,KeyError,ValueError):
        result = {"schema":"llm-edge-v1","status":"unverified",
                  "reason":"production_audit_unavailable",
                  "llm_after_cost_edge_verified":False,
                  "halt_historical_backfill":False}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print("LFM3 production readiness:",json.dumps(result,sort_keys=True))
    return 1 if result["status"] == "unverified" else 0

if __name__ == "__main__":
    raise SystemExit(main())
