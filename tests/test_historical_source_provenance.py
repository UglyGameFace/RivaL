"""Regression proof for source-exact listing, OI archives and remote readbacks."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from parlay_bot.research import binance_oi_metrics as oi
from parlay_bot.research import official_contract_listings as listings
from parlay_bot.research.drive_source_census import summarize
from parlay_bot.research.free_history import HistoryImportError, ingest_binance
from parlay_bot.research.historical_coordinator import build_plan
from parlay_bot.research.history_byte_audit import choose

NOW = datetime(2026, 11, 2, 15, tzinfo=UTC)


def _listing_report():
    b = {
        "symbols": [{
            "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
            "contractType": "PERPETUAL", "onboardDate": 1609459200000,
        }]
    }
    o = {"code": "0", "data": [{
        "instId": "SOL-USDT-SWAP", "instType": "SWAP",
        "settleCcy": "USDT", "ctType": "linear",
        "listTime": "1693526400000",
    }]}
    return (
        listings.parse_official("binance", json.dumps(b).encode(), now=NOW),
        listings.parse_official("okx", json.dumps(o).encode(), now=NOW),
    )


def test_official_listing_does_not_infer_other_exchange_or_unreturned_coins():
    binance, okx = _listing_report()
    assert next(x for x in binance if x["asset"] == "BTC")[
        "contract_trading_start_utc"] == "2021-01-01T00:00:00+00:00"
    assert next(x for x in okx if x["asset"] == "SOL")[
        "contract_trading_start_utc"] == "2023-09-01T00:00:00+00:00"
    assert next(x for x in binance if x["asset"] == "XRP")[
        "contract_trading_start_utc"] is None
    assert all(x["older_delisted_contracts_ruled_out"] is False
               for x in (*binance, *okx))


def test_wrong_market_duplicate_listing_and_future_evidence_fail_closed():
    forged = {"symbols": [{
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
        "contractType": "CURRENT_QUARTER", "onboardDate": 1609459200000,
    }]}
    with pytest.raises(HistoryImportError):
        listings.parse_official("binance", json.dumps(forged).encode(), now=NOW)
    forged["symbols"][0]["contractType"] = "PERPETUAL"
    forged["symbols"].append(forged["symbols"][0].copy())
    with pytest.raises(HistoryImportError, match="duplicate"):
        listings.parse_official("binance", json.dumps(forged).encode(), now=NOW)
    with pytest.raises(HistoryImportError):
        listings._time("1968864000000", now=NOW)


def test_coordinator_tracks_only_same_provider_listing_and_flags_conflicts():
    b, o = _listing_report()
    report = {
        "schema_version": "rival-official-perpetual-listings-v1",
        "observed_at": NOW.isoformat(), "assets": b + o,
        "all_listing_dates_verified": False,
    }
    plan = build_plan(summarize([
        "BTCUSDT-5m-2020-12--" + "a" * 16 + ".manifest.json",
        "BTCUSDT-5m-2021-01--" + "b" * 16 + ".manifest.json",
        "okx-SOL-USDT-SWAP-USDT-5m-2023-07--" + "c" * 16 + ".manifest.json",
    ], now=NOW), listing_report=report)
    assert next(x for x in plan["scopes"] if x["venue"] == "binance"
                and x["asset"] == "BTC")["actual_contract_listing_month"] == "2021-01"
    assert next(x for x in plan["scopes"] if x["venue"] == "okx"
                and x["asset"] == "SOL" and x["family"] == "candles_5m")[
                    "actual_contract_listing_month"] == "2023-09"
    assert next(x for x in plan["scopes"] if x["venue"] == "okx"
                and x["asset"] == "SOL" and x["family"] == "candles_5m")[
                    "named_months_before_reported_listing"] == ["2023-07"]
    assert next(x for x in plan["scopes"] if x["venue"] == "bitget"
                and x["asset"] == "BTC")["actual_contract_listing_month"] is None


def _metrics_csv(asset: str, day: date, count: int = 288) -> bytes:
    fields = sorted(oi.REQUIRED)
    lines = [",".join(fields)]
    for i in range(count):
        moment = datetime.combine(day, datetime.min.time(), UTC) + i * timedelta(minutes=5)
        row = {f: "1.2" for f in fields}
        row["create_time"] = moment.strftime("%Y-%m-%d %H:%M:%S")
        row["symbol"] = asset + "USDT"
        lines.append(",".join(row[f] for f in fields))
    return ("\n".join(lines) + "\n").encode()


def test_daily_oi_validates_exact_utc_grid_no_synthetic_missing_candles():
    day = date(2024, 4, 10)
    validated = oi.validate_csv(_metrics_csv("BTC", day), asset="BTC", day=day)
    assert validated["source_records"] == 288
    assert validated["source_day_complete_exact_5m_grid"]
    partial = oi.validate_csv(_metrics_csv("BTC", day, 287), asset="BTC", day=day)
    assert partial["source_records"] == 287
    assert not partial["source_day_complete_exact_5m_grid"]
    with pytest.raises(HistoryImportError):
        oi.validate_csv(_metrics_csv("BTC", day), asset="ETH", day=day)


def test_source_exact_binance_metrics_checksum_validates_original_zip(monkeypatch):
    day = date(2024, 4, 10)
    u = oi.url("ETH", day, now=NOW)
    file_name = u.rsplit("/", 1)[-1]
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr(file_name.removesuffix(".zip") + ".csv",
                   _metrics_csv("ETH", day))
    payload = raw.getvalue()
    checksum = (hashlib.sha256(payload).hexdigest()
                + "  " + file_name + "\n").encode()
    def fake_fetch(_client, url, *, limit):
        return checksum if url.endswith(".CHECKSUM") else payload
    monkeypatch.setattr(oi, "_fetch", fake_fetch)
    with httpx.Client() as client:
        original, check, proof = oi.download(
            client, asset="ETH", day=day, now=NOW)
    assert original == payload and check == checksum
    assert proof["source_day_complete_exact_5m_grid"]
    assert proof["provider_published_checksum"]
    assert not proof["live_model_admission"]
    monkeypatch.setattr(oi, "_fetch", lambda c, url, *, limit: (
        b"0" * 64 + b"  " + file_name.encode()
        if url.endswith(".CHECKSUM") else payload))
    with httpx.Client() as client, pytest.raises(HistoryImportError):
        oi.download(client, asset="ETH", day=day, now=NOW)


def test_daily_metrics_are_only_name_hints_not_full_historical_months():
    day = "2024-04-10"
    name = "binance-metrics-BTC-" + day + "--" + "a" * 16 + ".manifest.json"
    census = summarize([name], now=NOW)
    entries = census["binance_daily_metrics_not_full_months"]
    assert len(entries) == 1 and entries[0]["distinct_named_days"] == 1
    assert entries[0]["family"] == ["open_interest", "positioning"]
    assert not entries[0]["exact_full_calendar_month_verified"]
    plan = build_plan(census)
    for family in ("open_interest", "positioning"):
        row = next(r for r in plan["scopes"]
                   if (r["venue"], r["asset"], r["family"]) ==
                   ("binance", "BTC", family))
        assert row["original_daily_source_name_hints_not_months"] == 1
        assert row["verified_complete_months_from_this_census"] == 0


def test_no_repeated_expired_lookback_or_fake_100_percent_progress():
    available = {("BTC", "2026-10-31")}
    selected = oi.candidate_days(now=NOW, available=available, max_new=16)
    assert len(selected) == 16
    assert len(selected) == len(set(selected))
    assert ("BTC", date(2026, 10, 31)) not in selected
    for asset, day in selected:
        assert asset in oi.ASSETS and day < NOW.date()
    with pytest.raises(ValueError):
        oi.candidate_days(now=NOW, available=set(), max_new=10000)


def test_drive_month_audit_selection_rotates_and_rejects_wrong_family():
    names = [
        "BTCUSDT-5m-2025-01--" + "a" * 16 + ".manifest.json",
        "ETHUSDT-5m-2025-01--" + "b" * 16 + ".manifest.json",
        "SOLUSDT-5m-2025-01--" + "c" * 16 + ".manifest.json",
        "XRPUSDT-5m-2025-01--" + "d" * 16 + ".manifest.json",
        "XRPUSDT-5m-2025-02--" + "e" * 16 + ".manifest.json",
        "binance-metrics-XRP-2025-01-01--" + "f" * 16 + ".manifest.json",
    ]
    a = choose(names, now=NOW, limit=2)
    b = choose(names, now=NOW + timedelta(hours=12), limit=2)
    assert len(a) == len(b) == 2 and a != b
    assert all("USDT-5m-" in x for x in a + b)


def test_retained_provider_original_zip_is_additive_no_source_relabel(
    monkeypatch, tmp_path,
):
    from parlay_bot.research import free_history

    month = "2024-04"
    first = int(datetime(2024, 4, 1, tzinfo=UTC).timestamp() * 1000)
    symbol = "BTCUSDT-5m-2024-04"
    zfile = symbol + ".zip"
    lines = []
    for n in range(2):
        stamp = first + n * 300000
        lines.append(",".join(map(str, [
            stamp, 10, 12, 9, 11, 100, stamp + 299999,
            1000, 10, 20, 200, 0,
        ])))
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as z:
        z.writestr(symbol + ".csv", "\n".join(lines))
    raw = source.getvalue()
    checksum = (hashlib.sha256(raw).hexdigest() + "  " + zfile).encode()
    monkeypatch.setattr(free_history, "_fetch",
                        lambda _client, url, **kwargs:
                        checksum if url.endswith(".CHECKSUM") else raw)
    with httpx.Client() as client:
        result = ingest_binance(
            client, asset="BTC", interval="5m", month=month,
            out_dir=tmp_path, retain_original=True,
        )
    assert (result.manifest_path.parent / zfile).read_bytes() == raw
    assert (result.manifest_path.parent / (zfile + ".CHECKSUM")).read_bytes() == checksum
    manifest = json.loads(result.manifest_path.read_text())
    # Preserve original normalized manifest layout for older byte-identical
    # receipts. New source ZIP readback is a SEPARATE publication receipt.
    assert "original_zip_retained_locally" not in manifest
    assert "original_zip_remote_readback_proven_by_manifest" not in manifest
