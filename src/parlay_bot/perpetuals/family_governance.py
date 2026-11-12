"""Source-specific, automatic, cost-gated family selection and rollback.

A passing offline backtest alone is never eligibility. Require genuine future
settlements, 21 distinct UTC days, time-matched source bid/ask snapshots and
positive day-block-stressed after-cost *paper* superiority, plus minimum quote
coverage. No live orders or claims of authenticated fills are introduced.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.calibration_validation import unique_window_calibration
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.model_competition import CHALLENGER_PREFIXES, FAMILIES

_LOG = logging.getLogger(__name__)

MIN_WINDOWS = 150
MIN_DAYS = 21
MIN_TRADES = 50
MIN_COVERAGE = .80
LOOKBACK = timedelta(days=30)
FRESH_FOR = timedelta(hours=48)
# Kill switch 2 (spec §2.8, §3.5 #6): a family in this set can never serve,
# even with a fresh promoted_paper row. Default: empty. Changing this set
# requires Felix's explicit approval of the specific change.
BLOCKED_FAMILIES: set[str] = set()


def _pinned_identity_matches(recorded, current) -> bool:
    """D14 pin check (autonomous loop §11): the served family must be the
    pinned artifact that earned promotion -- exact weights-SHA match, same
    feature digest, trained-through and graded count (under that
    artifact's hash) monotone. A different fit never inherits the
    promotion; a rejected pin advances the queue (offline_model_pins)."""
    if not recorded or not current:
        return False
    try:
        recorded_sha = str(recorded["weights_sha256"])
        recorded_digest = str(recorded["feature_digest"])
        recorded_through = utc(datetime.fromisoformat(
            str(recorded["trained_through"])))
        recorded_count = int(recorded["graded_count"])
        current_through = utc(datetime.fromisoformat(
            str(current["trained_through"])))
        current_count = int(current["graded_count"])
    except (ValueError, TypeError, KeyError):
        return False
    return (len(recorded_sha) == 64
            and recorded_digest == current["feature_digest"]
            and (not recorded.get("model_sha256")
                 or current.get("model_sha256") == recorded["model_sha256"])
            and str(current["weights_sha256"]) == recorded_sha
            and current_through >= recorded_through
            and current_count >= recorded_count)


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


FAMILY_PROMOTION_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_family_promotion (
    venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
    asset TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
    evaluated_at TEXT NOT NULL,selected_family TEXT,
    status TEXT NOT NULL,reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(venue,contract,quote,horizon_minutes)
)"""


class FamilyPromotion:
    def __init__(self, store):
        self.store = store
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        with self.store.connect() as conn:
            # One statement, so plain execute (not executescript): the
            # status card also runs this DDL on its shared connection,
            # and name-keyed row wrappers expose execute only.
            conn.execute(FAMILY_PROMOTION_SCHEMA)
        self._initialized = True

    def evaluate(self, *, asset, venue, contract, quote, horizon, now=None):
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        # The optional quote ledger may not have seen a venue book yet.
        # Research promotion still records a strict HELD decision.
        from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow
        from parlay_bot.perpetuals.profit_proof import COST_SCENARIOS

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
                   ORDER BY s.family,s.target_opened_at,s.issued_at,s.reference_opened_at LIMIT 24000""",
                (asset,venue,contract,quote,horizon,
                 (stamp-LOOKBACK).isoformat(),stamp.isoformat()),
            ).fetchall()
        scored={}
        # D14 pinning: for the offline families, only rows issued by the
        # pinned artifact count as that family's evidence. A queued fit's
        # rows never inherit the pinned fit's record. No pin (no usable
        # artifact) means no attributable rows for the family.
        pinned_shas: dict[str, str | None] = {}
        try:
            from parlay_bot.perpetuals.archive_model import ArchiveModelSet
            _doc = ArchiveModelSet(self.store).active(asset, horizon, now=stamp)
            pinned_shas["archive_v1"] = (_doc or {}).get("_sha256")
        except Exception:  # noqa: BLE001 - no pin means no attributable rows
            pinned_shas["archive_v1"] = None
        try:
            from parlay_bot.perpetuals.gbm_model import GbmModelSet
            _doc = GbmModelSet(self.store).active(asset, horizon, now=stamp)
            pinned_shas["gbm_v1"] = (_doc or {}).get("_sha256")
        except Exception:  # noqa: BLE001 - no pin means no attributable rows
            pinned_shas["gbm_v1"] = None
        try:
            from parlay_bot.perpetuals.regime2_model import Regime2ModelSet
            _doc = Regime2ModelSet(self.store).active(asset, horizon, now=stamp)
            pinned_shas["regime_v2"] = (_doc or {}).get("_sha256")
        except Exception:  # noqa: BLE001 - no pin means no attributable rows
            pinned_shas["regime_v2"] = None
        try:
            from parlay_bot.perpetuals.volcal_model import VolcalModelSet
            _doc = VolcalModelSet(self.store).active(asset, horizon, now=stamp)
            pinned_shas["volcal_v1"] = (_doc or {}).get("_sha256")
        except Exception:  # noqa: BLE001 - no pin means no attributable rows
            pinned_shas["volcal_v1"] = None
        try:
            from parlay_bot.perpetuals.xmarket_model import XmarketModelSet
            _doc = XmarketModelSet(self.store).active(asset, horizon, now=stamp)
            pinned_shas["xmarket_v1"] = (_doc or {}).get("_sha256")
        except Exception:  # noqa: BLE001 - no pin means no attributable rows
            pinned_shas["xmarket_v1"] = None
        # Distinct immutable boosted artifacts compete only on their own
        # source-specific future records, never as one pooled model family.
        observed_families = sorted({
            str(row["family"]) for row in forecasts
            if str(row["family"]).startswith(CHALLENGER_PREFIXES)
            and len(str(row["family"])) <= 64
        })
        for family in list(FAMILIES) + observed_families:
            # Models train from their own venue/horizon labels. Only uniquely
            # aligned target windows can contribute independent evidence.
            seen=set()
            seen_targets=set()
            duplicate_target_rows=0
            scored_pairs=[]
            scored_base=[]
            scored_coin=[]
            calibration_rows=[]
            quote_pairs={label:[] for label,_ in COST_SCENARIOS}
            covered=eligible=trades=0
            invalid=0
            for row in forecasts:
                if row["family"]!=family:
                    continue
                if family in pinned_shas and str(row["training_sha256"]) != pinned_shas[family]:
                    # D14: a row from a non-pinned fit is not this
                    # family's evidence (it belongs to a queued fit, or
                    # to no pin at all).
                    continue
                # A single target outcome is one independent observation,
                # regardless of how many overlapping reference candles issued
                # forecasts for it. Select the EARLIEST issued row before
                # looking at settlement or quote success to avoid hindsight
                # cherry-picking a conveniently quoted later forecast.
                target_key=str(row["target_opened_at"])
                if target_key in seen_targets:
                    duplicate_target_rows+=1
                    continue
                seen_targets.add(target_key)
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
                    if int(target.timestamp())%(horizon*60):
                        # Other valid 15m/60m forecasts are overlapping
                        # windows, not corrupt training examples.
                        continue
                    if (not issued<target+timedelta(minutes=5)<settle<=stamp
                            or y not in (0,1)
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
                scored_base.append((target.isoformat(),values[3],values[5]))
                scored_coin.append((target.isoformat(),values[3],.25))
                calibration_rows.append({
                    "target_opened_at":target.isoformat(),
                    "issued_at":issued.isoformat(),
                    "settled_at":settle.isoformat(),
                    "probability_up":values[0],
                    "reference_probability_up":values[1],
                    "outcome_up":y,
                })
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
                base_proof=score_paired_forward(scored_base)
                coin_proof=score_paired_forward(scored_coin)
                calibration=unique_window_calibration(calibration_rows)
            except (ValueError,TypeError,OverflowError):
                proof=base_proof=coin_proof=None
                calibration={"status":"invalid","ece":None}
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
                and base_proof is not None and base_proof.status=="qualified_shadow"
                and coin_proof is not None and coin_proof.status=="qualified_shadow"
                and calibration["status"]=="research_screen_passed"
                and proof.days>=MIN_DAYS and proof.targets>=MIN_WINDOWS
                and eligible>=MIN_WINDOWS and covered>=MIN_WINDOWS
                and trades>=MIN_TRADES and coverage>=MIN_COVERAGE
                and invalid==0
                and all(s["qualified"] for s in cost_proofs.values())
            )
            # Model Lab designs also clear a stricter bar for the number of
            # designs tried on this timeframe, so a search never crowns a
            # lucky fluke (model_lab.search_bar).
            search=None
            if family.startswith("lab_"):
                from parlay_bot.perpetuals import model_lab
                try:
                    search=model_lab.search_bar(
                        scored_pairs, tried=model_lab.tried_count(horizon, self.store))
                except (ValueError,TypeError,statistics.StatisticsError):
                    search={"passed":False,"lower":None}
                qualified=qualified and bool(search["passed"])
            scored[family]={
                "gate_status": {
                    "days": bool(proof and proof.days >= MIN_DAYS),
                    "targets": bool(proof and proof.targets >= MIN_WINDOWS),
                    "incumbent superiority": bool(proof and proof.status == "qualified_shadow"),
                    "baseline superiority": bool(base_proof and
                                                   base_proof.status == "qualified_shadow"
                                                   and coin_proof and
                                                   coin_proof.status == "qualified_shadow"),
                    "calibration": calibration["status"] == "research_screen_passed",
                    "quotes": eligible >= MIN_WINDOWS and covered >= MIN_WINDOWS
                              and trades >= MIN_TRADES and coverage >= MIN_COVERAGE,
                    "valid records": invalid == 0,
                    "after-cost proof": all(s["qualified"] for s in cost_proofs.values()),
                    "search correction": search is None or bool(search["passed"]),
                },
                "search_bar":search,
                "qualified":qualified,
                "targets":proof.targets if proof else 0,
                "days":proof.days if proof else 0,
                "forward_gain":proof.paired_gain if proof else None,
                "baseline_gain":base_proof.paired_gain if base_proof else None,
                "coin_gain":coin_proof.paired_gain if coin_proof else None,
                "ece":calibration["ece"],
                "calibration_status":calibration["status"],
                "eligible_quotes":eligible,"closed_quotes":covered,
                "quote_coverage":coverage,"challenger_trades":trades,
                "missing_quote_windows":max(0,eligible-covered),
                "duplicate_target_rows":duplicate_target_rows,
                "invalid_rows":invalid,"cost_proof":cost_proofs,
                "real_fills_verified":False,
                "qualification":"prospective_shadow_paper_only",
            }
        # Record the artifact that earned promotion, so selected() can check
        # the served family is still that artifact (spec §2.2). Additive
        # evidence only; no gate or threshold changes.
        if "flow_v3" in scored:
            try:
                from parlay_bot.perpetuals.flow_challenger_v3 import FlowChallengerV3
                scored["flow_v3"]["artifact"] = FlowChallengerV3.artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["flow_v3"]["artifact"] = None
        if "archive_v1" in scored:
            try:
                from parlay_bot.perpetuals.archive_model import artifact_identity
                scored["archive_v1"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["archive_v1"]["artifact"] = None
        if "gbm_v1" in scored:
            try:
                from parlay_bot.perpetuals.gbm_model import artifact_identity
                scored["gbm_v1"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["gbm_v1"]["artifact"] = None
        if "volcal_v1" in scored:
            try:
                from parlay_bot.perpetuals.volcal_model import artifact_identity
                scored["volcal_v1"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["volcal_v1"]["artifact"] = None
        if "xmarket_v1" in scored:
            try:
                from parlay_bot.perpetuals.xmarket_model import artifact_identity
                scored["xmarket_v1"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["xmarket_v1"]["artifact"] = None
        if "liq_v1" in scored:
            try:
                from parlay_bot.perpetuals.liquidity_challenger_v1 import (
                    LiquidityChallengerV1,
                )
                scored["liq_v1"]["artifact"] = (
                    LiquidityChallengerV1.artifact_identity(
                        self.store, asset, horizon))
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["liq_v1"]["artifact"] = None
        if "regime_v1" in scored:
            try:
                from parlay_bot.perpetuals.regime_model import artifact_identity
                scored["regime_v1"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["regime_v1"]["artifact"] = None
        if "regime_wild_v1" in scored:
            try:
                from parlay_bot.perpetuals.wild_model import artifact_identity
                scored["regime_wild_v1"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["regime_wild_v1"]["artifact"] = None
        for cross_family in ("cross_v1", "cross_all_v1"):
            if cross_family in scored:
                try:
                    from parlay_bot.perpetuals import cross_model
                    scored[cross_family]["artifact"] = cross_model.artifact_identity(
                        self.store, cross_family, asset, horizon)
                except Exception:  # noqa: BLE001 - a missing record is no artifact
                    scored[cross_family]["artifact"] = None
        if "regime_v2" in scored:
            try:
                from parlay_bot.perpetuals.regime2_model import artifact_identity
                scored["regime_v2"]["artifact"] = artifact_identity(
                    self.store, asset, horizon)
            except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                scored["regime_v2"]["artifact"] = None
        for live_family in ("regime_live_v1", "regime_live_fast_v1"):
            if live_family in scored:
                try:
                    from parlay_bot.perpetuals.regime_live_model import (
                        artifact_identity as live_artifact_identity,
                    )
                    scored[live_family]["artifact"] = live_artifact_identity(
                        self.store, asset, horizon, family=live_family)
                except Exception:  # noqa: BLE001 - a missing state row records as no artifact
                    scored[live_family]["artifact"] = None
        for family, pinned_sha in pinned_shas.items():
            evidence = scored.get(family)
            if evidence and (evidence.get("artifact") or {}).get("model_sha256") != pinned_sha:
                evidence["qualified"] = False
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
        # First promoted_paper write for flow_v3: emit a Discord receipt
        # (spec §2.7). The receipt is persisted and surfaced through the
        # status card; a receipt failure never blocks governance.
        if selected == "flow_v3" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "flow_v3"):
            try:
                from parlay_bot.perpetuals.flow_challenger_v3 import FlowChallengerV3
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                record_promotion_receipt(
                    self.store, family="flow_v3", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["flow_v3"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=FlowChallengerV3.calibration_status(
                        self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL flow_v3 promotion receipt unavailable")
        # First promoted_paper write for archive_v1: same pattern, same
        # dedupe (one receipt per family/scope/kind, ON CONFLICT DO NOTHING).
        if selected == "archive_v1" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "archive_v1"):
            try:
                from parlay_bot.perpetuals.archive_model import calibration_status
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                record_promotion_receipt(
                    self.store, family="archive_v1", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["archive_v1"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=calibration_status(self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL archive_v1 promotion receipt unavailable")
        # First promoted_paper write for volcal_v1: same pattern, same
        # dedupe (one receipt per family/scope/kind, ON CONFLICT DO NOTHING).
        if selected == "volcal_v1" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "volcal_v1"):
            try:
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                from parlay_bot.perpetuals.volcal_model import calibration_status
                record_promotion_receipt(
                    self.store, family="volcal_v1", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["volcal_v1"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=calibration_status(self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL volcal_v1 promotion receipt unavailable")
        # First promoted_paper write for xmarket_v1: same pattern, same
        # dedupe (one receipt per family/scope/kind, ON CONFLICT DO NOTHING).
        if selected == "xmarket_v1" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "xmarket_v1"):
            try:
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                from parlay_bot.perpetuals.xmarket_model import calibration_status
                record_promotion_receipt(
                    self.store, family="xmarket_v1", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["xmarket_v1"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=calibration_status(self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL xmarket_v1 promotion receipt unavailable")
        # First promoted_paper write for liq_v1: same pattern, same
        # dedupe (one receipt per family/scope/kind, ON CONFLICT DO NOTHING).
        if selected == "liq_v1" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "liq_v1"):
            try:
                from parlay_bot.perpetuals.liquidity_challenger_v1 import (
                    LiquidityChallengerV1,
                )
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                record_promotion_receipt(
                    self.store, family="liq_v1", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["liq_v1"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=LiquidityChallengerV1.calibration_status(
                        self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL liq_v1 promotion receipt unavailable")
        # First promoted_paper write for gbm_v1: same pattern, same
        # dedupe (one receipt per family/scope/kind, ON CONFLICT DO NOTHING).
        if selected == "gbm_v1" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "gbm_v1"):
            try:
                from parlay_bot.perpetuals.gbm_model import calibration_status
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                record_promotion_receipt(
                    self.store, family="gbm_v1", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["gbm_v1"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=calibration_status(self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL gbm_v1 promotion receipt unavailable")
        # First promoted_paper write for regime_v2: same pattern, same
        # dedupe (one receipt per family/scope/kind, ON CONFLICT DO NOTHING).
        # Regime_v1 has no receipt block; v2 adds one rather than copying
        # that gap (spec rival-regime-v2-spec-20261108, §1.6).
        if selected == "regime_v2" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "regime_v2"):
            try:
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                from parlay_bot.perpetuals.regime2_model import calibration_status
                record_promotion_receipt(
                    self.store, family="regime_v2", venue=venue, contract=contract,
                    quote=quote, horizon=horizon, kind="promoted",
                    evidence={**scored["regime_v2"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=calibration_status(self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL regime_v2 promotion receipt unavailable")
        # First promoted_paper write for the live families: same pattern,
        # same dedupe (one receipt per family/scope/kind, ON CONFLICT DO
        # NOTHING). The receipt preserves the weights SHA the family
        # earned promotion with (spec
        # rival-regime-live-learning-spec-20261108, D6): the live
        # learner's weights keep moving after promotion under
        # continuity, and the receipt is the record of what was
        # promoted, not a pin.
        for live_family in ("regime_live_v1", "regime_live_fast_v1"):
            if selected == live_family and not (
                    previous and previous["status"] == "promoted_paper"
                    and previous["selected_family"] == live_family):
                try:
                    from parlay_bot.perpetuals.promotion_receipts import (
                        record_promotion_receipt,
                    )
                    from parlay_bot.perpetuals.regime_live_model import (
                        calibration_status as live_calibration_status,
                    )
                    record_promotion_receipt(
                        self.store, family=live_family, venue=venue,
                        contract=contract, quote=quote, horizon=horizon,
                        kind="promoted",
                        evidence={**scored[live_family],
                                  "evaluated_at": stamp.isoformat()},
                        calibration=live_calibration_status(
                            self.store, asset, horizon, family=live_family),
                        now=stamp)
                except Exception:  # noqa: BLE001 - a receipt must never block governance
                    _LOG.exception("RivaL %s promotion receipt unavailable", live_family)
        # First promoted_paper write for regime_wild_v1 (audit Step 5):
        # same pattern, same dedupe. Regime_v1 has no receipt block; the
        # wild family adds one rather than copying that gap.
        if selected == "regime_wild_v1" and not (
                previous and previous["status"] == "promoted_paper"
                and previous["selected_family"] == "regime_wild_v1"):
            try:
                from parlay_bot.perpetuals.promotion_receipts import (
                    record_promotion_receipt,
                )
                from parlay_bot.perpetuals.wild_model import (
                    calibration_status as wild_calibration_status,
                )
                record_promotion_receipt(
                    self.store, family="regime_wild_v1", venue=venue,
                    contract=contract, quote=quote, horizon=horizon,
                    kind="promoted",
                    evidence={**scored["regime_wild_v1"],
                              "evaluated_at": stamp.isoformat()},
                    calibration=wild_calibration_status(
                        self.store, asset, horizon),
                    now=stamp)
            except Exception:  # noqa: BLE001 - a receipt must never block governance
                _LOG.exception("RivaL regime_wild_v1 promotion receipt unavailable")
        # D14 pin verdicts: a selected offline family is promoted; a pinned
        # fit whose evidence is exhausted without a promotion is rejected.
        # The next queued fit is admitted on the next reload
        # (offline_model_pins).
        for _family in ("archive_v1", "gbm_v1", "regime_v2", "volcal_v1", "xmarket_v1"):
            try:
                from parlay_bot.perpetuals import offline_model_pins
                offline_model_pins.resolve_pin(
                    self.store, _family, asset, horizon,
                    selected=(selected == _family), now=stamp,
                    expected_sha=pinned_shas[_family], lookback=LOOKBACK)
            except Exception:  # noqa: BLE001 - a pin verdict never blocks governance
                _LOG.exception("RivaL %s pin verdict unavailable", _family)
        return {"status":status,"selected_family":selected,"families":scored,
                "market":(venue,contract,quote,horizon),
                "real_fills_verified":False}

    def selected(self, *, asset,venue,contract,quote,horizon,now=None):
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            row=conn.execute(
                """SELECT selected_family,status,evaluated_at,evidence_json
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
        selected = str(row["selected_family"])
        # Kill switch 2: a blocked family never serves, even with a fresh
        # promoted_paper row.
        if selected in BLOCKED_FAMILIES:
            return None
        if selected in FAMILIES:
            return selected
        if selected == "flow_v3":
            # Artifact-identity check: the served family must be the
            # continuation of the artifact that earned promotion -- same
            # feature contract, training only moved forward, weights moved
            # only by graded learning. Exact weights-SHA equality with
            # promotion time would fail closed on every routine per-cycle
            # online-learning step (learn() runs each cycle), so the check
            # verifies continuity on all three recorded fields instead of
            # a frozen weight vector: digest equal, trained-through and
            # graded count monotone, and a changed weights SHA is accepted
            # only when graded learning actually advanced. A refit/reset
            # (digest change, cursor/count regression, or unexplained
            # weight change) blocks serve until the family re-earns
            # promotion. The promotion-time weights SHA is preserved in
            # the evidence/receipt for audit.
            try:
                from parlay_bot.perpetuals.flow_challenger_v3 import FlowChallengerV3
                recorded = json.loads(row["evidence_json"])["flow_v3"]["artifact"]
                current = FlowChallengerV3.artifact_identity(
                    self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            if not recorded or not current:
                return None
            try:
                recorded_sha = str(recorded["weights_sha256"])
                recorded_digest = str(recorded["feature_digest"])
                recorded_through = utc(datetime.fromisoformat(
                    str(recorded["trained_through"])))
                recorded_count = int(recorded["graded_count"])
                current_through = utc(datetime.fromisoformat(
                    str(current["trained_through"])))
                current_count = int(current["graded_count"])
            except (ValueError, TypeError, KeyError):
                return None
            if (len(recorded_sha) != 64
                    or recorded_digest != current["feature_digest"]
                    or current_through < recorded_through
                    or current_count < recorded_count):
                return None
            if (str(current["weights_sha256"]) != recorded_sha
                    and not (current_count > recorded_count
                             and current_through >= recorded_through)):
                return None
            return selected
        if selected == "archive_v1":
            # D14 pinning (autonomous loop §11): the served family must be
            # the pinned artifact that earned promotion -- exact weights-SHA
            # match, same feature digest, trained-through and graded count
            # (under that artifact's hash) monotone. A retrain queues
            # behind the pinned fit and never inherits its promotion; a
            # rejected pin advances the queue (offline_model_pins).
            try:
                from parlay_bot.perpetuals.archive_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["archive_v1"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return selected if _pinned_identity_matches(recorded, current) else None
        if selected == "gbm_v1":
            # Same D14 pin check as archive_v1, on the GBM artifact
            # (spec §3.4): the served family must be the pinned artifact
            # that earned promotion -- exact weights-SHA match, same
            # feature digest, trained-through and graded count (under that
            # artifact's hash) monotone. A queued fit never inherits the
            # promotion; a rejected pin advances the queue.
            try:
                from parlay_bot.perpetuals.gbm_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["gbm_v1"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return selected if _pinned_identity_matches(recorded, current) else None
        if selected == "volcal_v1":
            # Same D14 pin check as archive_v1/gbm_v1, on the volcal
            # artifact: the served family must be the pinned artifact
            # that earned promotion -- exact weights-SHA match, same
            # feature digest, trained-through and graded count (under that
            # artifact's hash) monotone. A queued fit never inherits the
            # promotion; a rejected pin advances the queue.
            try:
                from parlay_bot.perpetuals.volcal_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["volcal_v1"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return selected if _pinned_identity_matches(recorded, current) else None
        if selected == "xmarket_v1":
            # Same D14 pin check as archive_v1/gbm_v1, on the xmarket
            # artifact: the served family must be the pinned artifact
            # that earned promotion -- exact weights-SHA match, same
            # feature digest, trained-through and graded count (under that
            # artifact's hash) monotone. A queued fit never inherits the
            # promotion; a rejected pin advances the queue.
            try:
                from parlay_bot.perpetuals.xmarket_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["xmarket_v1"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return selected if _pinned_identity_matches(recorded, current) else None
        if selected == "regime_v2":
            # Same D14 pin check as archive_v1/gbm_v1, on the regime_v2
            # artifact (spec rival-regime-v2-spec-20261108, D3/D5):
            # the served family must be the pinned artifact that earned
            # promotion -- exact weights-SHA match, same feature digest
            # (which pins the trending-subset descriptor), trained-through
            # and graded count (under that artifact's hash) monotone. A
            # retrain queues behind the pinned fit and never inherits
            # its promotion; a rejected pin advances the queue.
            try:
                from parlay_bot.perpetuals.regime2_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["regime_v2"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return selected if _pinned_identity_matches(recorded, current) else None
        if selected == "liq_v1":
            # Same continuity check as flow_v3, on the liq_v1 artifact
            # (spec §2.10.2, D13): the served family must be the
            # continuation of the artifact that earned promotion -- same
            # feature contract, training only moved forward, weights moved
            # only by graded learning. Exact weights-SHA equality with
            # promotion time would fail closed on every routine per-cycle
            # online-learning step, so the check verifies continuity on
            # all three recorded fields instead of a frozen weight vector.
            # A refit/reset (digest change, cursor/count regression, or
            # unexplained weight change) blocks serve until the family
            # re-earns promotion.
            try:
                from parlay_bot.perpetuals.liquidity_challenger_v1 import (
                    LiquidityChallengerV1,
                )
                recorded = json.loads(row["evidence_json"])["liq_v1"]["artifact"]
                current = LiquidityChallengerV1.artifact_identity(
                    self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            if not recorded or not current:
                return None
            try:
                recorded_sha = str(recorded["weights_sha256"])
                recorded_digest = str(recorded["feature_digest"])
                recorded_through = utc(datetime.fromisoformat(
                    str(recorded["trained_through"])))
                recorded_count = int(recorded["graded_count"])
                current_through = utc(datetime.fromisoformat(
                    str(current["trained_through"])))
                current_count = int(current["graded_count"])
            except (ValueError, TypeError, KeyError):
                return None
            if (len(recorded_sha) != 64
                    or recorded_digest != current["feature_digest"]
                    or current_through < recorded_through
                    or current_count < recorded_count):
                return None
            if (str(current["weights_sha256"]) != recorded_sha
                    and not (current_count > recorded_count
                             and current_through >= recorded_through)):
                return None
            return selected
        if selected == "regime_v1":
            # Same continuity check as flow_v3/liq_v1, on the regime
            # identity (spec rival-regime-gated-spec-20261107, D3/D5):
            # the served call is the gated live logistic, so the check
            # verifies the live weights the gate last served are a
            # continuation of the promoted ones -- same feature digest,
            # trained-through and graded count monotone. No offline
            # artifact exists, so there is no D14 pin to check.
            # A refit/reset (digest change, cursor/count regression, or
            # unexplained weight change) blocks serve until the family
            # re-earns promotion.
            try:
                from parlay_bot.perpetuals.regime_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["regime_v1"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            if not recorded or not current:
                return None
            try:
                recorded_sha = str(recorded["weights_sha256"])
                recorded_digest = str(recorded["feature_digest"])
                recorded_through = utc(datetime.fromisoformat(
                    str(recorded["trained_through"])))
                recorded_count = int(recorded["graded_count"])
                current_through = utc(datetime.fromisoformat(
                    str(current["trained_through"])))
                current_count = int(current["graded_count"])
            except (ValueError, TypeError, KeyError):
                return None
            if (len(recorded_sha) != 64
                    or recorded_digest != current["feature_digest"]
                    or current_through < recorded_through
                    or current_count < recorded_count):
                return None
            if (str(current["weights_sha256"]) != recorded_sha
                    and not (current_count > recorded_count
                             and current_through >= recorded_through)):
                return None
            return selected
        if selected in ("cross_v1", "cross_all_v1"):
            # Cross-exchange live learners (cross_model.py): no offline
            # artifact, so the same continuity check as regime_v1 -- the
            # learner behind the latest graded row must be a forward
            # continuation of the one that earned promotion (same feature
            # digest, trained-through and graded count never backwards).
            try:
                from parlay_bot.perpetuals import cross_model
                recorded = json.loads(row["evidence_json"])[selected]["artifact"]
                current = cross_model.artifact_identity(
                    self.store, selected, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            return selected if cross_model.live_continuity(recorded, current) else None
        if selected == "regime_wild_v1":
            # Same continuity check as regime_v1, on the wild identity
            # (audit Step 5): the served call is the gated live logistic,
            # so the check verifies the live weights the gate last served
            # are a continuation of the promoted ones -- same feature
            # digest, trained-through and graded count monotone. No
            # offline artifact exists, so there is no D14 pin to check.
            try:
                from parlay_bot.perpetuals.wild_model import artifact_identity
                recorded = json.loads(row["evidence_json"])["regime_wild_v1"]["artifact"]
                current = artifact_identity(self.store, asset, horizon)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            if not recorded or not current:
                return None
            try:
                recorded_sha = str(recorded["weights_sha256"])
                recorded_digest = str(recorded["feature_digest"])
                recorded_through = utc(datetime.fromisoformat(
                    str(recorded["trained_through"])))
                recorded_count = int(recorded["graded_count"])
                current_through = utc(datetime.fromisoformat(
                    str(current["trained_through"])))
                current_count = int(current["graded_count"])
            except (ValueError, TypeError, KeyError):
                return None
            if (len(recorded_sha) != 64
                    or recorded_digest != current["feature_digest"]
                    or current_through < recorded_through
                    or current_count < recorded_count):
                return None
            if (str(current["weights_sha256"]) != recorded_sha
                    and not (current_count > recorded_count
                             and current_through >= recorded_through)):
                return None
            return selected
        if selected in ("regime_live_v1", "regime_live_fast_v1"):
            # Same continuity check as regime_v1, on the live family's
            # identity (spec rival-regime-live-learning-spec-20261108,
            # D5/D6): the served call is the family's own live learner,
            # so the check verifies the live weights the family last
            # served are a continuation of the promoted ones -- same
            # feature digest, trained-through and graded count monotone.
            # No D14 pin: the learner keeps moving under continuity, and
            # a weight change with more graded learning behind it is a
            # continuation, not a refit. A refit/reset (digest change,
            # cursor/count regression, or unexplained weight change)
            # blocks serve until the family re-earns promotion.
            try:
                from parlay_bot.perpetuals.regime_live_model import (
                    artifact_identity as live_artifact_identity,
                )
                recorded = json.loads(row["evidence_json"])[selected]["artifact"]
                current = live_artifact_identity(
                    self.store, asset, horizon, family=selected)
            except (ValueError, TypeError, KeyError, AttributeError):
                return None
            if not recorded or not current:
                return None
            try:
                recorded_sha = str(recorded["weights_sha256"])
                recorded_digest = str(recorded["feature_digest"])
                recorded_through = utc(datetime.fromisoformat(
                    str(recorded["trained_through"])))
                recorded_count = int(recorded["graded_count"])
                current_through = utc(datetime.fromisoformat(
                    str(current["trained_through"])))
                current_count = int(current["graded_count"])
            except (ValueError, TypeError, KeyError):
                return None
            if (len(recorded_sha) != 64
                    or recorded_digest != current["feature_digest"]
                    or current_through < recorded_through
                    or current_count < recorded_count):
                return None
            if (str(current["weights_sha256"]) != recorded_sha
                    and not (current_count > recorded_count
                             and current_through >= recorded_through)):
                return None
            return selected
        if selected.startswith("pooled_"):
            # Serves only while it is still the active pooled model for this
            # horizon; a newer nightly model must earn its own promotion.
            from parlay_bot.perpetuals.pooled_model import PooledModelSet
            try:
                models = PooledModelSet(self.store)
                serving = {(doc or {}).get("_family") for doc in (
                    models.active(horizon, now=stamp),
                    models.active(horizon, now=stamp, context=False))}
            except Exception:  # noqa: BLE001 - fail closed to the incumbent
                return None
            return selected if selected in serving else None
        if selected.startswith("historical_"):
            # A paper winner cannot serve after its screened source-specific
            # historical artifact disappears or becomes incompatible.
            from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
            try:
                candidates = ResearchCandidateRegistry(self.store).compatible_candidates(
                    venue=venue, contract=contract, quote=quote,
                    horizon=horizon, now=stamp,
                )
            except (ValueError, TypeError, KeyError):
                return None
            return selected if any(
                selected == f"historical_{candidate['model_sha256'][:48]}"
                for candidate in candidates
            ) else None
        if not selected.startswith("boosted_"):
            return None
        # A missing/revoked/mismatched artifact cannot keep its old paper
        # promotion record indefinitely. It must still be a viable exact
        # source research model at the moment of a watch.
        from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
        try:
            candidates = ResearchCandidateRegistry(self.store).compatible_boosters(
                venue=venue, contract=contract, quote=quote,
                horizon=horizon, now=stamp,
            )
        except (ValueError, TypeError, KeyError):
            return None
        return selected if any(
            selected == f"boosted_{candidate['family']}_{candidate['model_sha256'][:12]}"
            for candidate in candidates
        ) else None

    def venue_summary(self, *, asset,venue,contract,quote,horizon,
                      model_version,now=None):
        """Signal proof for precisely the same promoted model and market.

        No borrowing logistic or fused scores for the family version.
        The response is a *research watch* only, not real fills.
        """
        family=self.selected(asset=asset,venue=venue,contract=contract,
                             quote=quote,horizon=horizon,now=now)
        rejected={
            "settled":0,"hit_rate":None,"model_beats_baseline":False,
            "watch_evidence_passes":False,"watch_days":0,
            "watch_reason":"family did not earn current source-specific promotion",
        }
        if family is None or model_version!=family:
            return rejected
        with self.store.connect() as conn:
            row=conn.execute(
                """SELECT evidence_json FROM rival_family_promotion
                   WHERE venue=? AND contract=? AND quote=? AND asset=?
                    AND horizon_minutes=? AND status='promoted_paper'
                    AND selected_family=?""",
                (venue,contract,quote,asset,horizon,family),
            ).fetchone()
        if row is None:
            return rejected
        try:
            info=json.loads(row["evidence_json"])[family]
            if not (info["qualified"] and info["targets"]>=MIN_WINDOWS
                    and info["days"]>=MIN_DAYS
                    and info["cost_proof"]["severe"]["qualified"]):
                return rejected
            return {
                "settled":info["targets"],"hit_rate":None,
                "model_beats_baseline":True,
                "watch_evidence_passes":True,"watch_days":info["days"],
                "watch_reason":"source-specific future/quote paper proof passed",
                "real_fills_verified":False,
            }
        except (ValueError,TypeError,KeyError):
            return rejected
