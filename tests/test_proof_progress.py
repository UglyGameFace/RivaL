"""Source-specific model improvement must remain explainable and fail closed."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import (
    _SECTIONS,
    ACCURACY_SECTIONS,
    ResearchDashboard,
)
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.proof_progress import proof_progress, proof_progress_text
from tests.test_family_cost_promotion import _seed


def _scope():
    return {"asset": "BTC", "venue": "hyperliquid"}


def test_no_saved_evaluations_cannot_claim_profit(learning_store):
    result = proof_progress(learning_store, **_scope())
    assert result["status"] in ("no_evaluations", "unavailable")
    assert not result["evaluations"]
    assert "No" in proof_progress_text(result)


def test_only_exact_fresh_paper_candidate_can_pass_all_gates(learning_store):
    now = datetime(2026, 11, 2, 16, tzinfo=UTC)
    winner = _seed(learning_store, now)
    decision = FamilyPromotion(learning_store).evaluate(
        now=now, contract="BTC", quote="USDC", horizon=5, **_scope(),
    )
    assert decision["status"] == "promoted_paper"
    result = proof_progress(learning_store, now=now, **_scope())
    assert result["status"] == "recorded"
    example = result["evaluations"][0]
    assert example["candidate"]["name"] == winner
    assert example["candidate"]["targets"] == 168
    assert example["candidate"]["days"] == 21
    assert example["candidate"]["closed_quotes"] == 168
    assert all(example["candidate"]["checks"].values())
    assert example["paper_qualified"]
    readable = proof_progress_text(result)
    assert "outcomes ✓" in readable
    assert "baselines ✓" in readable
    assert "quotes ✓" in readable
    assert "cost ✓" in readable
    assert "records ✓" in readable
    assert "real fills and real profitability are NOT verified" in readable
    assert not proof_progress(learning_store, asset="ETH", venue="hyperliquid",
                              now=now)["evaluations"]
    assert not proof_progress(learning_store, asset="BTC", venue="okx",
                              now=now)["evaluations"]
    expired = proof_progress(
        learning_store, now=now+timedelta(hours=49), **_scope(),
    )
    assert not expired["evaluations"][0]["paper_qualified"]
    assert "expired" in proof_progress_text(expired)


def test_inadequate_quotes_show_missing_coverage_not_a_win(learning_store):
    now = datetime(2026, 11, 2, 16, tzinfo=UTC)
    _seed(learning_store, now, quote_count=110)
    decision = FamilyPromotion(learning_store).evaluate(
        now=now, contract="BTC", quote="USDC", horizon=5, **_scope(),
    )
    assert decision["status"] == "held"
    sample = proof_progress(learning_store, now=now, **_scope())["evaluations"][0]
    assert not sample["paper_qualified"]
    assert sample["candidate"]["targets"] == 168
    assert not sample["candidate"]["checks"]["quote_coverage"]
    assert not sample["candidate"]["checks"]["after_cost_paper"]
    assert "quote coverage" in sample["reason"]
    assert "quotes ✗ (58 missing)" in proof_progress_text({"evaluations": [sample]})


def test_bad_saved_proof_never_becomes_qualified(learning_store):
    now = datetime(2026, 11, 2, 16, tzinfo=UTC)
    _seed(learning_store, now)
    FamilyPromotion(learning_store).evaluate(
        now=now, contract="BTC", quote="USDC", horizon=5, **_scope(),
    )
    with learning_store.connect() as conn:
        value = conn.execute(
            "SELECT evidence_json FROM rival_family_promotion"
        ).fetchone()["evidence_json"]
        data = json.loads(value)
        name = next(iter(data))
        data[name]["closed_quotes"] += 9000
        conn.execute(
            "UPDATE rival_family_promotion SET evidence_json=?",
            (json.dumps(data),),
        )
    result = proof_progress(learning_store, now=now, **_scope())
    assert not result["evaluations"][0]["paper_qualified"]
    assert "invalid" in result["evaluations"][0]["reason"].lower()


@pytest.mark.asyncio
async def test_progress_is_a_lightweight_navigable_results_section(learning_store):
    now = datetime(2026, 11, 2, 16, tzinfo=UTC)
    _seed(learning_store, now, quote_count=110)
    FamilyPromotion(learning_store).evaluate(
        now=now, contract="BTC", quote="USDC", horizon=5, **_scope(),
    )
    panel = ResearchDashboard(
        owner_id=42, market=PerpetualMarketStore(learning_store),
        ai=None, venues=("hyperliquid", "okx"),
    )
    panel.screen = "accuracy"
    panel.section = "progress"
    # Audit Step 3: proof is the detail topic inside "Is anything better?".
    panel.detail = "progress"
    panel.selected_venue = "hyperliquid"
    panel._rebuild()
    try:
        assert "progress" in ACCURACY_SECTIONS["better"]
        assert "progress" not in dict(_SECTIONS["accuracy"])
        assert any(item.custom_id == "rival:research:venue" for item in panel.children)
        result = await panel.render()
        fields = {field.name: field.value for field in result.fields}
        assert "Future model proof" in fields
        # Audit Step 2: the card answers first — "Not yet" is the honest
        # verdict for an evaluated-but-unqualified candidate, not a "No".
        assert fields["Future model proof"].split("\n")[0] == (
            "Not yet — no test model passed proof.")
        assert "real profitability" in fields["Future model proof"]
        assert len(fields["Future model proof"]) <= 1024
    finally:
        panel.stop()

def test_quote_gap_and_overlap_evidence_is_visible_and_fail_closed(learning_store):
    now = datetime(2026, 11, 2, 16, tzinfo=UTC)
    _seed(learning_store, now, quote_count=110)
    FamilyPromotion(learning_store).evaluate(
        now=now, contract="BTC", quote="USDC", horizon=5, **_scope(),
    )
    with learning_store.connect() as conn:
        saved = conn.execute(
            "SELECT evidence_json FROM rival_family_promotion"
        ).fetchone()
        data = json.loads(saved["evidence_json"])
        name = next(iter(data))
        assert data[name]["missing_quote_windows"] == 58
        data[name]["duplicate_target_rows"] = 3
        conn.execute(
            "UPDATE rival_family_promotion SET evidence_json=?",
            (json.dumps(data),),
        )
    progress = proof_progress(learning_store, now=now, **_scope())
    candidate = progress["evaluations"][0]["candidate"]
    assert candidate["valid"]
    assert candidate["missing_quote_windows"] == 58
    assert candidate["duplicate_target_rows"] == 3
    text = proof_progress_text(progress)
    assert "(58 missing, 3 dup)" in text
    assert "quotes ✗" in text
    # A corrupt persisted summary is never silently shown as qualified.
    data[name]["missing_quote_windows"] = 0
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_family_promotion SET evidence_json=?",
            (json.dumps(data),),
        )
    invalid = proof_progress(learning_store, now=now, **_scope())
    assert not invalid["evaluations"][0]["candidate"]["valid"]
    assert not invalid["evaluations"][0]["paper_qualified"]
