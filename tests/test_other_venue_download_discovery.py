"""Source download discovery must not claim source-native archive success."""
from __future__ import annotations

import json
from urllib import parse

import pytest

from tools.other_venue_download_discovery import (
    CANDIDATES, MAX_BYTES, PROBE_PARAMS, probe_url, report, verify_candidate,
)


def test_every_source_has_exact_venue_identity_and_no_paid_network_probes():
    known = {"bitget", "kucoin", "deribit", "dydx", "lighter", "phemex", "hyperliquid"}
    assert {item["venue"] for item in CANDIDATES} == known
    assert len(CANDIDATES) == len({(x["venue"], x["source"]) for x in CANDIDATES})
    for source in CANDIDATES:
        assert parse.urlsplit(source["url"]).scheme == "https"
        assert not source["perp_history_2021_verified"]
        assert source["kind"] in ("history_api", "interactive_portal", "requester_pays_archive", "third_party_bulk")
        if not source["free"]:
            assert probe_url(source) is None
        if source["kind"] in ("third_party_bulk", "requester_pays_archive"):
            assert source["probe"] == "skip"


def test_api_probes_are_bounded_and_do_not_guess_archive_file_names():
    assert MAX_BYTES <= 131072
    assert set(PROBE_PARAMS) == {"deribit", "dydx", "lighter"}
    for source in CANDIDATES:
        url = probe_url(source)
        if url:
            assert parse.urlsplit(url).netloc == parse.urlsplit(source["url"]).netloc
            assert parse.urlsplit(url).path == parse.urlsplit(source["url"]).path


def test_read_only_plan_never_reports_complete_or_downloaded():
    out = report(network=False)
    assert out["complete_2021_to_present_verified"] is False
    assert out["market_history_downloaded"] == 0
    assert out["models_promoted"] == 0
    assert not out["all_sources_are_original"]
    assert len(out["source_candidates"]) == len(CANDIDATES)
    assert all(x["probe_status"] == "not_run" for x in out["source_candidates"])
    assert all(not x["download_ready"] for x in out["source_candidates"])
    assert not any(x["archive_sha_verified"] for x in out["source_candidates"])
    json.dumps(out)


def test_paid_and_third_party_cannot_masquerade_as_official():
    for candidate in CANDIDATES:
        if candidate["kind"] in ("third_party_bulk", "requester_pays_archive"):
            result = verify_candidate(candidate)
            assert result["probe_status"] == "not_attempted_no_safe_bounded_proof"
            assert not result["archive_sha_verified"] and not result["download_ready"]


@pytest.mark.parametrize("source", [
    {"venue":"deribit","source":"malicious","url":"http://evil.example/test",
     "kind":"history_api","families":("candles",),"free":True,
     "probe":"deribit","needs":"not applicable"},
])
def test_probe_refuses_plain_http(source):
    with pytest.raises(ValueError):
        verify_candidate(source)
