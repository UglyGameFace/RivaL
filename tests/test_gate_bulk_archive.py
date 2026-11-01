"""Offline contract tests for public Gate bulk source acquisition."""
import gzip
import io
from pathlib import Path

import httpx
import pytest

from parlay_bot.research.gate_bulk_archive import download, inspect, url_for


def test_gate_monthly_url_and_fail_closed_scope():
    assert url_for(asset="BTC", kind="trades", month="2021-08") == (
        "https://download.gatedata.org/futures_usdt/trades/202108/BTC_USDT-202108.csv.gz"
    )
    for kwargs in (
        {"asset": "DOGE", "kind": "trades", "month": "2021-08"},
        {"asset": "BTC", "kind": "orders", "month": "2021-08"},
        {"asset": "BTC", "kind": "trades", "month": "2021-13"},
    ):
        with pytest.raises(ValueError):
            url_for(**kwargs)


def test_gate_archive_bounded_and_research_only(tmp_path):
    payload = gzip.compress(b"timestamp,price,amount\n1627776000,100,2\n")
    receipt = inspect(payload, asset="BTC", kind="trades", month="2021-08")
    assert receipt["rows"] == 2
    assert receipt["source_native_model_eligible"] is False
    assert receipt["time_alignment_verified"] is False
    with pytest.raises(ValueError):
        inspect(gzip.compress(b"timestamp,price\n1\n"), asset="BTC",
                kind="trades", month="2021-08")
    with pytest.raises(ValueError):
        inspect(b"not gzip", asset="BTC", kind="trades", month="2021-08")


def test_gate_http_download_and_immutable_receipt(tmp_path):
    payload = gzip.compress(b"timestamp,price,amount\n1627776000,100,2\n")
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )) as client:
        out = tmp_path / "2021-08.csv.gz"
        result = download(client=client, asset="BTC", kind="trades",
                          month="2021-08", output=out)
        assert result["compressed_bytes"] == len(payload)
        assert out.read_bytes() == payload
        assert out.with_suffix(".gz.manifest.json").exists()
        with pytest.raises(ValueError, match="already exists"):
            download(client=client, asset="BTC", kind="trades",
                     month="2021-08", output=out)
