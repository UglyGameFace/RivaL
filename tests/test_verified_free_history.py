from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.research.benchmarks import (
    _load_verified,
    bookmaker_benchmark,
    fair_probabilities,
)
from parlay_bot.research.free_history import (
    HistoryImportError,
    _fetch,
    binance_archive_url,
    ingest_binance,
    ingest_football,
)

START = datetime(2024, 1, 1, tzinfo=UTC)


def binance_csv(*, gap=False, duplicate=False, malformed=False):
    begin = int(START.timestamp() * 1000)
    rows = []
    for i in (0, 2 if gap else 1):
        opened = begin + i * 300_000
        rows.append([
            str(opened if not malformed else opened * 1000),
            "100", "105", "95", "101", "33", str(opened + 299_999),
            "3333", "70", "12", "1200", "0",
        ])
    if duplicate:
        rows.append(rows[-1])
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    return output.getvalue().encode()


def zip_of(payload, *, name="BTCUSDT-5m-2024-01.csv", extra=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, payload)
        if extra:
            z.writestr("../unexpected.csv", "bad")
    return buffer.getvalue()


def mock_download(zip_bytes, *, checksum=True):
    archive = "BTCUSDT-5m-2024-01.zip"
    sha = hashlib.sha256(zip_bytes).hexdigest()
    def handler(request):
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(
                200, text=(sha if checksum else "0" * 64) + "  " + archive + "\n",
            )
        if request.url.path.endswith(archive):
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_binance_verified_manifest_idempotent_and_gap(tmp_path):
    with mock_download(zip_of(binance_csv(gap=True))) as client:
        got = ingest_binance(
            client, asset="BTC", interval="5m", month="2024-01", out_dir=tmp_path,
        )
        again = ingest_binance(
            client, asset="BTC", interval="5m", month="2024-01", out_dir=tmp_path,
        )
    assert got == again
    assert got.records == 2 and got.gaps == 1
    manifest = json.loads(got.manifest_path.read_text())
    assert manifest["checksum_verified"] is True
    assert manifest["prospective_forecast"] is False
    assert manifest["interval_minutes"] == 5
    assert manifest["source_sha256"]
    bars = [json.loads(s) for s in got.data_path.read_text().splitlines()]
    assert bars[0]["opened_at"] == START.isoformat()
    assert "first_observed_at" not in bars[0]
    assert len(list(tmp_path.rglob("*.ndjson"))) == 1


@pytest.mark.parametrize("wrong", ["checksum", "extra", "duplicate", "microseconds"])
def test_corrupt_market_history_fails_closed(tmp_path, wrong):
    content = binance_csv(duplicate=wrong == "duplicate", malformed=wrong == "microseconds")
    packed = zip_of(content, extra=wrong == "extra")
    with mock_download(packed, checksum=wrong != "checksum") as client, pytest.raises(
        HistoryImportError
    ):
        ingest_binance(
            client, asset="BTC", interval="5m", month="2024-01", out_dir=tmp_path,
        )
    assert not list(tmp_path.rglob("*.ndjson"))


def test_source_allowlist_rejects_malicious_paths_and_partial_month():
    for asset in ("../BTC", "BTC/../../", "UNKNOWN"):
        with pytest.raises(HistoryImportError):
            binance_archive_url(asset=asset, interval="5m", month="2024-01")
    with pytest.raises(HistoryImportError):
        binance_archive_url(asset="BTC", interval="1s", month="2024-01")
    with pytest.raises(HistoryImportError):
        binance_archive_url(asset="BTC", interval="5m", month=datetime.now(UTC).strftime("%Y-%m"))


def test_bound_on_remote_payload():
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"x" * 13),
    )) as client, pytest.raises(HistoryImportError):
        _fetch(client, "https://data.binance.vision/a", limit=12)


def football_csv(*, wrong_result=False):
    out = io.StringIO()
    keys = [
        "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR",
        "B365H", "B365D", "B365A", "B365CH", "B365CD", "B365CA",
    ]
    writer = csv.DictWriter(out, fieldnames=keys)
    writer.writeheader()
    writer.writerow(dict(zip(keys, [
        "10/08/2025", "Alpha", "Beta", "2", "1", "A" if wrong_result else "H",
        "2.50", "3.20", "2.90", "2.30", "3.40", "3.10",
    ], strict=True)))
    writer.writerow(dict(zip(keys, [
        "11/08/2025", "Gamma", "Delta", "0", "0", "D",
        "2.05", "3.30", "4.50", "2.15", "3.10", "4.10",
    ], strict=True)))
    return out.getvalue().encode()


def test_football_research_and_bookmaker_baseline(tmp_path):
    payload = football_csv()
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=payload),
    )) as client:
        got = ingest_football(client, league="E0", season="2526", out_dir=tmp_path)
    assert got.records == 2
    manifest = json.loads(got.manifest_path.read_text())
    assert manifest["opening_odds_have_verified_capture_time"] is False
    assert manifest["source_redistribution_rights_not_assumed"] is True
    rows = _load_verified(got.manifest_path)
    assert rows[0]["books"]["B365"]["opening"] == [2.5, 3.2, 2.9]
    assert rows[0]["books"]["B365"]["closing"] == [2.3, 3.4, 3.1]
    result = bookmaker_benchmark(rows)
    assert result["all"]["matches"] == 2
    assert result["all"]["closing_matches"] == 2
    assert result["status"] == "thin_descriptive_sample"
    assert result["model_trained"] is False
    assert result["independent_prospective_forecasts"] == 0
    assert result["actual_betting_profit_verified"] is False


def test_football_rejects_outcome_mismatch_and_tampered_research(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=football_csv(wrong_result=True)),
    )) as client, pytest.raises(HistoryImportError):
        ingest_football(client, league="E0", season="2526", out_dir=tmp_path)
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=football_csv()),
    )) as client:
        got = ingest_football(client, league="E0", season="2526", out_dir=tmp_path)
    got.data_path.write_bytes(got.data_path.read_bytes() + b"{}\n")
    with pytest.raises(HistoryImportError):
        _load_verified(got.manifest_path)


def test_bookmaker_price_safety():
    fair, margin = fair_probabilities([2.5, 3.2, 2.9])
    assert sum(fair) == pytest.approx(1.0)
    assert margin > 0
    for bad in ([float("nan"), 2, 3], [1.0, 2, 3], [2, 3], [2, 3, float("inf")]):
        with pytest.raises(HistoryImportError):
            fair_probabilities(bad)


def test_football_no_retrospective_result_as_live_signal(tmp_path):
    payload = football_csv()
    with httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=payload),
    )) as client:
        got = ingest_football(client, league="E0", season="2526", out_dir=tmp_path)
    row = _load_verified(got.manifest_path)[0]
    row["odds_snapshot_time_verified"] = True
    with pytest.raises(HistoryImportError):
        bookmaker_benchmark([row])


def test_xrp_archive_is_allowed_with_exact_source_contract():
    url = binance_archive_url(asset="XRP", interval="5m", month="2026-09")
    assert "XRPUSDT" in url
    assert "XRPUSDT-5m-2026-09.zip" in url
