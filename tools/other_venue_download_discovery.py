"""Bounded discovery of real, legally usable historical source entry points.

This checks a tiny public response, NOT the complete archive or data coverage.
No API keys, paid transfers, model changes, or raw warehouse publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, parse, request

MAX_BYTES = 131072
PROBE_TIMEOUT_SECONDS = 12

# Exact independently documented entry points. Portals are not raw archives.
CANDIDATES = (
    {
        "venue": "bitget", "source": "official_market_download_portal",
        "url": "https://www.bitget.com/data-download",
        "kind": "interactive_portal", "families": ("candles", "trades", "orderbook_l2"),
        "perp_history_2021_verified": False, "free": True,
        "probe": "portal", "needs": "Identify the actual futures file links and release months",
    },
    {
        "venue": "kucoin", "source": "official_market_download_portal",
        "url": "https://www.kucoin.com/markets/historydata",
        "kind": "interactive_portal", "families": ("candles", "trades", "orderbook_l2"),
        "perp_history_2021_verified": False, "free": True,
        "probe": "portal", "needs": "Confirm futures support; displayed public categories may be spot only",
    },
    {
        "venue": "deribit", "source": "official_history_rest",
        "url": "https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time",
        "kind": "history_api", "families": ("trades", "expired_instrument_metadata"),
        "perp_history_2021_verified": False, "free": True,
        "probe": "deribit",
        "needs": "Page actual BTC/ETH/SOL/XRP contracts and verify original trade sequence",
    },
    {
        "venue": "dydx", "source": "official_chain_v4_indexer",
        "url": "https://indexer.dydx.trade/v4/candles/perpetualMarkets/BTC-USD",
        "kind": "history_api", "families": ("candles",),
        "perp_history_2021_verified": False, "free": True,
        "probe": "dydx",
        "needs": "Do not confuse v3 history with v4 chain-market launch",
    },
    {
        "venue": "lighter", "source": "official_candles_rest",
        "url": "https://mainnet.zklighter.elliot.ai/api/v1/candles",
        "kind": "history_api", "families": ("candles", "funding"),
        "perp_history_2021_verified": False, "free": True,
        "probe": "lighter",
        "needs": "Page bounded original candles; verify asset/market mapping before ingest",
    },
    {
        "venue": "phemex", "source": "official_contract_rest",
        "url": "https://api.phemex.com/exchange/public/md/v2/kline/list",
        "kind": "history_api", "families": ("candles",),
        "perp_history_2021_verified": False, "free": True,
        "probe": "skip",
        "needs": "Verify supported historical range and parameters against live source",
    },
    {
        "venue": "hyperliquid", "source": "official_historical_s3",
        "url": "https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data",
        "kind": "requester_pays_archive", "families": ("orderbook_l2", "fills", "asset_context"),
        "perp_history_2021_verified": False, "free": False,
        "probe": "skip",
        "needs": "Never transfer requester-paid AWS data without cost authorization",
    },
    {
        "venue": "lighter", "source": "community_repacked_release",
        "url": "https://github.com/Ankvik-Tech-Labs/lighter-historical-data/releases",
        "kind": "third_party_bulk", "families": ("candles", "funding"),
        "perp_history_2021_verified": False, "free": True,
        "probe": "skip",
        "needs": "Check usage rights, raw hashes, contract identity and official API parity",
    },
)

# Do not construct source download URLs from query parameters that were never
# independently documented. These bounded probes exist ONLY to test reachability.
PROBE_PARAMS = {
    "deribit": {
        "instrument_name": "BTC-PERPETUAL", "include_old": "true",
        "start_timestamp": "1609459200000", "end_timestamp": "1609459260000",
        "count": "2",
    },
    "dydx": {"resolution": "5MINS", "limit": "1"},
    "lighter": {
        "market_id": "1", "resolution": "1h",
        "start_timestamp": "1788776325", "end_timestamp": "1788862725",
        "count_back": "2",
    },
}

class _NoCrossHostRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if parse.urlsplit(newurl).hostname != parse.urlsplit(req.full_url).hostname:
            raise ValueError("cross-host redirect requires separate approval")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_url(candidate: dict) -> str | None:
    if candidate["probe"] == "skip" or not candidate["free"]:
        return None
    endpoint = candidate["url"]
    if candidate["probe"] in PROBE_PARAMS:
        endpoint += "?" + parse.urlencode(PROBE_PARAMS[candidate["probe"]])
    return endpoint


def verify_candidate(candidate: dict) -> dict:
    """Always distinguish reachable portal/API from verified historical bytes."""
    record = {
        "venue": candidate["venue"], "source": candidate["source"],
        "kind": candidate["kind"], "url": candidate["url"],
        "data_families_offered": list(candidate["families"]),
        "download_cost_policy": "free_entry_point" if candidate["free"] else "requester_pays_blocked",
        "bulk_2021_coverage_verified": False, "archive_sha_verified": False,
        "download_ready": False, "next_requirement": candidate["needs"],
    }
    endpoint = probe_url(candidate)
    if endpoint is None:
        record["probe_status"] = "not_attempted_no_safe_bounded_proof"
        return record
    parsed = parse.urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname != parse.urlsplit(candidate["url"]).hostname:
        raise ValueError("untrusted source host")
    opener = request.build_opener(_NoCrossHostRedirect())
    try:
        with opener.open(
            request.Request(endpoint, headers={
                "User-Agent": "RivaL-history-discovery/1.0",
                "Accept": "application/json,text/html;q=0.8",
            }),
            timeout=PROBE_TIMEOUT_SECONDS,
        ) as response:
            if response.status != 200:
                record["probe_status"] = "source_http_non_200"
                record["http_status"] = response.status
                return record
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            record["probe_status"] = "response_exceeds_probe_limit"
            return record
        if candidate["probe"] in ("deribit", "dydx", "lighter"):
            doc = json.loads(raw)
            ok = (
                isinstance(doc, dict)
                and (
                    (candidate["probe"] == "deribit"
                     and isinstance(doc.get("result"), dict)
                     and isinstance(doc["result"].get("trades"), list))
                    or (candidate["probe"] == "dydx"
                        and isinstance(doc.get("candles"), list))
                    or (candidate["probe"] == "lighter"
                        and isinstance(doc.get("c"), list))
                )
            )
        else:
            ok = len(raw) > 0 and b"<html" in raw[:20000].lower()
        record["probe_status"] = "reachable_entry_point_only" if ok else "unexpected_schema"
        record["sample_response_sha256"] = hashlib.sha256(raw).hexdigest()
        record["sample_response_bytes"] = len(raw)
    except (error.HTTPError, error.URLError, OSError, ValueError,
            json.JSONDecodeError, ssl.SSLError, TimeoutError) as exc:
        record["probe_status"] = "unverified_network_or_schema"
        record["error_class"] = type(exc).__name__
    return record


def report(*, network: bool) -> dict:
    if len({(c["venue"], c["source"]) for c in CANDIDATES}) != len(CANDIDATES):
        raise ValueError("duplicate venue/source identifier")
    candidates = [
        verify_candidate(c) if network else {
            "venue": c["venue"], "source": c["source"], "url": c["url"],
            "kind": c["kind"], "download_cost_policy":
                "free_entry_point" if c["free"] else "requester_pays_blocked",
            "probe_status": "not_run", "bulk_2021_coverage_verified": False,
            "archive_sha_verified": False, "download_ready": False,
            "next_requirement": c["needs"],
        } for c in CANDIDATES
    ]
    return {
        "schema_version": "rival-other-venue-download-discovery-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "network_probes_enabled": network,
        "source_candidates": candidates,
        "complete_2021_to_present_verified": False,
        "market_history_downloaded": 0,
        "models_promoted": 0,
        "all_sources_are_original": False,
        "disclaimer": (
            "Reachable endpoints/portals are NOT monthly archive availability. "
            "No historical source has been imported or qualified by this probe."
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data/native-reports/other-venue-download-discovery.json"))
    args = parser.parse_args(argv)
    outcome = report(network=args.probe)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = [(c["venue"], c["source"], c["probe_status"]) for c in outcome["source_candidates"]]
    print(json.dumps({"sources": summary, "full_history_verified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
