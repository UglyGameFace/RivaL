"""Quota-respecting, one-time local LFM3 to PostgreSQL acceptance.

Run inside RivaL's already configured Discloud environment. Does not enable
background AI, change forecasts, or circumvent the shared database quota.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.config import Settings
from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.gemini_research import ResearchUnavailable
from parlay_bot.perpetuals.local_first_research import LOCAL_MODEL, LocalFirstResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore

MARKER = "RIVAL_LFM3_PERSISTENCE_RECEIPT="
QUOTA_MODEL = "rival-local-cloudflare-shared-budget"


def verified_receipt(result, row: dict | None, *, now: datetime) -> bool:
    """Only exact, fresh production database rows qualify as a receipt."""
    if row is None:
        return False
    try:
        issued = datetime.fromisoformat(str(row["issued_at"]))
        age = now - issued.astimezone(UTC)
        return (
            timedelta(0) <= age <= timedelta(minutes=10)
            and result.asset == row["asset"] == "BTC"
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


async def _ask(market: PerpetualMarketStore, settings: Settings):
    ai = LocalFirstResearch(
        market,
        local_key=settings.rival_local_ai_token.get_secret_value(),
        cloudflare_key=None,
        daily_max=settings.rival_openrouter_daily_max,
        cooldown_seconds=settings.rival_openrouter_cooldown_seconds,
    )
    try:
        started = time.monotonic()
        result = await ai.ask(asset="BTC", venues=tuple(MARKET_SYMBOLS))
        return result, round(time.monotonic() - started, 2)
    finally:
        await ai.aclose()


def verify_once() -> dict:
    settings = Settings()
    report: dict = {"outcome": "deferred", "provider": settings.rival_ai_provider}
    if settings.rival_storage_backend != "postgres" or settings.rival_database_url is None:
        return {"outcome": "failed", "reason": "postgres_not_configured"}
    if settings.rival_ai_provider != "local" or settings.rival_local_ai_token is None:
        return {"outcome": "deferred", "reason": "local_provider_not_configured"}

    store = PostgresHotStore(settings.rival_database_url.get_secret_value())
    try:
        market = PerpetualMarketStore(store)
        status = market.ai_request_status(
            model=QUOTA_MODEL, now=datetime.now(UTC),
            daily_max=settings.rival_openrouter_daily_max,
            cooldown_seconds=settings.rival_openrouter_cooldown_seconds,
        )
        report["quota"] = {
            "ready": bool(status["ready"]),
            "used": status["requests_today"],
            "reasons": list(status["reasons"]),
            "retry_at": str(status["retry_at"]) if status["retry_at"] else None,
        }
        with store.connect() as conn:
            report["saved_before"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM perp_ai_research WHERE model=?",
                (LOCAL_MODEL,),
            ).fetchone()["n"])
        if not status["ready"]:
            report["reason"] = "quota_or_cooldown"
            return report
        try:
            evidence, digest = build_evidence(
                market, asset="BTC", venues=tuple(MARKET_SYMBOLS),
                horizon_minutes=5,
            )
        except InsufficientEvidence:
            report["reason"] = "insufficient_live_venues"
            return report
        report["healthy_venues"] = len(evidence["sources"])
        report["evidence_hash_valid"] = len(digest) == 64

        # The request itself performs a SECOND atomic quota reservation. If a
        # simultaneous real user consumes the slot, this cannot bypass it.
        try:
            result, elapsed = asyncio.run(_ask(market, settings))
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
        report["outcome"] = "verified" if is_saved and new_or_existing else "failed"
        if report["outcome"] != "verified":
            report["reason"] = "exact_database_receipt_missing"
        return report
    finally:
        store.close()


def main() -> int:
    try:
        report = verify_once()
    except Exception as exc:  # noqa: BLE001 - redact host errors
        report = {"outcome": "failed", "reason": "unverified_host_exception",
                  "error_class": type(exc).__name__}
    print(MARKER + json.dumps(report, sort_keys=True))
    return 1 if report["outcome"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
