"""One-time private LFM3 inference to PostgreSQL acceptance.

Checks an authenticated local model result and its persisted original evidence.
It does not consume third-party credits, enable background AI or modify forecasts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.config import Settings
from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS
from parlay_bot.perpetuals.ai_types import ResearchUnavailable
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.local_first_research import (
    LOCAL_MODEL,
    LocalCapacityDeferred,
    LocalFirstResearch,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
from parlay_bot.storage.hot import PostgresHotStore

MARKER = "RIVAL_LFM3_PERSISTENCE_RECEIPT="


def verified_receipt(result, row: dict | None, *, now: datetime,
                     asset: str = "BTC") -> bool:
    """Only exact, fresh production database rows qualify as a receipt."""
    if row is None:
        return False
    try:
        issued = datetime.fromisoformat(str(row["issued_at"]))
        age = now - issued.astimezone(UTC)
        return (
            timedelta(0) <= age <= timedelta(minutes=10)
            and result.asset == row["asset"] == asset
            and result.model == row["model"] == LOCAL_MODEL
            and result.evidence_sha256 == row["evidence_sha256"]
            and len(result.evidence_sha256) == 64
            and int(row["source_count"]) >= 3
            and result.source_count == int(row["source_count"])
            and result.direction == row["direction"] == "uncertain"
            and result.confidence == row["confidence"] == "low"
            and bool(row["thesis"]) and bool(row["risk"])
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


async def _ask(market: PerpetualMarketStore, settings: Settings,
             *, asset: str = "BTC"):
    ai = LocalFirstResearch(
        market,
        local_key=settings.rival_local_ai_token.get_secret_value(),
        daily_max=settings.rival_llm_daily_max,
    )
    try:
        started = time.monotonic()
        result = await ai.ask(
            asset=asset, venues=tuple(MARKET_SYMBOLS), busy_retry=True,
        )
        return result, round(time.monotonic() - started, 2)
    finally:
        await ai.aclose()


def _research_market(store: PostgresHotStore) -> PerpetualMarketStore:
    """Use one production-grade market/evidence wiring in both asset probes."""
    market = PerpetualMarketStore(store)
    market.research_candidate_registry = ResearchCandidateRegistry(store)
    market.research_history_auditor = SixMonthQualityAudit(market)
    market.research_forward_auditor = ForwardAccuracyMonitor(market)
    return market


def _verify_xrp_stage(market: PerpetualMarketStore, settings: Settings,
                      store: PostgresHotStore, report: dict,
                      *, standalone: bool) -> dict:
    """One source-verified XRP ask and exact persisted PostgreSQL readback."""
    with store.connect() as conn:
        before = int(conn.execute(
            "SELECT COUNT(*) AS n FROM perp_ai_research WHERE model=?",
            (LOCAL_MODEL,),
        ).fetchone()["n"])
    try:
        result, elapsed = asyncio.run(_ask(market, settings, asset="XRP"))
    except LocalCapacityDeferred as exc:
        return {**report, "outcome": "deferred", "reason": "minimum_spacing",
                "retry_at": exc.retry_at.isoformat() if exc.retry_at else None}
    except ResearchUnavailable:
        return {**report, "outcome": "failed",
                "reason": "xrp_research_unavailable"}
    except Exception as exc:  # noqa: BLE001 - safe exception class only
        return {**report, "outcome": "failed",
                "reason": "xrp_research_exception",
                "error_class": type(exc).__name__}
    row = market.cached_ai_research(
        asset=result.asset, model=result.model,
        evidence_sha256=result.evidence_sha256,
    )
    with store.connect() as conn:
        after = int(conn.execute(
            "SELECT COUNT(*) AS n FROM perp_ai_research WHERE model=?",
            (LOCAL_MODEL,),
        ).fetchone()["n"])
    report["xrp_seconds"] = elapsed
    report["xrp_from_cache"] = bool(result.from_cache)
    report["xrp_verified"] = bool(
        verified_receipt(result, row, now=datetime.now(UTC), asset="XRP")
        and (after > before or result.from_cache)
    )
    if standalone:
        report.update({
            "model": result.model, "source_count": result.source_count,
            "from_cache": bool(result.from_cache), "saved_before": before,
            "saved_after": after,
        })
    report["outcome"] = "verified" if report["xrp_verified"] else "failed"
    if not report["xrp_verified"]:
        report["reason"] = "xrp_exact_database_receipt_missing"
    return report


def verify_once(*, include_xrp: bool = True) -> dict:
    settings = Settings()
    report: dict = {"outcome": "deferred", "provider": settings.rival_ai_provider}
    if settings.rival_storage_backend != "postgres" or settings.rival_database_url is None:
        return {"outcome": "failed", "reason": "postgres_not_configured"}
    if settings.rival_ai_provider == "disabled" or settings.rival_local_ai_token is None:
        return {"outcome": "deferred", "reason": "local_provider_not_configured"}

    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    try:
        # Same production evidence readers as the application.
        market = _research_market(store)
        report["inference_provider"] = "private_lfm3_only"
        with store.connect() as conn:
            report["saved_before"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM perp_ai_research WHERE model=?",
                (LOCAL_MODEL,),
            ).fetchone()["n"])
        try:
            evidence, digest = build_evidence(
                market, asset="BTC", venues=tuple(MARKET_SYMBOLS),
                horizon_minutes=5,
            )
        except InsufficientEvidence:
            report["reason"] = "insufficient_live_venues"
            return report
        report["healthy_venues"] = len(evidence["sources"])
        report["mathematical_evidence_status"] = (
            evidence.get("mathematical_research", {}).get("status", "unavailable")
        )
        report["fresh_six_month_source_audits"] = sum(
            x.get("status") in ("complete", "incomplete")
            for x in evidence.get("historical_quality", [])
        )
        report["cold_archive_readback"] = evidence.get("cold_archive_readback")
        report["prospective_math_status"] = evidence.get("prospective_math", {}).get("status")
        report["evidence_hash_valid"] = len(digest) == 64

        # Private inference is CPU-serialized by the gateway, not quota-gated.
        try:
            result, elapsed = asyncio.run(_ask(market, settings))
        except LocalCapacityDeferred as exc:
            return {**report, "outcome": "deferred", "reason": "minimum_spacing",
                    "retry_at": exc.retry_at.isoformat() if exc.retry_at else None}
        except ResearchUnavailable as exc:
            return {**report, "outcome": "failed",
                    "reason": "ordinary_research_unavailable",
                    "details": str(exc)[:160]}
        except Exception as exc:  # noqa: BLE001 - host receipt fails closed
            return {**report, "outcome": "failed",
                    "reason": "ordinary_research_exception",
                    "error_class": type(exc).__name__}

        row = market.cached_ai_research(
            asset=result.asset, model=result.model,
            evidence_sha256=result.evidence_sha256,
        )
        with store.connect() as conn:
            report["saved_after"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM perp_ai_research WHERE model=?",
                (LOCAL_MODEL,),
            ).fetchone()["n"])
        report["seconds"] = elapsed
        report["model"] = result.model
        report["source_count"] = result.source_count
        report["from_cache"] = bool(result.from_cache)
        is_saved = verified_receipt(result, row, now=datetime.now(UTC))
        new_or_existing = (
            report["saved_after"] > report["saved_before"] or result.from_cache
        )
        if not (is_saved and new_or_existing):
            report["outcome"] = "failed"
            report["reason"] = "exact_database_receipt_missing"
            return report
        # The off-host acceptance checks the same BTC and XRP receipts in
        # separate console requests: two CPU inferences in one synchronous
        # request can exceed the Discloud gateway deadline. The default
        # combined path stays available for backward-compatible diagnostics.
        if not include_xrp:
            report["asset"] = "BTC"
            report["outcome"] = "verified"
            return report
        # XRP remains mandatory in legacy combined acceptance. Both modes
        # share a single persisted-source proof instead of diverging logic.
        return _verify_xrp_stage(market, settings, store, report, standalone=False)
    finally:
        store.close()



def verify_xrp_only() -> dict:
    """Independently prove XRP LFM3 persistence, without a BTC inference.

    The off-host acceptance relay pairs this result with its own BTC proof.
    The same verified XRP writer/reader is used by the legacy combined mode.
    """
    settings = Settings()
    report: dict = {"outcome": "deferred", "asset": "XRP",
                    "provider": settings.rival_ai_provider}
    if settings.rival_storage_backend != "postgres" or settings.rival_database_url is None:
        return {**report, "outcome": "failed", "reason": "postgres_not_configured"}
    if settings.rival_ai_provider == "disabled" or settings.rival_local_ai_token is None:
        return {**report, "reason": "local_provider_not_configured"}
    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    try:
        return _verify_xrp_stage(
            _research_market(store), settings, store, report, standalone=True,
        )
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private LFM3 source-exact acceptance")
    parser.add_argument("--asset", choices=("both", "BTC", "XRP"), default="both")
    args = parser.parse_args(argv)
    try:
        report = (verify_xrp_only() if args.asset == "XRP" else
                  verify_once(include_xrp=args.asset == "both"))
    except Exception as exc:  # noqa: BLE001 - redact host errors
        report = {"outcome": "failed", "reason": "unverified_host_exception",
                  "error_class": type(exc).__name__}
    print(MARKER + json.dumps(report, sort_keys=True))
    return 1 if report["outcome"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
