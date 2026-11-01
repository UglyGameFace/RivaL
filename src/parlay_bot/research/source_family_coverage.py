"""Conservative 2021+ source-family evidence ledger.

Enumerates every requested month even when a provider has no validated data.
This does NOT download archives, read hosted PostgreSQL, or claim that a
contract existed before listing. Original source/venue and no-call gates remain.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.research.audit import audit_partition
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import prepare

ASSETS = ("BTC", "ETH", "SOL", "XRP")
VENUES = (
    "binance", "bitget", "deribit", "dydx", "gate",
    "hyperliquid", "kucoin", "lighter", "okx", "phemex",
)
# A requirement to investigate a family is not a claim the provider publishes it.
PERP_FAMILIES = (
    "candles_1m", "candles_5m", "candles_15m", "candles_1h",
    "candles_4h", "candles_1d", "trades", "funding", "mark",
    "index", "premium", "open_interest", "positioning",
    "liquidations", "orderbook_l2", "options_volatility",
)
EVENT_VENUES = ("polymarket", "kalshi", "draftkings_predictions")
EVENT_FAMILIES = (
    "market_catalog", "contract_price_history", "historical_bid_ask_depth",
    "settlement_oracle", "official_settlements", "fee_schedule",
)
CONTEXT_FAMILIES = {
    "macroeconomic": ("calendar", "consensus", "actuals", "revisions"),
    "onchain": ("exchange_flows", "stablecoin_supply", "network_activity"),
    "institutional": ("etf_flows",),
    "cross_asset": ("equities", "rates", "dollar", "commodities"),
}
INTERVAL_NAMES = {1: "candles_1m", 5: "candles_5m", 15: "candles_15m",
                  60: "candles_1h", 240: "candles_4h", 1440: "candles_1d"}
BINANCE_SOURCE_FAMILIES = {
    "binance_um_funding": "funding",
    "binance_um_mark_klines": "mark",
    "binance_um_index_klines": "index",
    "binance_um_premium_klines": "premium",
    "binance_um_daily_metrics": "positioning",
}
MONTH_RE = re.compile(r"(?<!\d)(20\d{2}-(?:0[1-9]|1[0-2]))(?!\d)")
MAX_MANIFESTS = 20000
MAX_MANIFEST_BYTES = 32768


def _month(value: str) -> int:
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", value):
        raise ValueError("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    return year * 12 + month - 1


def months(first: str, through: str) -> list[str]:
    start, end = _month(first), _month(through)
    if start < _month("2021-01") or end < start or end - start > 120:
        raise ValueError("only bounded complete months since 2021 are supported")
    return [f"{divmod(i, 12)[0]:04d}-{divmod(i, 12)[1] + 1:02d}"
            for i in range(start, end + 1)]


def last_completed_month(now: datetime) -> str:
    if now.utcoffset() is None:
        raise ValueError("timezone-aware UTC clock required")
    instant = now.astimezone(UTC)
    index = instant.year * 12 + instant.month - 2
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def _asset_from_name(path: Path) -> str | None:
    found = re.match(r"(BTC|ETH|SOL|XRP)USDT-", path.name)
    return found.group(1) if found else None


def _asset_from_contract(contract: object) -> str | None:
    if not isinstance(contract, str):
        return None
    for asset in ASSETS:
        if contract.startswith(asset) or (asset == "BTC" and contract.startswith("XBT")):
            return asset
    return None


def _local_evidence(path: Path) -> tuple[tuple[str, str, str, str], str, str] | None:
    """Accept local checksum-valid evidence only; remote Drive proof is separate.

    The existing source-specific auditors are authoritative. A generic
    manifest, file name, provider HTTP 200, or local source SHA does not prove
    official publication, full-month continuity, or live availability.
    """
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise HistoryImportError("oversized research manifest")
    meta = json.loads(path.read_text(encoding="utf-8"))
    source = meta.get("source")
    if source in ("binance_um_klines", "verified_venue_perp_klines"):
        audit = audit_partition(path)  # checks actual normalized bytes and UTC grid
        asset = _asset_from_contract(audit["contract"])
        venue = audit["venue"]
        family = INTERVAL_NAMES.get(audit["interval_minutes"])
        if (asset not in ASSETS or venue not in VENUES or family is None
            or (source == "binance_um_klines" and venue != "binance")
            or (source == "verified_venue_perp_klines" and venue == "binance")):
            raise HistoryImportError("invalid venue/contract/interval source identity")
        status = ("local_full_calendar_checked_not_drive_reverified"
                  if audit["full_calendar_month_verified"]
                  else "local_incomplete_calendar")
        return (venue, asset, family, audit["month"]), status, audit["data_sha256"]
    if source not in BINANCE_SOURCE_FAMILIES:
        return None
    # prepare checks source type, identity, local data SHA, count and path safety.
    prepare(path)
    asset = _asset_from_name(path)
    found = MONTH_RE.findall(path.name)
    if asset is None or len(found) != 1 or meta.get("checksum_verified") is not True:
        raise HistoryImportError("supplemental month or official-checksum flag missing")
    return (
        ("binance", asset, BINANCE_SOURCE_FAMILIES[source], found[0]),
        "local_integrity_only_month_continuity_not_qualified",
        meta["data_sha256"],
    )


def scan_local(roots: list[Path]) -> tuple[dict, list[dict], int]:
    found: dict[tuple[str, str, str, str], list[tuple[str, str]]] = defaultdict(list)
    invalid = []
    checked = 0
    for root in roots:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("local evidence root must be a directory")
        for path in root.rglob("*.manifest.json"):
            checked += 1
            if checked > MAX_MANIFESTS:
                raise ValueError("research manifest scan exceeded bounded limit")
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise HistoryImportError("manifest escapes evidence directory")
                item = _local_evidence(path)
                if item is None:
                    continue
                key, status, digest = item
                found[key].append((status, digest))
            except (OSError, ValueError, TypeError, KeyError,
                    json.JSONDecodeError, HistoryImportError) as exc:
                invalid.append({"file": path.name, "reason": str(exc)[:180]})
    states = {}
    for key, values in found.items():
        digests = {sha for _, sha in values}
        if len(digests) > 1:
            states[key] = "conflicting_source_revisions_manual_review"
        elif any(state == "local_full_calendar_checked_not_drive_reverified"
                 for state, _ in values):
            states[key] = "local_full_calendar_checked_not_drive_reverified"
        elif any(state == "local_incomplete_calendar" for state, _ in values):
            states[key] = "local_incomplete_calendar"
        else:
            states[key] = "local_integrity_only_month_continuity_not_qualified"
    return states, invalid[:200], checked


def _requirements():
    for venue in VENUES:
        for asset in ASSETS:
            for family in PERP_FAMILIES:
                yield "perpetuals", venue, asset, family
    for venue in EVENT_VENUES:
        for asset in ASSETS:
            for family in EVENT_FAMILIES:
                yield "prediction_market", venue, asset, family
    for venue, families in CONTEXT_FAMILIES.items():
        for family in families:
            yield "context", venue, "GLOBAL", family


def write_ledger(
    *, roots: list[Path], first: str, through: str, out_dir: Path,
    now: datetime | None = None,
) -> dict:
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    if _month(through) > _month(last_completed_month(clock)):
        raise ValueError("current or future partial months cannot be certified")
    calendar = months(first, through)
    states, invalid, checked = scan_local(roots)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "source-family-coverage.jsonl.gz"
    summary_path = out_dir / "source-family-summary.json"
    temp = rows_path.with_name(rows_path.name + ".tmp")
    count = Counter()
    by_family: dict[str, Counter] = defaultdict(Counter)
    examples = []
    try:
        with gzip.open(temp, "wt", encoding="utf-8", compresslevel=6) as output:
            for scope, venue, asset, family in _requirements():
                for month in calendar:
                    status = states.get((venue, asset, family, month),
                                        "not_verified_in_this_run")
                    item = {
                        "scope": scope, "venue": venue, "asset": asset,
                        "family": family, "utc_month": month,
                        "status": status,
                        "provider_publication_and_contract_listing": "unverified",
                        "drive_sha_readback_this_run": False,
                        "production_model_feature_admitted": False,
                        "prospective_forecast": False,
                    }
                    output.write(json.dumps(item, sort_keys=True) + "\n")
                    count[status] += 1
                    by_family[f"{scope}/{venue}/{family}"][status] += 1
                    if status != "not_verified_in_this_run" and len(examples) < 30:
                        examples.append(item)
        os.replace(temp, rows_path)
    finally:
        temp.unlink(missing_ok=True)
    summary = {
        "schema_version": "rival-source-family-2021-ledger-v1",
        "generated_at": clock.isoformat(),
        "first_utc_month": first, "last_completed_utc_month": through,
        "current_partial_month_excluded": clock.strftime("%Y-%m"),
        "local_evidence_roots": [str(p) for p in roots],
        "local_manifests_inspected": checked,
        "invalid_local_manifests_count_including_truncated": len(invalid),
        "invalid_local_examples": invalid,
        "source_month_slots": sum(count.values()),
        "status_counts": dict(sorted(count.items())),
        "source_family_status_counts": {
            k: dict(sorted(v.items())) for k, v in sorted(by_family.items())
        },
        "sample_nonempty_rows": examples,
        "full_2021_to_present_coverage_proven": False,
        "provider_contract_existence_and_entitlements_proven": False,
        "remote_drive_warehouse_reconciled": False,
        "actual_live_model_import_or_improvement_proven": False,
        "real_fill_or_after_cost_edge_proven": False,
        "explanation": (
            "Unverified does not mean unavailable or not listed. Local research "
            "hashes are not current Drive readback, original publication-time "
            "proof, prospective predictions, or complete coverage of other families."
        ),
    }
    summary_path.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evidence-only historical source-family matrix")
    parser.add_argument("--root", type=Path, action="append", default=[])
    parser.add_argument("--from-month", default="2021-01")
    parser.add_argument("--through-month", default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)
    clock = datetime.now(UTC)
    try:
        result = write_ledger(
            roots=args.root, first=args.from_month,
            through=args.through_month or last_completed_month(clock),
            out_dir=args.out_dir, now=clock,
        )
    except (OSError, ValueError, HistoryImportError) as exc:
        parser.exit(2, f"RivaL source-family ledger BLOCKED: {exc}\n")
    print(json.dumps({
        "slots": result["source_month_slots"],
        "statuses": result["status_counts"],
        "complete_2021_to_present": False,
        "report": str(args.out_dir / "source-family-summary.json"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
