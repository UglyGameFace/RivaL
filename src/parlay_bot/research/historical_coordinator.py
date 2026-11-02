"""Source-scoped historical acquisition controller.

The warehouse census contains manifest NAMES, not certified bytes. This
controller coordinates existing collectors; it cannot declare a month
complete, infer a listing date or approve a trading model from those names.
No external provider request is performed by this module.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.research.history_priority import window
from parlay_bot.research.source_family_backlog import build_backlog, family_for
from parlay_bot.research.source_family_coverage import ASSETS, PERP_FAMILIES, VENUES

# Publication-era / downloader attempt floors. These are NOT contract listing
# dates. A coin can be listed later, or older source data may be discoverable.
ATTEMPT_FLOORS: dict[tuple[str, str], str] = {
    ("binance", "candles_5m"): "2021-01",
    ("bitget", "candles_5m"): "2024-11",
    ("bitget", "mark"): "2024-11",
    ("bitget", "index"): "2024-11",
    ("bitget", "premium"): "2024-11",
    ("bitget", "trades"): "2024-12",  # Tardis public DAY sample only
    ("bitget", "orderbook_l2"): "2024-12",  # Tardis public DAY sample only
    ("deribit", "trades"): "2021-01",
    ("dydx", "funding"): "2023-11",  # v4 only
    ("gate", "candles_1m"): "2023-01",
    ("gate", "candles_5m"): "2023-01",
    ("gate", "candles_1h"): "2023-01",
    ("gate", "trades"): "2023-01",
    ("gate", "funding"): "2023-01",
    ("gate", "mark"): "2023-01",
    ("gate", "orderbook_l2"): "2021-08",  # hourly samples, not complete L2
    ("lighter", "funding"): "2021-01",  # exploratory; no listing proof
    ("lighter", "mark"): "2021-01",
    ("okx", "candles_1m"): "2023-07",
    ("okx", "candles_5m"): "2023-07",
    ("okx", "trades"): "2021-09",
    ("okx", "funding"): "2022-03",
    ("okx", "orderbook_l2"): "2023-03",
}
# No fallback of one venue's launch date to any other venue/asset/family.
# The source's earliest REAL contract listing is unknown until separately proven.
ROUTES: dict[tuple[str, str], tuple[str, str]] = {
    ("binance", "candles_5m"): ("binance-deep-file-backfill.yml", "scheduled_bulk"),
    **{("binance", family): ("binance-original-auxiliary.yml", "official_checksum_zip")
       for family in ("mark", "index", "premium", "candles_1m",
                      "candles_15m", "candles_1h", "candles_4h", "candles_1d")},
    ("binance", "open_interest"): ("historical-source-provenance.yml", "official_daily_5m_metrics_partial"),
    ("binance", "positioning"): ("historical-source-provenance.yml", "official_daily_5m_metrics_partial"),
    ("bitget", "open_interest"): ("bitget-liquidity-archive.yml", "live_history_pages_not_complete"),
    ("bitget", "liquidations"): ("bitget-liquidity-archive.yml", "recent_history_pages_not_complete"),
    ("bitget", "orderbook_l2"): ("bitget-tardis-source-archive.yml", "sample_days_only"),
    **{("bitget", f): ("bitget-v3-overnight-archive.yml", "scheduled_bulk")
       for f in ("candles_5m", "mark", "index", "premium")},
    ("bitget", "funding"): ("bitget-liquidity-archive.yml", "scheduled_pages"),
    ("bitget", "trades"): ("bitget-tardis-source-archive.yml", "sample_days_only"),
    ("deribit", "trades"): ("deribit-hourly-native-backfill.yml", "hourly_chunks_only"),
    ("dydx", "funding"): ("dydx-v4-original-funding.yml", "scheduled_pages"),
    ("lighter", "funding"): ("lighter-original-auxiliary.yml", "scheduled_pages"),
    ("lighter", "mark"): ("lighter-original-auxiliary.yml", "scheduled_pages"),
    ("gate", "orderbook_l2"): ("gate-hourly-l2-source.yml", "sample_hours_only"),
    **{("gate", f): ("gate-official-archive-backfill.yml", "scheduled_bulk")
       for f in ("candles_1m", "candles_5m", "candles_1h",
                 "trades", "funding", "mark")},
    ("okx", "candles_5m"): ("official-okx-bulk-bootstrap.yml", "scheduled_bulk"),
    ("okx", "candles_1m"): ("official-okx-bulk-bootstrap.yml", "scheduled_bulk"),
    **{("okx", f): ("official-okx-advanced-archive.yml", "scheduled_pages")
       for f in ("trades", "funding", "orderbook_l2")},
    **{(venue, "candles_5m"): ("discloud_live_history_recovery", "live_six_month_loop")
       for venue in ("hyperliquid", "kucoin", "phemex", "dydx",
                     "deribit", "lighter")},
}
SOURCE_CAPABILITIES = {
    "orderbook_l2": "Original snapshots/deltas need independent sequence proof",
    "trades": "Sampled source days/hours are not a continuous trade tape",
    "liquidations": "No universal historic liquidation source is configured",
    "open_interest": "No universal historic open-interest source is configured",
}


def _month_after(month: str) -> str:
    year, m = (int(n) for n in month.split("-"))
    return f"{year + (m == 12):04d}-{m % 12 + 1:02d}"


def completed_range(first: str, current: str) -> list[str]:
    """Completed UTC months only. Unknown listings are NOT excluded."""
    if first < "2021-01" or first >= current:
        return []
    result = []
    month = first
    while month < current:
        result.append(month)
        month = _month_after(month)
    return result


def candidate_training_window(row: dict, *, current: str,
                              all_months: set[str] | None = None,
                              rotation: int = 0) -> str | None:
    """A six-month NAME candidate only; research must verify all raw bytes."""
    if row["family"] != "candles_5m":
        return None
    names = set(all_months if all_months is not None else
                row["priority_window_named_months_not_reverified"])
    # Technical API attempt floors are NOT listing dates. An older archived
    # source month may come from a different genuine acquisition method.
    # Never reject it without letting the exact-byte importer audit it.
    eligible = []
    for first in sorted(names, reverse=True):
        if first < "2021-01":
            continue
        seq = [first]
        for _ in range(5):
            seq.append(_month_after(seq[-1]))
        if seq[-1] < current and all(m in names for m in seq):
            eligible.append(first)
    return eligible[rotation % len(eligible)] if eligible else None


def build_plan(census: dict, *, max_jobs: int = 2,
               listing_report: dict | None = None) -> dict:
    if not 1 <= max_jobs <= 4:
        raise ValueError("bounded dispatcher limit required")
    backlog = build_backlog(census)
    instant = datetime.fromisoformat(census["generated_at"])
    if instant.utcoffset() is None:
        raise ValueError("census clock must have an offset")
    now = instant.astimezone(UTC)
    current = now.strftime("%Y-%m")
    priority = window(now)
    all_hints: dict[tuple[str, str, str], set[str]] = {}
    for hint in census.get("all_completed_scope_name_hints", []):
        venue, asset = hint.get("venue"), hint.get("asset")
        family = family_for(venue, hint.get("family"))
        if family is None:
            continue  # Independently unclassified sources are never candle proof.
        key = (venue, asset, family)
        listed = hint.get("listed_completed_utc_months")
        if (key[0] not in VENUES or key[1] not in ASSETS
            or key[2] not in PERP_FAMILIES or not isinstance(listed, list)
            or len(listed) > 120 or len(set(listed)) != len(listed)
            or any(not isinstance(m, str) or len(m) != 7
                   or m < "2021-01" or m >= current or
                   m[4] != "-" or m[5:] not in
                   {f"{n:02d}" for n in range(1, 13)} for m in listed)):
            raise ValueError("invalid independent source-month name inventory")
        all_hints.setdefault(key, set()).update(listed)
    listing_by_instrument: dict[tuple[str, str], tuple[str, str]] = {}
    if listing_report is not None:
        if (listing_report.get("schema_version") !=
            "rival-official-perpetual-listings-v1"
            or listing_report.get("all_listing_dates_verified") is not False):
            raise ValueError("untrusted official listing report")
        observed_at = datetime.fromisoformat(listing_report["observed_at"])
        if (observed_at.utcoffset() is None or observed_at > now + timedelta(minutes=5)
            or now - observed_at > timedelta(days=7)):
            raise ValueError("stale or future official contract metadata")
        for item in listing_report["assets"]:
            venue, asset = item["venue"], item["asset"]
            if venue not in VENUES or asset not in ASSETS:
                raise ValueError("wrong official listing identity")
            ident = (venue, asset)
            if ident in listing_by_instrument:
                raise ValueError("duplicate official listing identity")
            stamp = item.get("contract_trading_start_utc")
            if stamp:
                if venue not in {"binance", "okx"} or (
                    item.get("evidence_state") != "official_contract_start_timestamp"
                    or item.get("published_field") !=
                    ("onboardDate" if venue == "binance" else "listTime")
                ):
                    raise ValueError("listing inferred from unapproved source")
                opened = datetime.fromisoformat(stamp)
                if opened.utcoffset() is None or opened > now:
                    raise ValueError("untrusted official listing clock")
                listing_by_instrument[ident] = (
                    opened.astimezone(UTC).strftime("%Y-%m"), stamp,
                )
            else:
                listing_by_instrument[ident] = ("", "")
    rows = []
    for entry in backlog["scopes"]:
        venue, asset, family = (entry[k] for k in ("venue", "asset", "family"))
        route, route_type = ROUTES.get(
            (venue, family), (None, "collector_not_yet_integrated"))
        attempt_floor = ATTEMPT_FLOORS.get((venue, family))
        named = entry["priority_window_named_months_not_reverified"]
        listing = listing_by_instrument.get((venue, asset))
        listed_month = listing[0] if listing and listing[0] else None
        source_days = [
            r for r in census.get("binance_daily_metrics_not_full_months", [])
            if venue == "binance" and r.get("asset") == asset
            and family in r.get("family", [])
        ]
        rows.append({
            "venue": venue, "asset": asset, "family": family,
            "original_daily_source_name_hints_not_months": sum(
                r["distinct_named_days"] for r in source_days
            ),
            "earliest_technical_attempt_month": attempt_floor,
            "actual_contract_listing_month": listed_month,
            "source_availability_evidence": (
                "provider_current_contract_start_timestamp" if listed_month
                else "provider_availability_not_verified"),
            "official_current_contract_prelisting_months": [
                m for m in priority.completed_months
                if listed_month and m < listed_month
            ],
            "source_gaps_permanently_unavailable_from_missing_filename": False,
            "missing_route_requires_implementation": route is None,
            "contract_listing_evidence": (
                "provider_onboard_or_list_time_snapshot" if listed_month
                else "not_verified"),
            "named_months_before_reported_listing": [
                m for m in sorted(all_hints.get((venue, asset, family), set()))
                if listed_month and m < listed_month
            ],
            "reported_listing_is_archive_proof": False,
            "collector": route,
            "collector_mode": route_type,
            "name_hint_month_count": len(named),
            "sample_or_page_hint_count": entry["partial_source_page_scopes"]
                                       + entry["hourly_original_depth_hints"],
            "last_named_month": max(named) if named else None,
            "all_history_named_months_not_reverified": len(all_hints.get(
                (venue, asset, family), set())),
            "verified_complete_months_from_this_census": 0,
            "unverified_priority_months": len(priority.completed_months) - len(named),
            "earliest_attempt_is_listing_proof": False,
            "no_name_means_unavailable": False,
            "status": ("manifest_names_only_not_reverified" if named
                       else "partial_sample_only_not_months" if
                       entry["partial_source_page_scopes"]
                       or entry["hourly_original_depth_hints"]
                       else "unindexed_collection_possible" if route
                       else "no_integrated_historical_collector"),
            "limitation": SOURCE_CAPABILITIES.get(family),
        })
    # The production collectors and their schedules are preserved. One
    # lightweight scheduler fills focused gaps, never blanket-dispatches
    # the dozen already running bulk workflows every six hours.
    candidates = []
    training = []
    for row in backlog["scopes"]:
        venue, asset, family = (row[k] for k in ("venue", "asset", "family"))
        if venue == "binance" or family != "candles_5m":
            continue
        first = candidate_training_window(
            row, current=current,
            all_months=all_hints.get((venue, asset, family)),
            rotation=now.date().toordinal(),
        )
        if first:
            training.append((venue, asset, first))
    # Rotate across independent venue/coin scopes. A single well-covered
    # exchange can never monopolize source-native model research forever.
    if training:
        venue, asset, first = training[now.date().toordinal() % len(training)]
        candidates.append({
            "workflow": "source-native-research.yml",
            "inputs": {"venue": venue, "first_month": first,
                       "asset": asset if venue == "bitget" else ""},
            "reason": "six same-source manifest names; original bytes and UTC grid must still pass",
            "scope": f"{venue}/{asset}/candles_5m",
        })
    # Bitget v3 is already scheduled twice/week, but its low default request
    # budget must not monopolize a whole historic queue or erase other venues.
    # Focus a daily optional dispatch only on a genuinely unlisted priority
    # month. The collector itself checks its original source and Drive receipts.
    bitget = next(r for r in backlog["scopes"] if
                  (r["venue"], r["asset"], r["family"]) ==
                  ("bitget", "BTC", "candles_5m"))
    missing = [m for m in priority.completed_months
               if m >= "2024-11" and
               m not in bitget["priority_window_named_months_not_reverified"]]
    if missing:
        candidates.append({
            "workflow": "bitget-v3-overnight-archive.yml",
            "inputs": {"first_month": missing[0], "max_requests": "8000"},
            "reason": "bounded Bitget source-specific historical hole; never use Binance as a substitute",
            "scope": "bitget/BTC-ETH-SOL-XRP/candles-mark-index-premium",
        })
    # Only non-scheduled Deribit original hourly-trade research needs an
    # independent dispatch. This is a chunk/sample probe, not bulk completion.
    first = datetime(2021, 1, 2, tzinfo=UTC)
    hours = max(1, int((now - first).total_seconds() // 3600) - 1)
    slot = int(now.timestamp()) // (6 * 3600)
    hour = first + timedelta(hours=slot % hours)
    candidates.append({
        "workflow": "deribit-hourly-native-backfill.yml",
        "inputs": {"day": hour.strftime("%Y-%m-%d"), "hour": str(hour.hour)},
        "reason": "independent original Deribit trade hour, never 2021 coverage for other venues",
        "scope": "deribit/BTC-ETH/trades/hourly_sample",
    })
    # Keep total GH Actions burden within a bounded request budget.
    distinct = []
    seen = set()
    for task in candidates:
        if task["workflow"] not in seen:
            distinct.append(task)
            seen.add(task["workflow"])
    # Fair rotation so a permanently incomplete venue cannot starve others.
    offset = (now.date().toordinal() % len(distinct)) if distinct else 0
    selected = (distinct[offset:] + distinct[:offset])[:max_jobs]
    return {
        "schema_version": "rival-historical-coordinator-v1",
        "snapshot_utc": now.isoformat(),
        "source_census_is_name_only": True,
        "complete_from_this_report": 0,
        "original_bytes_reverified_in_this_report": False,
        "contract_listing_dates_verified_in_this_report": False,
        "live_model_admission": False,
        "venues": list(VENUES), "assets": list(ASSETS), "families": list(PERP_FAMILIES),
        "scope_count": len(rows),
        "collector_routes_integrated": sum(r["collector"] is not None for r in rows),
        "collection_routes_missing": sum(r["collector"] is None for r in rows),
        "priority": priority.metadata(),
        "scopes": rows, "dispatch_candidates": selected,
        "all_history_complete": False,
        "warning": (
            "No month, actual coin listing, full orderbook, complete trade tape "
            "or forward-model benefit is inferred from a Drive filename. "
            "Unlisted and inaccessible are different. Source attempts stay "
            "venue/asset/family-specific; collector safety budgets remain in force."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="10-venue history coordinator")
    parser.add_argument("--census", type=Path, default=Path(
        "data/native-reports/drive-source-census.json"))
    parser.add_argument("--out", type=Path, default=Path(
        "data/native-reports/historical-coordinator.json"))
    parser.add_argument("--max-jobs", type=int, default=2)
    parser.add_argument("--listing-report", type=Path, default=None)
    args = parser.parse_args()
    listing = (json.loads(args.listing_report.read_text())
               if args.listing_report is not None else None)
    report = build_plan(
        json.loads(args.census.read_text()), max_jobs=args.max_jobs,
        listing_report=listing,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "tracked_scopes": report["scope_count"],
        "automatic_collection_scopes": report["collector_routes_integrated"],
        "missing_collection_routes": report["collection_routes_missing"],
        "potential_dispatches": len(report["dispatch_candidates"]),
        "fully_verified_by_name_inventory": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
