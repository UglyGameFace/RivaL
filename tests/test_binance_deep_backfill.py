"""Offline acceptance for bounded Binance 2021+ ZIP research publication."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.research import binance_deep_backfill as deep


def test_plan_starts_at_january_2021_and_excludes_current_month():
    assert deep.months_since_2021(
        datetime(2021, 3, 19, tzinfo=UTC),
    ) == ["2021-01", "2021-02"]
    assert deep.months_since_2021(datetime(2021, 1, 1, tzinfo=UTC)) == []
    with pytest.raises(ValueError):
        deep.months_since_2021(datetime(2021, 1, 1))



def test_recent_months_first_while_older_backlog_and_resume_hints_survive():
    months = ["2021-01", "2021-02", "2021-03", "2021-04",
              "2021-05", "2021-06", "2026-09", "2026-10"]
    existing = {"BTCUSDT-5m-2026-10", "ETHUSDT-5m-2021-01"}
    pairs, skipped = deep.prioritize_missing_source_months(
        months, ("BTC", "ETH"), existing,
        now=datetime(2026, 11, 2, tzinfo=UTC),
    )
    assert skipped == 2
    assert len(pairs) == len(months) * 2 - skipped
    assert pairs[:3] == [
        ("ETH", "2026-10"), ("BTC", "2026-09"), ("ETH", "2026-09"),
    ]
    assert pairs[-1] == ("BTC", "2021-01")
    assert ("BTC", "2021-01") in pairs  # older history kept in backlog
    assert ("BTC", "2026-10") not in pairs


@pytest.mark.asyncio
async def test_drive_binance_month_inventory_paginates_and_filters(tmp_path):
    queries = []
    class Client:
        async def get(self, url, *, params, headers):
            queries.append(params)
            if len(queries) == 1:
                files = [{
                    "name": "BTCUSDT-5m-2021-01--abcdefabcdefabcd.manifest.json",
                    "parents": ["private-manifests"],
                    "appProperties": {"rival_kind": "manifest"},
                }]
                return httpx.Response(200, json={
                    "files": files, "nextPageToken": "next",
                })
            return httpx.Response(200, json={
                "files": [{
                    "name": "BTCUSDT-5m-2021-02--abcdefabcdefabcd.manifest.json",
                    "parents": ["private-manifests"],
                    "appProperties": {"rival_kind": "manifest"},
                }],
            })
    class Drive:
        DRIVE_URL = "https://www.googleapis.com/drive/v3"
        folders = SimpleNamespace(manifests="private-manifests")
        _client = Client()

        async def _verify_folder_boundary(self):
            pass

        async def _headers(self):
            return {}

        @staticmethod
        def _escape_query(value):
            return value

    keys = await deep.existing_month_keys(Drive())
    assert keys == {"BTCUSDT-5m-2021-01", "BTCUSDT-5m-2021-02"}
    assert queries[1]["pageToken"] == "next"


@pytest.mark.asyncio
async def test_publish_only_complete_source_month_and_remote_sha_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(deep, "months_since_2021", lambda now: ["2021-01", "2021-02"])
    async def existing(drive):
        return {"BTCUSDT-5m-2021-01"}
    monkeypatch.setattr(deep, "existing_month_keys", existing)
    calls = []
    def ingest(client, *, asset, interval, month, out_dir):
        calls.append((asset, interval, month))
        return SimpleNamespace(records=8928, manifest_path=Path("pretend"))
    monkeypatch.setattr(deep, "ingest_binance", ingest)
    monkeypatch.setattr(deep, "audit_partition", lambda p: {
        "month": "2021-02", "venue": "binance", "contract": "BTCUSDT",
        "interval_minutes": 5, "full_calendar_month_verified": True,
    })
    monkeypatch.setattr(deep, "prepare", lambda p: SimpleNamespace(
        history=SimpleNamespace(sha256="a" * 64)
    ))
    async def publish(drive, prepared):
        return {"restore_sha256_verified": True}
    monkeypatch.setattr(deep, "publish", publish)
    outcome = await deep.collect_official_binance(
        drive=object(), assets=("BTC",), max_new=1,
        now=datetime(2021, 3, 1, tzinfo=UTC),
        root=tmp_path / "archive", report_dir=tmp_path / "reports",
    )
    assert calls == [("BTC", "5m", "2021-02")]
    assert outcome["new_months"] == 1
    assert outcome["remote_manifest_names_skipped_without_readback"] == 1
    assert outcome["newly_published"][0]["restore_sha256_verified"] is True
    assert outcome["fully_verified_2021_to_present"] is False
    assert json.loads((tmp_path / "reports/binance-2021-bulk-progress.json").read_text())["live_model_promoted"] is False


@pytest.mark.asyncio
async def test_incomplete_source_month_does_not_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(deep, "months_since_2021", lambda now: ["2021-01"])
    async def existing(drive):
        return set()
    monkeypatch.setattr(deep, "existing_month_keys", existing)
    monkeypatch.setattr(deep, "ingest_binance", lambda *a, **kw:
                        SimpleNamespace(records=8927, manifest_path=Path("pretend")))
    monkeypatch.setattr(deep, "audit_partition", lambda p: {
        "month": "2021-01", "venue": "binance",
        "contract": "BTCUSDT", "interval_minutes": 5,
        "full_calendar_month_verified": False,
    })
    async def unexpected(*args, **kwargs):
        raise AssertionError("incomplete month was published")
    monkeypatch.setattr(deep, "publish", unexpected)
    result = await deep.collect_official_binance(
        drive=object(), assets=("BTC",), max_new=1,
        now=datetime(2021, 2, 1, tzinfo=UTC),
        root=tmp_path / "archive", report_dir=tmp_path / "reports",
    )
    assert result["new_months"] == 0
    assert result["unavailable_or_rejected"][0]["month"] == "2021-01"
    assert result["fully_verified_2021_to_present"] is False
