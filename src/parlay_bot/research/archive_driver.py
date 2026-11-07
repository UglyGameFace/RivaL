"""Minimal research-loop driver for archive-model feature hypotheses.

Spec §3 option B / D10 (approved pick): the research loop exists with
zero callers (§1.6). This driver runs it end-to-end for archive-model
feature hypotheses: propose (already recorded) -> verify -> offline
ablation -> finding. It stops at ``screened``; shadow issuance and
promotion are Phase 2 scope and are not driven here.

Decision rights are unchanged: actor ``llm`` is refused by
research_loop.transition (TRANSITION_ACTORS = ("system", "human")); this
driver only ever acts as "system", and the LLM's feedback stays
numeric-only via the loop's _numeric_outcomes. The driver never selects
a model, sets a weight, or approves a promotion.
"""

from __future__ import annotations

from datetime import datetime

from parlay_bot.perpetuals.research_loop import ResearchLoop


def drive_archive_feature_hypotheses(
    store,
    *,
    asset: str,
    receipt: object,
    venue: str,
    hypothesis: str = "volume_only",
    trials: list[dict] | None = None,
    ablate_kwargs: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Run one archive feature hypothesis through verify -> ablation.

    ``receipt`` is the verified LLM-proposal receipt
    (verify_proposals boundary). ``trials`` are the dicts returned by
    evaluate_hypothesis_ablations, or an ablation callable is used via
    ``ablate_kwargs``. Returns the final experiment dict plus its
    findings. Every closed experiment leaves a finding.
    """
    loop = ResearchLoop(store)
    loop.initialize()
    experiment = loop.create(
        proposer="human",
        hypothesis=hypothesis,
        design={
            "asset": asset,
            "venues": [venue],
            "model_shape": "archive-offline-logistic-v1",
            "calibration_method": "platt-scaling-ece<=0.10",
        },
        now=now,
    )
    experiment_id = experiment["experiment_id"]
    verified = loop.verify(experiment_id, receipt, venue=venue, now=now)
    if verified["status"] == "failed":
        return {"experiment": verified, "findings": loop.findings(experiment_id)}
    ablated = loop.ablate(experiment_id, trials=trials, ablate_kwargs=ablate_kwargs, now=now)
    if ablated["status"] == "discarded":
        return {"experiment": ablated, "findings": loop.findings(experiment_id)}
    outcome = ablated.get("outcome") or {}
    loop.finding(
        experiment_id=experiment_id,
        kind="discovery",
        summary=(
            f"Archive feature hypothesis {hypothesis} for {asset} "
            f"passed offline ablation: {outcome.get('passed_trials')} "
            f"trial(s) beat the baseline on the sealed holdout "
            f"(holdout Brier delta "
            f"{outcome.get('holdout_brier_delta')}). Screened for "
            f"future shadow; no promotion claimed."
        ),
        evidence_ref=f"rival_research_experiments:{experiment_id}",
        recorded_by="system",
    )
    return {"experiment": loop.get(experiment_id), "findings": loop.findings(experiment_id)}
