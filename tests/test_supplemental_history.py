"""Regressions for the additive retrospective-only supplemental intake."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.research.supplemental_history import (
    _funding_rows, _premium_rows, _report_status, _resume_verified,
    binance_supplement_url, collect_supplemental, ingest_binance_supplement,
    ingest_kalshi_contract, ingest_polymarket_token, supplemental_jobs,
)
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.publish import prepare

MONTH = "2024-01"
T0 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def fixture_zip(name: str, csv_data: bytes) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zipfile_obj:
        zipfile_obj.writestr(name[:-4] + ".csv", csv_data)
    return target.getvalue()


def mock_archive(url: str, csv_data: bytes, *, tamper=False) -> httpx.Client:
    name = url.rsplit("/", 1)[-1]
    archive = fixture_zip(name, csv_data)
    digest = hashlib.sha256(archive).hexdigest()
    def handler(request):
        if str(request.url) == url + ".CHECKSUM":
            return httpx.Response(200, text=(("0" * 64) if tamper else digest) +
                                  "  " + name + "\n")
        if str(request.url) == url:
            return httpx.Response(200, content=archive)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def kline(*, negative=False) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    for i in range(2):
        ts = T0 + i * 300_000
        low, high = ("-0.004", "-0.002") if negative else ("90", "110")
        writer.writerow([ts, "-0.003" if negative else "100", high, low,
                         "-0.0025" if negative else "101", 0, ts + 299999,
                         0, 0, 0, 0, 0])
    return out.getvalue().encode()


def funding_csv() -> bytes:
    return (b"calc_time,funding_interval_hours,last_funding_rate\n" +
            f"{T0},8,0.0001\n{T0 + 8 * 3600000},8,-0.0002\n".encode())


@pytest.mark.parametrize("kind", ["funding", "mark", "index", "premium"])
def test_official_binance_supplements_are_verified_and_isolated(tmp_path, kind):
    url = binance_supplement_url(asset="BTC", kind=kind, month=MONTH)
    payload = funding_csv() if kind == "funding" else kline(negative=kind == "premium")
    with mock_archive(url, payload) as client:
        result = ingest_binance_supplement(
            client, asset="BTC", kind=kind, month=MONTH, out_dir=tmp_path,
        )
        again = ingest_binance_supplement(
            client, asset="BTC", kind=kind, month=MONTH, out_dir=tmp_path,
        )
    assert result == again and result.records == 2
    verified = prepare(result.manifest_path)
    assert verified.source == result.source and verified.history.row_count == 2
    meta = json.loads(result.manifest_path.read_text())
    assert meta["retrospective"] and not meta["prospective_forecast"]
    assert meta["feature_family"] == kind
    assert meta["no_verified_bid_ask_or_executability"]
    assert _resume_verified(tmp_path, result.source, result.data_path.name.split("--")[0])


@pytest.mark.parametrize("kind", ["funding", "mark", "index", "premium"])
def test_binance_source_checksum_tamper_rejected(tmp_path, kind):
    url = binance_supplement_url(asset="BTC", kind=kind, month=MONTH)
    with mock_archive(url, funding_csv() if kind == "funding" else kline(), tamper=True) as client:
        with pytest.raises(HistoryImportError):
            ingest_binance_supplement(
                client, asset="BTC", kind=kind, month=MONTH, out_dir=tmp_path,
            )
    assert not list(tmp_path.rglob("*.ndjson"))


def test_premium_accepts_negative_prices_but_rejects_broken_grid():
    rows, gaps = _premium_rows(kline(negative=True))
    assert rows[0]["close"] < 0 and gaps == 0
    malformed = kline(negative=True).replace(str(T0 + 300_000).encode(),
                                              str(T0 + 300_100).encode())
    with pytest.raises(HistoryImportError):
        _premium_rows(malformed)


def test_funding_data_requires_schema_and_order():
    assert _funding_rows(funding_csv())[1]["rate"] < 0
    with pytest.raises(HistoryImportError):
        _funding_rows(b"fundingTime,fundingRate\n100,0.2\n")
    with pytest.raises(HistoryImportError):
        _funding_rows(funding_csv() + f"{T0},8,0.00001\n".encode())


def _market_client(payload):
    def handler(request):
        return httpx.Response(200, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_polymarket_history_is_retrospective_token_not_condition_price(tmp_path):
    start = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp())
    condition = "0x" + "a" * 64
    token = "123456789012345678901234567890"
    payload = {"history": [{"t": start + 20, "p": "0.42"},
                           {"t": start + 40, "p": 0.58}]}
    with _market_client(payload) as client:
        result = ingest_polymarket_token(
            client, condition_id=condition, token_id=token,
            start_ts=start, end_ts=start + 400, fidelity=5, out_dir=tmp_path,
        )
    meta = json.loads(result.manifest_path.read_text())
    assert meta["token_id"] == token and meta["condition_id"] == condition
    assert meta["no_historical_orderbook_or_fill_evidence"] is True
    assert prepare(result.manifest_path).source == "polymarket_token_history"
    with _market_client({"history": [{"t": start + 100, "p": 0.2},
                                     {"t": start + 10, "p": 0.3}]}) as client:
        with pytest.raises(HistoryImportError):
            ingest_polymarket_token(
                client, condition_id=condition, token_id=token,
                start_ts=start, end_ts=start + 400, fidelity=5, out_dir=tmp_path,
            )


def test_kalshi_quotes_have_ohlc_and_no_made_up_book_fills(tmp_path):
    start = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
    response = {"candlesticks": [
        {"end_period_ts": start + 3600,
         "yes_bid": {"open": "0.34", "high": "0.36", "low": "0.30", "close": "0.35"},
         "yes_ask": {"open": "0.38", "high": "0.41", "low": "0.36", "close": "0.40"},
         "price": None, "volume": 8, "open_interest": 30},
    ]}
    with _market_client(response) as client:
        item = ingest_kalshi_contract(
            client, ticker="KXBTC-26AUG01-AT100000", start_ts=start,
            end_ts=start + 7200, period_minutes=60, out_dir=tmp_path,
        )
    assert item.records == 1
    rows = [json.loads(t) for t in item.data_path.read_text().splitlines()]
    assert rows[0]["yes_bid"]["close"] == .35
    assert rows[0]["open_interest"] == 30
    assert json.loads(item.manifest_path.read_text())["settlement_outcome_not_included"]
    assert prepare(item.manifest_path).source == "kalshi_contract_candles"


def test_bounded_batch_does_not_claim_incomplete_success(tmp_path):
    jobs = supplemental_jobs(["2026-09"], ["BTC", "ETH"])
    assert len(jobs) == 8
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404),
    )) as client:
        result = collect_supplemental(client, root=tmp_path, jobs=jobs, max_new=2)
    assert result["rejected"] == 2 and result["remaining"] == 6
    report = _report_status(tmp_path, jobs, result)
    assert report["verified_coverage"]["binance_um_funding"]["missing"] == 2
    assert report["capabilities_not_retroactively_proven"]["historical_order_book_depth"]
    assert report["model_training_from_supplements_enabled"] is False
    assert not list(tmp_path.rglob("*.ndjson"))


def test_reject_untrusted_source_identity_and_future_month():
    with pytest.raises(HistoryImportError):
        binance_supplement_url(asset="../BTC", kind="funding", month=MONTH)
    with pytest.raises(HistoryImportError):
        binance_supplement_url(asset="BTC", kind="index", month="2030-01")
