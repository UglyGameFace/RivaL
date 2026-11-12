from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.perpetuals.xmarket_inputs import (
    BUNDLE_SCHEMA,
    XmarketInputs,
)
from parlay_bot.research.xmarket_history import (
    collect_contract,
    combine,
    compare_history,
    validate_bundle,
    weekly_inputs,
)
from tests.xmarket_fixtures import NOW, point


def test_native_input_record_is_immutable_and_received_time_is_enforced(learning_store):
    inputs = XmarketInputs(learning_store)
    at = NOW.replace(second=0, microsecond=0)
    p = point("polymarket", at)
    assert inputs.record([p]) == 1
    assert inputs.record([p]) == 0
    assert inputs.recent("BTC", at=at, now=at) == []
    assert len(inputs.recent("BTC", at=at, now=NOW)) == 1
    with pytest.raises(ValueError, match="revised"):
        inputs.record([{**p, "price": .9}])
    assert inputs.recent("BTC", at=at, now=NOW)[0]["price"] == p["price"]


def test_overlap_counts_unique_points_and_coarse_backfill_does_not_pass():
    at = NOW.replace(second=0, microsecond=0)
    live = [point(source, at - timedelta(minutes=i)) for source in ("polymarket", "kalshi") for i in range(300)]
    history = [{**p, "origin": "backfill"} for p in live]
    checks = compare_history(live, history)
    assert all(c["status"] == "verified" and c["paired_points"] == 60 for c in checks.values())
    assert compare_history(live, [history[0]] * 100)["polymarket"]["status"] == "unverified"
    coarse = [p for i, p in enumerate(history) if i % 15 == 0]
    assert compare_history(live, coarse)["polymarket"]["status"] == "unverified"
    revised = [{**p, "price": .9} for p in history]
    assert compare_history(live, revised)["kalshi"]["mismatch_rate"] == 1
    bundle = {"schema": BUNDLE_SCHEMA, "asset": "BTC", "samples": live}
    assert combine([bundle], history=revised)["samples"] == []


@pytest.mark.parametrize("source", ["polymarket", "kalshi"])
def test_existing_supplement_ingest_binds_contract_and_preserves_provider_hash(tmp_path, source):
    at = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    row = point(source, at)
    identity = {k: row[k] for k in ("asset", "contract_id", "opens_at", "closes_at", "token_id", "slug") if k in row}
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if source == "polymarket":
            return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .6}]})
        if "/historical/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"ticker": identity["contract_id"], "candlesticks": [{
            "end_period_ts": int(at.timestamp()),
            "yes_bid": {k + "_dollars": ".59" for k in ("open", "high", "low", "close")},
            "yes_ask": {k + "_dollars": ".61" for k in ("open", "high", "low", "close")},
            "price": None, "volume_fp": "4.00", "open_interest_fp": "6.00"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, publication = collect_contract(client, source=source, identity=identity,
                                              root=tmp_path, now=NOW)
    assert rows[0]["price"] == (.6 if source == "polymarket" else .61)
    assert rows[0]["origin"] == "backfill"
    assert rows[0]["provider_sha256"] != rows[0]["normalized_sha256"]
    metadata = json.loads(publication.manifest.path.read_text())
    assert metadata["xmarket_contract"] == identity
    if source == "kalshi":
        assert len(calls) == 2
        assert "/series/KXBTC15M/" in calls[1]


@pytest.mark.asyncio
async def test_live_capture_reuses_clients_and_native_price_not_normalized_ask_ratio(learning_store):
    inputs = XmarketInputs(learning_store)
    opened = NOW.replace(minute=0, second=0, microsecond=0)
    clock = opened + timedelta(minutes=6)
    at = opened + timedelta(minutes=5)
    p = point("polymarket", at)
    notice = SimpleNamespace(horizon=15, issued=opened, expires=opened + timedelta(minutes=15),
        asset="BTC", slug=p["slug"], condition_id=p["contract_id"], up_token_id=p["token_id"],
        quote_at=at, up_bid=.52, up_ask=.54, down_ask=.7)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"history": [{"t": int(at.timestamp()), "p": .61}]}))) as client:
        assert await inputs.capture_polymarket(notice, client, now=clock) == 1
        assert await inputs.capture_polymarket(notice, client, now=clock) == 0
    row = inputs.recent("BTC", at=at, now=clock)[0]
    assert row["price"] == .61
    assert row["price"] != notice.up_ask / (notice.up_ask + notice.down_ask)


def test_bundle_cannot_mix_assets_or_substitute_normalized_hash():
    p = point("polymarket", NOW.replace(second=0, microsecond=0))
    bundle = {"schema": BUNDLE_SCHEMA, "asset": "ETH", "samples": [p]}
    with pytest.raises(ValueError, match="cross-asset"):
        validate_bundle(bundle)
    p["provider_sha256"] = p["normalized_sha256"]
    with pytest.raises(ValueError, match="normalized"):
        validate_bundle({**bundle, "asset": "BTC"})


