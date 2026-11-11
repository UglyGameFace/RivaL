"""One shared answer-first card builder + the one set of display names.

Why this module exists (audit Step 2, 2026-11-10): Detailed Research has
25 sections across 3 screens, and each screen formatted its own text
inside two long render functions (``GuidedHome.render`` in home.py and
``ResearchDashboard._render_full`` in research_dashboard.py), which is why
the wording drifted apart — the same model was "incumbent" on one screen,
"live model" on another, and "mathematical model" on a third. Both
renderers now call this module, so there is exactly one place where a
card's answer, wording, and disclaimer are decided.

Rules every converted card follows (the audit's rules 1–6):

1. Answer first: line one is Yes, No, Not yet or Can't tell, ≤12 words.
2. One reason, one next step: line two says why; line three says what
   would change it, as progress toward a target (e.g. "day 3 of 21").
3. Details on request: one sentence per number; a full table appears
   only under "Raw numbers". Internal model ids live in the raw view
   ONLY, so logs can still be matched.
4. At most three numbers per paragraph; a fourth belongs in the raw view.
5. One disclaimer, in the footer: "Research only. No real trades."
   (the honesty substance — "not proven", "no real fills" — stays in
   the card bodies, it is not deleted).
6. Feed status only when it is the news: "Live market feeds" on the
   overview, and elsewhere only if a feed is behind.

This module imports nothing from the Discord app, so home.py and
research_dashboard.py can both import it without a cycle. Callers fit
the finished value with ``fit_embed_field_value`` (home.py) as before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The single footer disclaimer (rule 5). Converted cards use exactly this.
RESEARCH_FOOTER = "Research only. No real trades."

# Marker for the raw-numbers block. Everything after it keeps internal
# ids verbatim; ``rename_text`` never rewrites past this marker.
RAW_MARKER = "Raw numbers:"

# Route B proof bars (parlay_bot/perpetuals/family_governance.py:
# MIN_DAYS = 21, MIN_WINDOWS = 150). Kept as constants here so this
# module stays importable without the perpetuals package; a test pins
# them against family_governance so drift fails loudly.
PROOF_MIN_DAYS = 21
PROOF_MIN_TARGETS = 150

VERDICTS = ("Yes", "No", "Not yet", "Can't tell")

# The name table (audit Step 2). Old term -> the one display name.
# Order matters: phrase rules run before the bare words they contain,
# and internal family ids run before the bare words inside them, so
# "regime_live_v1" becomes "trend learner", never "market_live_v1".
# New names never contain an old term as a whole word, so renaming is
# idempotent: running it over already-renamed text changes nothing.
DISPLAY_NAMES: dict[str, str] = {
    # Internal family ids first (longest first at runtime).
    "regime_live_fast_v1": "fast trend learner",
    "regime_live_v1": "trend learner",
    "gaussian_nb_v1": "pattern test A",
    "knn21_v1": "pattern test B",
    "regime_v1": "trend-only test",
    "regime_wild_v1": "wild-only test",
    "cross_all_v1": "all-coins test",
    "cross_v1": "all-exchanges test",
    # Phrases before their bare words.
    "paired brier gain": "paired error score gain",
    "market regime": "market",
    "quiet regime": "calm market",
    "turbulent regime": "wild market",
    "normal regime": "normal market",
    "simple guess": "coin flip",
    "basic forecast": "coin flip",
    "guess 0.250": "coin flip 0.250",
    "mathematical model": "main model",
    "live model": "main model",
    "advanced models": "6-year history models",
    "advanced model": "6-year history model",
    "boosted model": "6-year history model",
    "pooled 6-year model": "6-year history model",
    "challenger models": "test models",
    "shadow model": "test model",
    "shadow quote": "practice trade",
    "trial detail": "test-model detail",
    "paper-qualified": "practice-trade qualified",
    "on trial": "being tested",
    # Bare words.
    "challenger": "test model",
    "incumbent": "main model",
    "baseline": "coin flip",
    "guess": "coin flip",
    "brier": "error score",
    "pooled": "6-year history model",
    "boosted": "6-year history model",
    "advanced": "6-year history model",
    "shadow": "test model",
    "family": "test model",
    "families": "test models",
    "trial": "test model",
    "trials": "tests",
    "quiet": "calm",
    "turbulent": "wild",
    "regime": "market",
    "settled": "graded",
    "scored": "graded",
    "decided": "graded",
    "paper": "practice trade",
    "simulated": "practice trade",
}

# Longest old term first, so "regime_live_fast_v1" wins over "regime_v1"
# and "paired brier gain" wins over "brier".
_NAME_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\b" + re.escape(old) + r"\b", re.IGNORECASE),
        new,
    )
    for old, new in sorted(
        DISPLAY_NAMES.items(), key=lambda item: -len(item[0])
    )
]

_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def _count_numbers(text: str) -> int:
    return len(_NUMBER_RE.findall(text))


def rename_text(text: str) -> str:
    """Apply the name table to user-facing text.

    The "Raw numbers:" block (and everything after it) is left verbatim
    so internal ids stay matchable against logs. Already-renamed text
    passes through unchanged. A leading capital on the matched term is
    preserved (``Brier`` -> ``Error score``).
    """
    if not text:
        return text
    head, sep, raw = text.partition(RAW_MARKER)
    renamed = head
    for pattern, new in _NAME_RULES:
        def _replace(match: re.Match, new: str = new) -> str:
            word = new
            if match.group(0)[:1].isupper():
                word = word[:1].upper() + word[1:]
            return word
        renamed = pattern.sub(_replace, renamed)
    if not sep:
        return renamed
    return renamed + sep + raw


@dataclass
class AnswerCard:
    """One answer-first card (rules 1–4).

    ``answer`` is line one: it must start with the verdict and be at
    most 12 words. ``reason`` is line two, ``next_step`` is line three
    (progress toward a target). ``details`` is one sentence per number.
    ``raw`` is the full table / original text, shown only under
    "Raw numbers:" — internal ids are allowed (and expected) there.
    """

    question: str
    verdict: str
    answer: str
    reason: str
    next_step: str
    details: tuple[str, ...] = ()
    raw: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}: {self.verdict!r}")
        if not self.answer.startswith(self.verdict):
            raise ValueError(
                f"answer must start with the verdict {self.verdict!r}: {self.answer!r}"
            )
        words = self.answer.split()
        if len(words) > 12:
            raise ValueError(
                f"answer must be at most 12 words, got {len(words)}: {self.answer!r}"
            )
        for label, paragraph in (("reason", self.reason), ("next_step", self.next_step)):
            if _count_numbers(paragraph) > 3:
                raise ValueError(
                    f"{label} has more than 3 numbers (rule 4): {paragraph!r}"
                )
        for detail in self.details:
            if _count_numbers(detail) > 3:
                raise ValueError(
                    f"detail has more than 3 numbers (rule 4): {detail!r}"
                )

    def field_value(self) -> str:
        """The embed field value: answer, reason, next step, details, raw."""
        lines = [self.answer, self.reason, self.next_step, *self.details]
        if self.raw:
            lines.append(f"{RAW_MARKER}\n{self.raw}")
        return "\n".join(lines)


def _missing_checks(candidate: dict) -> str:
    checks = candidate.get("checks") or {}
    labels = {
        "future_outcomes": "outcomes",
        "beats_baselines": "baselines",
        "quote_coverage": "quotes",
        "after_cost_paper": "cost",
        "valid_records": "records",
    }
    missing = [labels.get(key, key) for key, ok in checks.items() if not ok]
    return ", ".join(missing) or "approval"


def proof_card(proof: dict, *, raw_text: str) -> AnswerCard:
    """Answer-first card for "Is any model proven better?".

    ``proof`` is the ``proof_progress()`` dict; ``raw_text`` is the
    original ``proof_progress_text(proof)`` — the full per-horizon
    check table, which keeps internal family ids under "Raw numbers:".
    """
    evaluations = proof.get("evaluations") or []
    if not evaluations:
        return AnswerCard(
            question="Is any model proven better?",
            verdict="Can't tell",
            answer="Can't tell — no proof evaluation recorded.",
            reason="No source-specific model qualification has been recorded.",
            next_step="Needs new genuinely future results to judge.",
            raw=raw_text,
        )
    qualified = next(
        (ev for ev in evaluations if ev.get("paper_qualified")), None,
    )
    if qualified is not None:
        candidate = qualified.get("candidate") or {}
        name = rename_text(str(candidate.get("name") or "test model"))
        return AnswerCard(
            question="Is any model proven better?",
            verdict="Yes",
            answer="Yes — a test model passed paper proof.",
            reason=(
                f"{name} passed all five proof checks "
                f"on {qualified['horizon']}m."
            ),
            next_step="Next: verify real fills before any live use.",
            details=(
                (f"{int(candidate.get('targets') or 0):,} graded targets over "
                 f"{int(candidate.get('days') or 0)} days."),
                (f"Quote coverage "
                 f"{float(candidate.get('quote_coverage') or 0):.0%} on "
                 f"{int(candidate.get('closed_quotes') or 0):,} of "
                 f"{int(candidate.get('eligible_quotes') or 0):,} quotes."),
            ),
            raw=raw_text,
        )
    top = evaluations[0]
    candidate = top.get("candidate") or {}
    if not candidate.get("valid", True):
        return AnswerCard(
            question="Is any model proven better?",
            verdict="Can't tell",
            answer="Can't tell — saved proof cannot be verified.",
            reason=str(top.get("reason") or "Saved evaluation is invalid."),
            next_step="Needs a fresh evaluation to judge.",
            raw=raw_text,
        )
    name = rename_text(str(candidate.get("name") or "test model"))
    targets = int(candidate.get("targets") or 0)
    days = int(candidate.get("days") or 0)
    return AnswerCard(
        question="Is any model proven better?",
        verdict="Not yet",
        answer="Not yet — no test model passed proof.",
        reason=f"{name} is still missing: {_missing_checks(candidate)}.",
        next_step=(
            f"Day {days} of {PROOF_MIN_DAYS} — {targets:,} targets so far."
        ),
        details=(
            f"{targets:,} graded targets over {days} days.",
            (f"Quote coverage "
             f"{float(candidate.get('quote_coverage') or 0):.0%} on "
             f"{int(candidate.get('closed_quotes') or 0):,} of "
             f"{int(candidate.get('eligible_quotes') or 0):,} quotes."),
            f"Needs {PROOF_MIN_TARGETS} targets to qualify.",
        ),
        raw=raw_text,
    )


def lineage_card(
    proofs: list[dict], *, details: tuple[str, ...] = (), raw: str | None = None,
) -> AnswerCard:
    """Answer-first card for "Is it learning from history?".

    ``proofs`` are the per-horizon ``learning_lineage_summary`` dicts
    (keys: ``lineage_proven``, ``historically_seeded``). ``details`` is
    one short sentence per horizon; ``raw`` is the original per-horizon
    training lines, which keep internal ids.
    """
    seen = [p for p in proofs if p.get("lineage_proven")]
    if not seen:
        return AnswerCard(
            question="Is it learning from history?",
            verdict="Can't tell",
            answer="Can't tell — no training history checked yet.",
            reason="No recent prediction has a verified training record.",
            next_step="Needs predictions with a recorded training lineage.",
            details=details,
            raw=raw,
        )
    seeded = sum(int(p["historically_seeded"]) for p in seen)
    total = sum(int(p["lineage_proven"]) for p in seen)
    if not seeded:
        return AnswerCard(
            question="Is it learning from history?",
            verdict="No",
            answer="No — the main model learns from live candles only.",
            reason=(
                "Stored history is used only by separate test models, "
                "and none has passed its test yet."
            ),
            next_step="Needs a test model to pass proof first.",
            details=details,
            raw=raw,
        )
    if seeded == total:
        return AnswerCard(
            question="Is it learning from history?",
            verdict="Yes",
            answer="Yes — every recent prediction used history.",
            reason=(
                f"{seeded} of the last {total} predictions started from "
                "a verified 6-year history model."
            ),
            next_step="Next: check whether history-seeded calls score better.",
            details=details,
            raw=raw,
        )
    return AnswerCard(
        question="Is it learning from history?",
        verdict="Yes",
        answer=f"Yes — partly, {seeded} of {total} predictions used history.",
        reason=(
            f"{seeded} of the last {total} predictions started from "
            "a verified 6-year history model."
        ),
        next_step="Next: check whether history-seeded calls score better.",
        details=details,
        raw=raw,
    )


def boosted_card(live_proof: dict, *, raw: str) -> AnswerCard:
    """Answer-first card for "Are 6-year history models ready?".

    ``live_proof`` is ``boosted_production_evidence``; ``raw`` is the
    original proof text (internal ids stay under "Raw numbers:").
    """
    imported = int(live_proof.get("portable_imported") or 0)
    screened = int(live_proof.get("portable_screened") or 0)
    paper = len(live_proof.get("fresh_paper_promotions") or [])
    blockers = live_proof.get("screening_blockers") or []
    reasons = {
        "fewer_than_six_source_months": "not enough verified historical months",
        "insufficient_purged_validation_or_holdout": "not enough independent test examples",
        "validation_brier_gain_below_0.005": "did not improve enough in validation",
        "holdout_brier_gain_below_0.005": "not better on unseen tests",
        "calibration_or_evaluation_days_below_gate": "not enough days of reliable tests",
        "reason_unrecorded": "reason not recorded",
    }
    blocker_text = (
        "; ".join(
            f"{int(row['count'])} {reasons.get(row['reason'], 'failed another check')}"
            for row in blockers[:2]
        )
        or "none recorded"
    )
    if paper:
        answer = "Not yet — test models qualify for practice only."
        reason = (
            f"{screened} of {imported} passed screening, "
            f"{paper} qualify for practice trades."
        )
        details = (
            f"{int(live_proof.get('issued') or 0):,} forecasts logged in 30 days.",
            f"{int(live_proof.get('settled') or 0):,} graded in 30 days.",
        )
    else:
        answer = "Not yet — no 6-year history model is ready."
        reason = f"{screened} of {imported} passed screening."
        details = (
            f"Holding them back: {blocker_text}.",
            f"{int(live_proof.get('issued') or 0):,} forecasts logged in 30 days.",
            f"{int(live_proof.get('settled') or 0):,} graded in 30 days.",
        )
    return AnswerCard(
        question="Are 6-year history models ready?",
        verdict="Not yet",
        answer=answer,
        reason=reason,
        next_step="Needs a promoted test model with verified real fills.",
        details=details,
        raw=raw,
    )


def readiness_card(readiness: dict, *, raw: str | None = None) -> AnswerCard:
    """Answer-first card for "Can RivaL predict yet?".

    ``readiness`` is ``forecast_readiness()``. The raw per-exchange
    explanation stays behind the venue select (it is the detail layer).
    """
    ready = int(readiness.get("eligible") or 0)
    configured = int(readiness.get("configured") or 0)
    streak = int(readiness.get("best_live_streak") or 0)
    needed = int(readiness.get("min_live_bars") or 0)
    if configured and ready >= configured:
        return AnswerCard(
            question="Can RivaL predict yet?",
            verdict="Yes",
            answer="Yes — every exchange can predict.",
            reason=(
                f"{ready} of {configured} exchange streams are eligible "
                "for test-model evaluation."
            ),
            next_step="Next: check the graded results on this page.",
            details=(
                f"Longest genuine live streak: {streak} of {needed} bars.",
                (f"Best 5m causal samples: "
                 f"{int(readiness.get('best_causal_5m') or 0)}, evaluated "
                 f"{int(readiness.get('best_evaluated_5m') or 0)}."),
            ),
            raw=raw,
        )
    return AnswerCard(
        question="Can RivaL predict yet?",
        verdict="Not yet",
        answer=f"Not yet — {ready} of {configured} exchanges can predict.",
        reason="Collecting is not training: RivaL needs continuous live candles.",
        next_step=(
            f"Needs {needed} live bars; longest streak is {streak}."
        ),
        details=(
            (f"Best 5m causal samples: "
             f"{int(readiness.get('best_causal_5m') or 0)}, evaluated "
             f"{int(readiness.get('best_evaluated_5m') or 0)}."),
            f"Stale or blocked: {int(readiness.get('stale_or_blocked') or 0)}.",
        ),
        raw=raw,
    )


def engine_card(
    *, state: str, age_minutes: int | None, issued: int = 0, settled: int = 0,
    raw: str,
) -> AnswerCard:
    """Answer-first card for "Is the engine running?".

    ``state`` is one of "running", "stale", "failed", "unknown".
    ``raw`` is the original heartbeat text (stage detail lives there).
    """
    if state == "running":
        return AnswerCard(
            question="Is the engine running?",
            verdict="Yes",
            answer="Yes — the learning loop is running.",
            reason=f"Last run {age_minutes} min ago; every stage passed.",
            next_step="Next run is due every 5 minutes.",
            details=(f"Made {issued:,} predictions, checked {settled:,} results.",),
            raw=raw,
        )
    if state == "failed":
        return AnswerCard(
            question="Is the engine running?",
            verdict="No",
            answer="No — a step failed on the last run.",
            reason=f"Last run {age_minutes} min ago with a failed stage.",
            next_step="Needs a clean run with every stage passing.",
            details=(f"Made {issued:,} predictions, checked {settled:,} results.",),
            raw=raw,
        )
    if state == "stale":
        return AnswerCard(
            question="Is the engine running?",
            verdict="No",
            answer="No — the learning loop has stalled.",
            reason=f"Last run {age_minutes} min ago, past the 12-minute limit.",
            next_step="Needs a fresh run within 12 minutes.",
            details=(f"Made {issued:,} predictions, checked {settled:,} results.",),
            raw=raw,
        )
    return AnswerCard(
        question="Is the engine running?",
        verdict="Can't tell",
        answer="Can't tell — no learning run recorded.",
        reason="No automatic learning cycle has been recorded yet.",
        next_step="Needs one recorded cycle to judge.",
        raw=raw,
    )


def costs_card(*, raw: str) -> AnswerCard:
    """Answer-first card for "Profit after trading fees".

    Fixed "Not yet": there are no authenticated matched fill receipts,
    so nothing here can ever read as a pass. The stress scenarios stay
    as details; the full text stays under "Raw numbers:".
    """
    return AnswerCard(
        question="Profit after trading fees?",
        verdict="Not yet",
        answer="Not yet — profit after fees is not verified.",
        reason="No matched entry and exit fill receipts exist.",
        next_step="Needs reconciled real fills, fees, funding and slippage.",
        details=(
            "Cost stress uses 16, 40 and 80 bps round trips.",
        ),
        raw=raw,
    )


def polish_embed(embed, *, section: str):
    """Apply the name table + single footer to a finished render (rule 5/6).

    Every field name and value is renamed (the raw block is untouched),
    the footer becomes the one disclaimer, and the "Live market feeds"
    field is kept only on Running details (the old overview opens Models
    on trial since the Nov 11 cleanup) or when a feed is behind
    (``live/configured`` in its value). Works on any object with the
    ``discord.Embed`` field/footer API; returns the same embed.
    """
    kept: list[tuple[str, str, bool]] = []
    for field in embed.fields:
        name = rename_text(field.name)
        value = rename_text(field.value)
        if "Live collection status" in field.name:
            name = "Live market feeds"
            if section not in ("overview", "running"):
                match = re.search(r"\*\*(\d+)/(\d+)\*\*", value)
                if match and int(match.group(1)) >= int(match.group(2)):
                    continue  # feeds fine and this is not the overview: not news
        kept.append((name, value, field.inline))
    embed.clear_fields()
    for name, value, inline in kept:
        embed.add_field(name=name, value=value, inline=inline)
    if getattr(embed, "description", None):
        embed.description = rename_text(embed.description)
    embed.set_footer(text=RESEARCH_FOOTER)
    return embed
