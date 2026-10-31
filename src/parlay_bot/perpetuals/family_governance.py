"""Source-specific, automatic, cost-gated family selection and rollback.

A passing offline backtest alone is never eligibility. Require genuine future
settlements, 21 distinct UTC days, time-matched source bid/ask snapshots and
positive day-block-stressed after-cost *paper* superiority, plus minimum quote
coverage. No live orders or claims of authenticated fills are introduced.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.model_competition import FAMILIES
from parlay_bot.perpetuals.profit_proof import COST_SCENARIOS

MIN_WINDOWS = 150
MIN_DAYS = 21
MIN_TRADES = 50
MIN_COVERAGE = .80
LOOKBACK = timedelta(days=30)
FRESH_FOR = timedelta(hours=48)


def _day_stress(pairs):
    buckets = {}
    for day, net, incumbent in pairs:
        buckets.setdefault(day, []).append((net, incumbent))
    differences = [
        sum(a-b for a,b in values)/len(values)
        for values in buckets.values()
    ]
    profits = [sum(a for a,_ in values)/len(values) for values in buckets.values()]
    if len(differences) < MIN_DAYS:
        return {"lower": None, "positive_days": 0, "days": len(differences),
                "net_lower": None}
    avg = statistics.mean(differences)
    lower = avg-2.58*statistics.stdev(differences)/math.sqrt(len(differences))
    net_lower = statistics.mean(profits)-2.58*statistics.stdev(profits)/math.sqrt(len(profits))
    return {"lower":lower, "net_lower":net_lower,
            "positive_days":sum(x>0 for x in differences),
            "days":len(differences)}


class FamilyPromotion:
    def __init__(self, store):
        self.store = store
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS rival_family_promotion (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                asset TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL,selected_family TEXT,
                status TEXT NOT NULL,reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                PRIMARY KEY(venue,contract,quote,horizon_minutes)
            );
            """)
        self._initialized = True

    def evaluate(self, *, asset, venue, contract, quote, horizon, now=None):
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        # The optional quote ledger may not have seen a venue book yet.
        # Research promotion still records a strict HELD decision.
        from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
        FamilyQuoteShadow(self.store).initialize()
        # Prove the exact same selected stream, not other venues of the asset.
        with self.store.connect() as conn:
            forecasts=conn.execute(
                """SELECT s.family,s.reference_opened_at,s.issued_at,
                          s.target_opened_at,s.settled_at,s.outcome_up,
                          s.probability_up,s.incumbent_up,s.baseline_up,
                          s.model_brier,s.incumbent_brier,s.baseline_brier,
                          q.state,q.training_sha256 AS quote_training,
                          s.training_sha256,q.entry_at,q.exit_at,
                          q.target_close_at,q.challenger_gross_bps,
                          q.incumbent_gross_bps,q.challenger_side,q.incumbent_side,
                          q.quantity
                   FROM rival_model_shadow_forecasts s
                   LEFT JOIN rival_family_quote_shadow q
                    ON q.venue=s.venue AND q.contract=s.contract AND q.quote=s.quote
                    AND q.horizon_minutes=s.horizon_minutes
                    AND q.family=s.family
                    AND q.reference_opened_at=s.reference_opened_at
                   WHERE s.asset=? AND s.venue=? AND s.contract=?
                    AND s.quote=? AND s.horizon_minutes=?
                    AND s.issued_at>=? AND s.issued_at<=?
                   ORDER BY s.family,s.target_opened_at LIMIT 24000""",
                (asset,venue,contract,quote,horizon,
                 (stamp-LOOKBACK).isoformat(),stamp.isoformat()),
            ).fetchall()
        scored={}
        for family in FAMILIES:
            # Models train from their own venue/horizon labels. Only uniquely
            # aligned target windows can contribute independent evidence.
            seen=set()
            scored_pairs=[]
            quote_pairs={label:[] for label,_ in COST_SCENARIOS}
            covered=eligible=trades=0
            invalid=0
            for row in forecasts:
                if row["family"]!=family:
                    continue
                # A still-open future target is not corrupt evidence.
                # Only genuinely settled observations enter the candidate
                # score, and missing quote exits remain a denominator loss.
                if row["settled_at"] is None or row["outcome_up"] is None:
                    continue
                try:
                    issued=utc(datetime.fromisoformat(str(row["issued_at"])))
                    target=utc(datetime.fromisoformat(str(row["target_opened_at"])))
                    settle=utc(datetime.fromisoformat(str(row["settled_at"])))
                    y=row["outcome_up"]
                    values=[float(row[k]) for k in (
                        "probability_up","incumbent_up","baseline_up",
                        "model_brier","incumbent_brier","baseline_brier",
                    )]
                    if (not issued<target+timedelta(minutes=5)<settle<=stamp
                            or y not in (0,1)
                            or int(target.timestamp())%(horizon*60)
                            or not all(math.isfinite(v) and 0<=v<=1 for v in values)
                            or row["reference_opened_at"] in seen):
                        invalid+=1
                        continue
                    # Brier must match the immutable forecast and actual outcome.
                    if any(abs(values[i+3]-(values[i]-y)**2)>1e-8 for i in range(3)):
                        invalid+=1
                        continue
                except (ValueError,TypeError,OverflowError):
                    invalid+=1
                    continue
                seen.add(row["reference_opened_at"])
                scored_pairs.append((target.isoformat(),values[3],values[4]))
                cs=("LONG" if values[0]>=.56 else "SHORT" if values[0]<=.44 else "CASH")
                ins=("LONG" if values[1]>=.56 else "SHORT" if values[1]<=.44 else "CASH")
                if cs=="CASH" and ins=="CASH":
                    continue
                eligible+=1
                if row["state"]!="EXIT_QUOTED":
                    continue
                try:
                    entered=utc(datetime.fromisoformat(str(row["entry_at"])))
                    exited=utc(datetime.fromisoformat(str(row["exit_at"])))
                    target_close=utc(datetime.fromisoformat(str(row["target_close_at"])))
                    candidate_gross=float(row["challenger_gross_bps"])
                    incumbent_gross=float(row["incumbent_gross_bps"])
                    if (str(row["training_sha256"])!=str(row["quote_training"])
                            or row["challenger_side"]!=cs or row["incumbent_side"]!=ins
                            or target_close!=target+timedelta(minutes=5)
                            or not issued<=entered<=issued+timedelta(seconds=90)
                            or not entered<exited
                            or not target_close<=exited<=target_close+timedelta(seconds=90)
                            or not all(math.isfinite(v) for v in (candidate_gross,incumbent_gross))
                            or not .00001<=float(row["quantity"])<=100000):
                        invalid+=1
                        continue
                except (ValueError,TypeError,OverflowError):
                    invalid+=1
                    continue
                covered+=1
                trades+=int(cs!="CASH")
                for label,cost in COST_SCENARIOS:
                    c_net=candidate_gross-(cost if cs!="CASH" else 0)
                    i_net=incumbent_gross-(cost if ins!="CASH" else 0)
                    quote_pairs[label].append((target.date().isoformat(),c_net,i_net))
            try:
                proof=score_paired_forward(scored_pairs)
            except (ValueError,TypeError,OverflowError):
                proof=None
            coverage=covered/eligible if eligible else 0.
            cost_proofs={}
            for label,rows in quote_pairs.items():
                stress=_day_stress(rows)
                c_mean=statistics.mean([a for _,a,_ in rows]) if rows else None
                i_mean=statistics.mean([b for _,_,b in rows]) if rows else None
                cost_proofs[label]={
                    "windows":len(rows),"days":stress["days"],
                    "challenger_net_bps":c_mean,"incumbent_net_bps":i_mean,
                    "paired_gain_bps":c_mean-i_mean if c_mean is not None else None,
                    "paired_lower_bps":stress["lower"],
                    "candidate_net_lower_bps":stress["net_lower"],
                    "positive_days":stress["positive_days"],
                    "qualified":bool(
                        len(rows)>=MIN_WINDOWS and stress["days"]>=MIN_DAYS
                        and stress["positive_days"]>=math.ceil(stress["days"]*.8)
                        and stress["lower"] is not None and stress["lower"]>0
                        and stress["net_lower"] is not None and stress["net_lower"]>0
                    ),
                }
            qualified=bool(
                proof is not None and proof.status=="qualified_shadow"
                and proof.days>=MIN_DAYS and proof.targets>=MIN_WINDOWS
                and eligible>=MIN_WINDOWS and covered>=MIN_WINDOWS
                and trades>=MIN_TRADES and coverage>=MIN_COVERAGE
                and invalid==0
                and all(s["qualified"] for s in cost_proofs.values())
            )
            scored[family]={
                "qualified":qualified,
                "targets":proof.targets if proof else 0,
                "days":proof.days if proof else 0,
                "forward_gain":proof.paired_gain if proof else None,
                "eligible_quotes":eligible,"closed_quotes":covered,
                "quote_coverage":coverage,"challenger_trades":trades,
                "invalid_rows":invalid,"cost_proof":cost_proofs,
                "real_fills_verified":False,
                "qualification":"prospective_shadow_paper_only",
            }
        winners=[f for f,v in scored.items() if v["qualified"]]
        winners.sort(key=lambda name:(
            -scored[name]["cost_proof"]["severe"]["paired_gain_bps"],name))
        selected=winners[0] if winners else None
        with self.store.connect() as conn:
            previous=conn.execute(
                """SELECT selected_family,status FROM rival_family_promotion
                   WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?""",
                (venue,contract,quote,horizon),
            ).fetchone()
            status=("promoted_paper" if selected else
                    "demoted" if previous and previous["status"]=="promoted_paper"
                    else "held")
            reason=("All same-source forward and severe after-cost PAPER gates passed"
                    if selected else "NO CALL: no family passed all future and cost gates")
            conn.execute(
                """INSERT INTO rival_family_promotion
                  (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                   selected_family,status,reason,evidence_json)
                  VALUES (?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(venue,contract,quote,horizon_minutes)
                  DO UPDATE SET evaluated_at=excluded.evaluated_at,
                   selected_family=excluded.selected_family,status=excluded.status,
                   reason=excluded.reason,evidence_json=excluded.evidence_json""",
                (venue,contract,quote,asset,horizon,stamp.isoformat(),selected,
                 status,reason,json.dumps(scored,sort_keys=True,allow_nan=False)),
            )
        return {"status":status,"selected_family":selected,"families":scored,
                "market":(venue,contract,quote,horizon),
                "real_fills_verified":False}

    def selected(self, *, asset,venue,contract,quote,horizon,now=None):
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            row=conn.execute(
                """SELECT selected_family,status,evaluated_at
                   FROM rival_family_promotion
                   WHERE asset=? AND venue=? AND contract=? AND quote=?
                    AND horizon_minutes=?""",
                (asset,venue,contract,quote,horizon),
            ).fetchone()
        if row is None or row["status"]!="promoted_paper":
            return None
        try:
            tested=utc(datetime.fromisoformat(row["evaluated_at"]))
        except (TypeError,ValueError):
            return None
        if not timedelta(0)<=stamp-tested<=FRESH_FOR:
            return None
        return row["selected_family"] if row["selected_family"] in FAMILIES else None
