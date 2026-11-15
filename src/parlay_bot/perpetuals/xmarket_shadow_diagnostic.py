"""Bounded, read-only reason codes for imported XMarket models with zero shadows.

Never trains, predicts, rewrites a forecast, imports a file, or creates tables.
Only observes model eligibility, actually issued incumbent targets, causal
Polymarket/Kalshi native history, and eligible dispersion venues.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.config import Settings
from parlay_bot.perpetuals.markets import asset_for_contract
from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor
from parlay_bot.research import xmarket_training
from parlay_bot.research.xmarket_features import sampled_bars
from parlay_bot.storage.hot import PostgresHotStore

MARKER = "RIVAL_XMARKET_SHADOW_DIAGNOSTIC="
SUPPORTED = ("BTC", "ETH", "SOL", "XRP")
MAX_SCOPES = 12
RECENT_TARGETS = 12


def read_probe(store, now: datetime) -> dict:
    """No migrations, inference, or mutations. Do not disclose model documents."""
    clock = now.astimezone(UTC)
    since = (clock - timedelta(hours=2)).isoformat()
    native_start = (clock - timedelta(hours=4)).isoformat()
    with store.connect() as conn:
        fits = conn.execute(
            "SELECT asset,horizon_minutes,document FROM rival_xmarket_models "
            "WHERE gates_passed=1 ORDER BY trained_through DESC,imported_at DESC LIMIT ?",
            (MAX_SCOPES,),
        ).fetchall()
    try:
        eligible = ShadowLearningMonitor(store)._eligible_streams(now=clock, limit=40)
        present = {(venue, asset_for_contract(contract))
                   for venue, contract, _quote in eligible}
    except Exception:  # noqa: BLE001 - optional source census must report unknown
        present = None
    samples_by_asset = {}
    report = []
    seen = set()
    for fit in fits:
        asset, horizon = str(fit["asset"]), int(fit["horizon_minutes"])
        if asset not in SUPPORTED or horizon not in (5, 15, 60):
            continue
        if (asset, horizon) in seen:
            continue
        seen.add((asset, horizon))
        try:
            doc = json.loads(fit["document"])
            usable = xmarket_training.usable(doc, now=clock)
            required = tuple(doc.get("dispersion_venues", ()))
            fidelity = int(doc["pm_fidelity_minutes"])
        except (ValueError, KeyError, TypeError, OverflowError):
            usable, required, fidelity = False, (), 1
        if asset not in samples_by_asset:
            with store.connect() as conn:
                raw = conn.execute(
                    "SELECT sample_json FROM rival_xmarket_observations "
                    "WHERE asset=? AND at>=? AND at<=? AND observed_at<=? "
                    "ORDER BY at LIMIT 600",
                    (asset, native_start, clock.isoformat(), clock.isoformat()),
                ).fetchall()
            samples_by_asset[asset] = [json.loads(r["sample_json"]) for r in raw]
        with store.connect() as conn:
            incumbents = conn.execute(
                "SELECT reference_opened_at,issued_at FROM perp_forward_audit "
                "WHERE asset=? AND horizon_minutes=? AND issued_at>=? "
                "ORDER BY issued_at DESC LIMIT ?",
                (asset, horizon, since, RECENT_TARGETS),
            ).fetchall()
        counts = {"pm_current": 0, "pm_15m_ago": 0, "pm_60m_ago": 0,
                  "kalshi_current": 0, "native_complete": 0}
        conflict = False
        for row in incumbents:
            try:
                issued_at = datetime.fromisoformat(row["issued_at"]).astimezone(UTC)
                target_bar = (datetime.fromisoformat(
                    row["reference_opened_at"]).astimezone(UTC) + timedelta(minutes=5))
                bars = sampled_bars(
                    samples_by_asset[asset], asset=asset,
                    as_of=issued_at, pm_fidelity=fidelity,
                )
            except (ValueError, KeyError, TypeError):
                conflict = True
                continue
            pm, kalshi = bars["polymarket"], bars["kalshi"]
            parts = (target_bar in pm,
                     target_bar - timedelta(minutes=15) in pm,
                     target_bar - timedelta(minutes=60) in pm,
                     target_bar in kalshi)
            for name, ok in zip(
                ("pm_current", "pm_15m_ago", "pm_60m_ago", "kalshi_current"), parts
            ):
                counts[name] += int(ok)
            counts["native_complete"] += int(all(parts))
        missing_venues = (
            sorted(v for v in required if (v, asset) not in present)
            if present is not None else None
        )
        status = (
            "stored_artifact_not_usable" if not usable else
            "no_recent_incumbent" if not incumbents else
            "native_history_conflict" if conflict and not counts["native_complete"] else
            "missing_exact_native_pair_at_issuance" if not counts["native_complete"] else
            "required_dispersion_venue_absent" if missing_venues else
            "needs_candle_window_proof"
        )
        report.append({
            "asset": asset, "horizon_minutes": horizon, "status": status,
            "stored_artifact_usable": usable, "recent_incumbent_rows": len(incumbents),
            "native_at_issuance": counts,
            "required_dispersion_venue_count": len(required),
            "missing_dispersion_venues": missing_venues,
            "source_census_available": present is not None,
            "does_not_prove_shadow_issuance": True,
            "does_not_prove_model_edge": True,
        })
    return {"status": "recorded", "scopes": report, "evaluated_models": len(report),
            "does_not_prove_prospective_predictions": True,
            "read_only": True}


def main() -> int:
    try:
        settings = Settings()
        if settings.rival_storage_backend != "postgres":
            raise ValueError("production probe requires postgres")
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
        try:
            result = read_probe(store, datetime.now(UTC))
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - never echo SQL/DSN/sample content
        result = {"status": "read_unavailable", "error_class": type(exc).__name__,
                  "does_not_prove_prospective_predictions": True, "read_only": True}
    print(MARKER + json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "recorded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
