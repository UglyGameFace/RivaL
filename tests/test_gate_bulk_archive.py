"""Contracts for the published Gate futures archive URL and CSV schemas.

Examples follow Gate's official historical-quotes documentation: most
downloads contain no header; historical events must not become live proof.
"""
import gzip
import json

import httpx
import pytest

from parlay_bot.research.gate_bulk_archive import download, inspect, url_for

MONTH = "2021-08"
T0 = "1627776000"


def compressed(csv_text: str) -> bytes:
    return gzip.compress(csv_text.encode("utf-8"))


@pytest.mark.parametrize(("kind", "fields"), [
    ("trades", f"{T0}.742477,5427,41.310000,-2"),
    ("mark_prices", f"{T0}.537,35041.68,35041.67,35020.7"),
    ("funding_updates", f"{T0},0.0001,0.0003,0,36.99,35036.29,35036.32,476"),
    ("funding_applies", f"{T0},-0.000315"),
    ("candlesticks_1m", f"{T0},399272,32372.13,32490.9,32265.76,32425.09"),
    ("candlesticks_5m", f"{T0},399272,32372.13,32490.9,32265.76,32425.09"),
    ("candlesticks_1h", f"{T0},399272,32372.13,32490.9,32265.76,32425.09"),
])
def test_real_provider_style_headerless_csv_schemas(kind, fields):
    data = compressed(fields + "\n")
    result = inspect(data, asset="BTC", kind=kind, month=MONTH)
    assert result["rows"] == 1
    assert result["has_header"] is False
    assert result["source_native_model_eligible"] is False
    assert result["time_alignment_verified"] is False
    assert result["first_source_timestamp"] == float(fields.split(",")[0])
    assert len(result["compressed_sha256"]) == 64


def test_optional_exact_header_does_not_count_as_data():
    data = compressed("timestamp,dealid,price,size\n1627776000,5427,41.31,-2\n")
    result = inspect(data, asset="BTC", kind="trades", month=MONTH)
    assert result["rows"] == 1
    assert result["has_header"] is True


def test_gate_monthly_url_and_restricted_scope():
    assert url_for(asset="BTC", kind="trades", month=MONTH) == (
        "https://download.gatedata.org/futures_usdt/trades/202108/BTC_USDT-202108.csv.gz"
    )
    assert url_for(asset="ETH", kind="candlesticks_5m", month="2023-04") == (
        "https://download.gatedata.org/futures_usdt/"
        "candlesticks_5m/202304/ETH_USDT-202304.csv.gz"
    )
    for kwargs in (
        {"asset": "DOGE", "kind": "trades", "month": MONTH},
        {"asset": "BTC", "kind": "orders", "month": MONTH},
        {"asset": "BTC", "kind": "trades", "month": "2021-13"},
        {"asset": "BTC", "kind": "trades", "month": "../etc"},
    ):
        with pytest.raises(ValueError):
            url_for(**kwargs)


@pytest.mark.parametrize(("kind", "body"), [
    ("trades", "1627776000,5427,41.31"),  # missing size
    ("trades", "1627776000,5427,41.31,-2,unexpected"),
    ("trades", "1627776000,5427,NaN,2"),
    ("trades", "1627776000,-1,41.31,2"),
    ("trades", "1627776000,5427,-41.31,2"),
    ("trades", "1630540800,5427,41.31,2"),  # >8h outside August
    ("trades", "timestamp,dealid,price,size"),  # empty header-only archive
    ("candlesticks_5m", "1627776000,10,100,99,95,96"),  # invalid high
    ("candlesticks_5m", "1627776000,10,100,102,101,96"),  # invalid low
    ("funding_applies", "1627776000,inf"),
])
def test_invalid_csv_fail_closed(kind, body):
    with pytest.raises(ValueError):
        inspect(compressed(body + "\n"), asset="BTC", kind=kind, month=MONTH)


def test_truncated_gzip_fails_closed():
    sample = compressed("1627776000,5427,41.31,2\n")
    with pytest.raises(ValueError):
        inspect(sample[:-5], asset="BTC", kind="trades", month=MONTH)


def test_gate_http_download_writes_immutable_research_receipt(tmp_path):
    data = compressed("1627776000,5427,41.31,2\n")
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=data)
    )) as client:
        out = tmp_path / "2021-08.csv.gz"
        result = download(client=client, asset="BTC", kind="trades",
                          month=MONTH, output=out)
        assert result["rows"] == 1
        assert result["compressed_bytes"] == len(data)
        assert out.read_bytes() == data
        meta = json.loads(out.with_suffix(".gz.manifest.json").read_text())
        assert meta["compressed_sha256"] == result["compressed_sha256"]
        assert meta["source_native_model_eligible"] is False
        with pytest.raises(ValueError, match="already exists"):
            download(client=client, asset="BTC", kind="trades",
                     month=MONTH, output=out)


def test_http_missing_archive_is_not_written(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404)
    )) as client:
        target = tmp_path / "missing.csv.gz"
        with pytest.raises(ValueError, match="HTTP 404"):
            download(client=client, asset="BTC", kind="trades",
                     month=MONTH, output=target)
        assert not target.exists()


def test_corrupt_source_is_not_persisted(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"bad archive")
    )) as client:
        target = tmp_path / "bad.csv.gz"
        with pytest.raises(ValueError, match="invalid Gate gzip"):
            download(client=client, asset="BTC", kind="trades",
                     month=MONTH, output=target)
        assert not target.exists()