@pytest.mark.asyncio
async def test_weekly_restore_keeps_real_overlaps_and_fidelity_receipts(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module
    at = NOW.replace(second=0, microsecond=0)
    live = [point(source, at - timedelta(minutes=i)) for source in ("polymarket", "kalshi") for i in range(300)]
    history = [{**p, "origin": "backfill", "observed_at": NOW.isoformat()} for p in live]
    bundle = {"schema": BUNDLE_SCHEMA, "asset": "BTC", "samples": live,
              "created_at": NOW.isoformat(), "live_venues": ["bitget", "okx"]}
    class Drive:
        async def read_verified_research_reports(self, **kwargs):
            assert kwargs["name_prefix"] == "xmarket-inputs-BTC-"
            return [("live", json.dumps(bundle).encode())], None
        async def ensure_research_output(self, **kwargs):
            assert kwargs["kind"] == "report"
            return SimpleNamespace(file_id="saved")
    monkeypatch.setattr(module, "backfill", lambda *a, **kw: (history, [], at, []))
    data = await weekly_inputs(Drive(), asset="BTC", through=NOW, cache=tmp_path, max_windows=0)
    assert len(data["samples"]) == 600
    assert data["live_venues"] == ["bitget", "okx"]
    assert all(c["status"] == "verified" for c in data["verification"].values())
    assert list(tmp_path.glob("xmarket-inputs-*.json"))


@pytest.mark.asyncio
async def test_verified_daily_publish_is_idempotent_and_recovers_an_older_backlog(learning_store):
    inputs = XmarketInputs(learning_store)
    at = NOW.replace(second=0, microsecond=0) - timedelta(days=7)
    inputs.record([point(s, at) for s in ("polymarket", "kalshi")])
    inputs.note_venues([("bitget", "BTCUSDT", "USDT"), ("okx", "BTC-USDT-SWAP", "USDT")], now=NOW)
    documents = []
    class Drive:
        async def ensure_research_output(self, **kwargs):
            documents.append(json.loads(kwargs["artifact"].path.read_text()))
            return SimpleNamespace(file_id="saved")
    assert await inputs.publish_days(Drive(), now=NOW) == 1
    assert await inputs.publish_days(Drive(), now=NOW) == 0
    assert len(documents[0]["samples"]) == 2
    assert documents[0]["day"] == at.date().isoformat()


def test_read_only_host_receipt_distinguishes_missing_capture_fit_and_rows(learning_store):
    from parlay_bot.perpetuals.learning_health_receipt import _xmarket_progress
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.perpetuals.xmarket_model import XmarketModelSet

    assert _xmarket_progress(learning_store, NOW)["status"] == "not_initialized"
    inputs = XmarketInputs(learning_store)
    inputs.initialize()
    XmarketModelSet(learning_store).initialize()
    ShadowModelCompetition(learning_store).initialize()
    assert _xmarket_progress(learning_store, NOW)["status"] == "waiting_for_inputs"
    inputs.record([point("polymarket", NOW.replace(second=0, microsecond=0))])
    receipt = _xmarket_progress(learning_store, NOW)
    assert receipt["status"] == "waiting_for_fit"
    assert receipt["shadow_rows_24h"] == 0
    assert receipt["input_sources_24h"][0]["points"] == 1
    from tests.xmarket_fixtures import valid_doc
    XmarketModelSet(learning_store).import_document("fit", json.dumps(valid_doc()).encode())
    imported = _xmarket_progress(learning_store, NOW)
    assert imported["status"] == "imported_no_shadow_rows"
    assert imported["shadow_rows_24h"] == 0
    assert imported["imported_scopes"][0]["imported_artifacts"] == 1


def test_identical_price_paths_for_different_tokens_keep_different_archive_ids(tmp_path):
    at = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    identities = []
    for asset, digit in (("BTC", "1"), ("ETH", "2")):
        row = point("polymarket", at, asset=asset)
        row.update(contract_id="0x" + digit * 64, token_id=digit * 40)
        identities.append({k: row[k] for k in
                           ("asset", "contract_id", "opens_at", "closes_at", "token_id", "slug")})
    def handler(request):
        return httpx.Response(200, json={"history": [{"t": int(at.timestamp()), "p": .6}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publications = [collect_contract(client, source="polymarket", identity=identity,
                                         root=tmp_path, now=NOW)[1] for identity in identities]
    assert publications[0].history.sha256 == publications[1].history.sha256
    assert publications[0].batch_id != publications[1].batch_id


@pytest.mark.asyncio
async def test_optional_input_capture_failure_does_not_break_existing_formula_heartbeat(monkeypatch):
    from parlay_bot.discord_app.bot import RivalDiscordClient
    from tests.test_formula_loop_heartbeat import HEARTBEAT_SLUG, _client, _notice, _run_one_pass

    async def broken_capture(self, notices):
        raise ValueError("synthetic source failure")
    monkeypatch.setattr(RivalDiscordClient, "_capture_xmarket_history", broken_capture)
    assert await _run_one_pass(monkeypatch, _client([_notice("BTC", 5)])) == [HEARTBEAT_SLUG]
