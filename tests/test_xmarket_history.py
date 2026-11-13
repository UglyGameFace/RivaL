from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.perpetuals.xmarket_inputs import (
    BUNDLE_SCHEMA,
    XmarketInputs,
    canonical,
    parse_points,
)
from parlay_bot.research.xmarket_history import (
    backfill,
    collect_contract,
    combine,
    compare_history,
    source_failure,
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
        assert len(calls) == 1
        assert "/series/KXBTC15M/" in calls[0]


def test_xmarket_kalshi_archived_contract_fallback_preserves_origin(tmp_path, monkeypatch):
    from parlay_bot.research import supplemental_history as source

    at = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
    row = point("kalshi", at)
    identity = {k: row[k] for k in ("asset", "contract_id", "opens_at", "closes_at")}
    calls = []
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    def serve(request):
        calls.append(request.url.path)
        if "/series/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"ticker": identity["contract_id"], "candlesticks": [{
            "end_period_ts": int(at.timestamp()),
            "yes_bid": {k + "_dollars": ".49" for k in ("open", "high", "low", "close")},
            "yes_ask": {k + "_dollars": ".52" for k in ("open", "high", "low", "close")},
            "price": None}]})
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        rows, publication = collect_contract(
            client, source="kalshi", identity=identity, root=tmp_path, now=NOW)
    assert len(calls) == 2
    assert "/series/KXBTC15M/" in calls[0]
    assert "/historical/markets/" in calls[1]
    assert len(rows) == 1
    assert rows[0]["origin"] == "backfill"
    assert rows[0]["price"] == .52
    assert json.loads(publication.manifest.path.read_text())["xmarket_contract"] == identity


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
async def test_weekly_xmarket_source_restore_runs_once_for_all_horizons(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module

    checked, through = [], NOW.replace(second=0, microsecond=0)
    live = [point(s, through - timedelta(minutes=1)) for s in ("polymarket", "kalshi")]
    result = {"samples": live, "live_venues": ["bitget", "okx"],
              "verification": {"polymarket": {"status": "unverified"},
                               "kalshi": {"status": "unverified"}},
              "errors": [{"source": "kalshi", "failure_code": "http_429"}],
              "receipts": [{"file_id": "original-drive-document", "sha256": "f" * 64}]}

    async def acquire(drive, *, asset, through, cache, max_windows):
        assert asset == "BTC" and max_windows == 96
        checked.append(through)
        return result

    monkeypatch.setattr(module, "weekly_inputs", acquire)
    cache = tmp_path / "xmarket-inputs"
    first = await module.weekly_inputs_once(object(), asset="BTC", through=through, cache=cache)
    second = await module.weekly_inputs_once(object(), asset="BTC", through=through, cache=cache)
    assert checked == [through], "5/15/60m must not repeat source history acquisition"
    assert first == second and first["samples"] == live
    assert first["errors"] == result["errors"]
    assert first["receipts"] == result["receipts"]
    assert len(list((tmp_path / "xmarket-training-snapshots").glob("BTC-*.json"))) == 1
    # A new frozen model input clock cannot reuse an older snapshot.
    await module.weekly_inputs_once(
        object(), asset="BTC", through=through + timedelta(minutes=5), cache=cache)
    assert len(checked) == 2


@pytest.mark.asyncio
async def test_corrupt_xmarket_cached_source_fails_closed_not_reacquired(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module

    through = NOW.replace(second=0, microsecond=0)
    calls = []
    async def acquire(drive, **kwargs):
        calls.append(True)
        return {"samples": [point("polymarket", through - timedelta(minutes=1))],
                "verification": {}, "errors": [], "receipts": [], "live_venues": []}

    monkeypatch.setattr(module, "weekly_inputs", acquire)
    cache = tmp_path / "xmarket-inputs"
    await module.weekly_inputs_once(object(), asset="BTC", through=through, cache=cache)
    snapshot = next((tmp_path / "xmarket-training-snapshots").glob("BTC-*.json"))
    doc = json.loads(snapshot.read_text())
    doc["samples"][0]["price"] = 0.91
    snapshot.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        await module.weekly_inputs_once(object(), asset="BTC", through=through, cache=cache)
    assert calls == [True]


@pytest.mark.asyncio
async def test_failed_first_xmarket_source_restore_is_retried_not_cached(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module

    calls = []
    async def acquire(drive, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("synthetic source outage")
        return {"samples": [], "verification": {}, "errors": [],
                "receipts": [], "live_venues": []}

    monkeypatch.setattr(module, "weekly_inputs", acquire)
    through = NOW.replace(second=0, microsecond=0)
    with pytest.raises(RuntimeError, match="source outage"):
        await module.weekly_inputs_once(
            object(), asset="BTC", through=through, cache=tmp_path / "data")
    assert not list(tmp_path.rglob("*.json"))
    receipt = await module.weekly_inputs_once(
        object(), asset="BTC", through=through, cache=tmp_path / "data")
    assert len(calls) == 2
    assert receipt["verification"] == {}


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



@pytest.mark.asyncio
async def test_daily_export_retries_exact_frozen_bytes_and_retires_snapshot(learning_store):
    """The upload can finish while its database acknowledgment fails."""
    from parlay_bot.perpetuals.xmarket_inputs import XmarketInputs

    inputs = XmarketInputs(learning_store)
    at = NOW.replace(second=0, microsecond=0) - timedelta(days=7)
    inputs.record([point(source, at) for source in ("polymarket", "kalshi")])
    inputs.note_venues([("bitget", "BTCUSDT", "USDT"),
                        ("okx", "BTC-USDT-SWAP", "USDT")], now=NOW)
    attempts = []

    class InterruptedDrive:
        async def ensure_research_output(self, **kwargs):
            artifact = kwargs["artifact"]
            attempts.append((artifact.file_name, artifact.path.read_bytes(),
                             kwargs["batch_id"]))
            if len(attempts) == 1:
                raise OSError("simulated interruption before export receipt")
            return SimpleNamespace(file_id="verified-original")

    drive = InterruptedDrive()
    with pytest.raises(OSError, match="interruption"):
        await inputs.publish_days(drive, now=NOW)
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM rival_xmarket_export_staging"
                            ).fetchone()["count"] == 1
        assert conn.execute("SELECT COUNT(*) AS count FROM rival_xmarket_exports"
                            ).fetchone()["count"] == 0

    # A later retry has a different clock and no recent venue heartbeat.
    # It must publish the identical original bytes and content-address.
    assert await inputs.publish_days(drive, now=NOW + timedelta(hours=2)) == 1
    assert attempts[0] == attempts[1]
    original = json.loads(attempts[0][1])
    assert original["live_venues"] == ["bitget", "okx"]
    assert original["created_at"] == NOW.isoformat()
    with learning_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM rival_xmarket_export_staging"
                            ).fetchone()["count"] == 0
        assert conn.execute("SELECT drive_file_id FROM rival_xmarket_exports"
                            ).fetchone()["drive_file_id"] == "verified-original"
    assert await inputs.publish_days(drive, now=NOW + timedelta(hours=3)) == 0
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_daily_export_rejects_modified_staging_before_drive_upload(learning_store):
    from parlay_bot.perpetuals.xmarket_inputs import XmarketInputs

    inputs = XmarketInputs(learning_store)
    at = NOW.replace(second=0, microsecond=0) - timedelta(days=7)
    inputs.record([point("polymarket", at)])
    inputs._staged_day_bundle("BTC", at.date().isoformat(), now=NOW)
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_xmarket_export_staging SET payload_json='{}' "
                     "WHERE asset=? AND day=?", ("BTC", at.date().isoformat()))

    class ForbiddenDrive:
        async def ensure_research_output(self, **kwargs):
            pytest.fail("corrupted pending evidence must never reach Drive")

    with pytest.raises(ValueError, match="staged export bytes changed"):
        await inputs.publish_days(ForbiddenDrive(), now=NOW)



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
    assert receipt["status"] == "waiting_for_inputs"
    assert receipt["source_coverage"]["expected_scopes"] == 8
    assert receipt["source_coverage"]["fresh_scopes"] == 1
    assert len(receipt["source_coverage"]["missing"]) == 7
    assert receipt["shadow_rows_24h"] == 0
    assert receipt["input_sources_24h"][0]["points"] == 1
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        inputs.record([point(source, NOW.replace(second=0, microsecond=0), asset=asset)
                       for source in ("polymarket", "kalshi")])
    complete = _xmarket_progress(learning_store, NOW)
    assert complete["status"] == "waiting_for_fit"
    assert complete["source_coverage"]["fresh_scopes"] == 8
    stale = _xmarket_progress(learning_store, NOW + timedelta(minutes=21))
    assert stale["status"] == "waiting_for_inputs"
    assert len(stale["source_coverage"]["stale"]) == 8
    from tests.xmarket_fixtures import valid_doc
    XmarketModelSet(learning_store).import_document("fit", json.dumps(valid_doc()).encode())
    imported = _xmarket_progress(learning_store, NOW)
    assert imported["status"] == "imported_no_shadow_rows"
    assert imported["shadow_rows_24h"] == 0
    assert imported["imported_scopes"][0]["imported_artifacts"] == 1


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "XRP"])
def test_pm_repeated_final_sample_keeps_native_cadence_and_original_provider_hash(asset):
    import hashlib

    at = NOW.replace(minute=5, second=0, microsecond=0) - timedelta(hours=1)
    row = point("polymarket", at, asset=asset)
    identity = {k: row[k] for k in
                ("asset", "contract_id", "opens_at", "closes_at", "token_id", "slug")}
    first = {"t": int(at.timestamp()), "p": .6}
    last = {"t": int((at + timedelta(minutes=5)).timestamp()), "p": .7}
    payload = {"history": [first, last, *[dict(last) for _ in range(5)]]}
    points = parse_points("polymarket", payload, identity=identity, observed_at=NOW)
    assert [p["price"] for p in points] == [.6, .7]
    assert {p["fidelity_minutes"] for p in points} == {5}
    assert {p["provider_sha256"] for p in points} == {hashlib.sha256(canonical(payload)).hexdigest()}
    conflicting = {"history": [first, last, {**last, "p": .8}]}
    with pytest.raises(ValueError, match="conflicting"):
        parse_points("polymarket", conflicting, identity=identity, observed_at=NOW)
    with pytest.raises(ValueError, match="reversed"):
        parse_points("polymarket", {"history": [last, first]}, identity=identity, observed_at=NOW)
    with pytest.raises(ValueError, match="price point"):
        parse_points("polymarket", {"history": [{**first, "p": True}]},
                     identity=identity, observed_at=NOW)


@pytest.mark.asyncio
async def test_pm_live_capture_records_valid_response_with_repeated_final_sample(learning_store):
    inputs = XmarketInputs(learning_store)
    opened = NOW.replace(minute=0, second=0, microsecond=0)
    at = opened + timedelta(minutes=5)
    p = point("polymarket", at)
    notice = SimpleNamespace(horizon=15, issued=opened, expires=opened + timedelta(minutes=15),
        asset="BTC", slug=p["slug"], condition_id=p["contract_id"], up_token_id=p["token_id"],
        quote_at=at, up_bid=.52, up_ask=.54)
    sample = {"t": int(at.timestamp()), "p": .61}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"history": [sample, dict(sample)]}))) as client:
        assert await inputs.capture_polymarket(notice, client, now=at + timedelta(minutes=1)) == 1
    assert inputs.recent("BTC", at=at, now=at + timedelta(minutes=1))[0]["price"] == .61


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


@pytest.mark.asyncio
async def test_completed_day_export_is_not_blocked_by_failed_records_backup(monkeypatch, caplog):
    caplog.set_level("INFO")
    from parlay_bot.discord_app import bot as module
    from parlay_bot.perpetuals import records_backup

    events = []
    class Backup:
        def __init__(self, *args):
            pass
        async def run_once(self):
            events.append("backup")
            raise RuntimeError("synthetic records-backup failure")

    client = SimpleNamespace(store=object(), research_drive=object(),
                             research_loop_drive=object(), closed=False)
    async def ready():
        return None
    async def advance(seconds):
        if seconds == 3600:
            client.closed = True
    async def publish(self, drive):
        events.append("xmarket_export")
        return 1

    client.wait_until_ready = ready
    client.is_closed = lambda: client.closed
    monkeypatch.setattr(records_backup, "RecordsBackup", Backup)
    monkeypatch.setattr(XmarketInputs, "publish_days", publish)
    monkeypatch.setattr(module.asyncio, "sleep", advance)
    monkeypatch.setattr(module.host_memory, "snapshot", lambda: SimpleNamespace(pressure=False))
    await module.RivalDiscordClient._records_loop(client)
    assert events == ["xmarket_export", "backup"]
    assert "XMarket completed-day source exports published=1" in caplog.text
    assert "RivaL records backup failed" in caplog.text


@pytest.mark.asyncio
async def test_live_receipt_identifies_pending_and_published_completed_days(learning_store):
    from parlay_bot.perpetuals.learning_health_receipt import _xmarket_progress
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
    from parlay_bot.perpetuals.xmarket_model import XmarketModelSet

    inputs = XmarketInputs(learning_store)
    inputs.initialize()
    XmarketModelSet(learning_store).initialize()
    ShadowModelCompetition(learning_store).initialize()
    # A complete UTC day is eligible only after the configured 3-hour delay.
    prior = (NOW - timedelta(days=1)).replace(hour=12, minute=5, second=0, microsecond=0)
    latest = NOW.replace(second=0, microsecond=0)
    inputs.record([point(source, prior) for source in ("polymarket", "kalshi")])
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        inputs.record([point(source, latest, asset=asset)
                       for source in ("polymarket", "kalshi")])
    before = _xmarket_progress(learning_store, NOW)
    assert before["status"] == "waiting_for_completed_day_export"
    assert before["export_coverage"]["recent_completed_days_pending"] == 1
    assert before["export_coverage"]["pending_by_asset"][0]["asset"] == "BTC"
    assert before["export_coverage"]["published"] == []

    class Drive:
        async def ensure_research_output(self, **kwargs):
            return SimpleNamespace(file_id="verified-test-output")
    assert await inputs.publish_days(Drive(), now=NOW) == 1
    after = _xmarket_progress(learning_store, NOW)
    assert after["status"] == "waiting_for_fit"
    assert after["export_coverage"]["recent_completed_days_pending"] == 0
    assert after["export_coverage"]["published"][0]["latest_exported_day"] == prior.date().isoformat()
    assert after["export_coverage"]["does_not_prove_independent_drive_restore"] is True


@pytest.mark.asyncio
async def test_optional_export_failure_does_not_misreport_a_successful_records_backup(monkeypatch, caplog):
    from parlay_bot.discord_app import bot as module
    from parlay_bot.perpetuals import records_backup

    saved = []
    class Backup:
        def __init__(self, *args):
            pass
        async def run_once(self):
            saved.append(True)
    client = SimpleNamespace(store=object(), research_drive=object(),
                             research_loop_drive=object(), closed=False)
    async def ready():
        return None
    async def advance(seconds):
        if seconds == 3600:
            client.closed = True
    async def unavailable(self, drive):
        raise ValueError("synthetic optional export failure")
    client.wait_until_ready = ready
    client.is_closed = lambda: client.closed
    monkeypatch.setattr(records_backup, "RecordsBackup", Backup)
    monkeypatch.setattr(XmarketInputs, "publish_days", unavailable)
    monkeypatch.setattr(module.asyncio, "sleep", advance)
    monkeypatch.setattr(module.host_memory, "snapshot", lambda: SimpleNamespace(pressure=False))
    await module.RivalDiscordClient._records_loop(client)
    assert saved == [True]
    assert "XMarket input export deferred" in caplog.text
    assert "RivaL records backup failed" not in caplog.text


def test_fidelity_reports_why_a_provider_cannot_be_verified():
    at = NOW.replace(second=0, microsecond=0)
    live = [point(source, at - timedelta(minutes=i))
            for source in ("polymarket", "kalshi") for i in range(300)]
    no_export = compare_history([], [{**p, "origin": "backfill"} for p in live])
    assert all(c["blocked_reason"] == "no_prospective_export"
               and c["live_feature_bars"] == 0 and c["history_feature_bars"] == 60
               for c in no_export.values())
    no_history = compare_history(live, [])
    assert all(c["blocked_reason"] == "no_independent_history"
               and c["live_feature_bars"] == 60 and c["history_feature_bars"] == 0
               for c in no_history.values())
    original = compare_history(live, [{**p, "origin": "backfill"} for p in live])
    assert all(c["blocked_reason"] is None and c["paired_points"] == 60
               and c["status"] == "verified" for c in original.values())


def test_source_failure_reports_actionable_non_secret_http_cause():
    from parlay_bot.research.free_history import HistoryImportError

    request = httpx.Request("GET", "https://example.invalid/path?token=PRIVATE")
    response = httpx.Response(429, request=request, text="PRIVATE")
    cause = httpx.HTTPStatusError("PRIVATE", request=request, response=response)
    wrapped = HistoryImportError("PRIVATE")
    wrapped.__cause__ = cause
    record = source_failure("kalshi", "backfill", wrapped, window_close=NOW)
    assert record == {
        "source": "kalshi", "stage": "backfill", "error": "HistoryImportError",
        "failure_code": "http_429", "retryable": True,
        "window_close": NOW.isoformat(),
    }
    assert "PRIVATE" not in json.dumps(record)
    invalid = source_failure("polymarket", "overlap", ValueError("PRIVATE"))
    assert invalid["failure_code"] == "contract_identity_or_schema"
    assert invalid["retryable"] is False


def test_historical_kalshi_discovery_uses_supported_pagination_once(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    last_closed = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC)
    windows = [(last_closed - timedelta(minutes=15 * (i + 1)),
                last_closed - timedelta(minutes=15 * i)) for i in range(2)]
    listed = [
        {"ticker": "KXBTC15M-" + start.strftime("%y%m%d%H%M"),
         "open_time": start.isoformat(), "close_time": end.isoformat()}
        for start, end in windows
    ]
    paths, collected = [], []

    def provider(client, url, params, **kwargs):
        paths.append((url, dict(params)))
        assert kwargs["timeout"] == 8
        assert kwargs["rate_limit_retries"] == 2
        if url.endswith("/markets") and "/historical/" not in url:
            return {"markets": []}
        assert url.endswith("/historical/markets")
        assert "min_close_ts" not in params and "max_close_ts" not in params
        assert params["series_ticker"] == "KXBTC15M"
        if "cursor" not in params:
            return {"markets": [listed[0]], "cursor": "second-page"}
        assert params["cursor"] == "second-page"
        return {"markets": [listed[1]], "cursor": ""}

    def gamma(client, url, params, **kwargs):
        assert "/events/slug/" in url
        return {"found": True}

    def identity(raw, *, asset, opens):
        row = point("polymarket", opens + timedelta(minutes=5), asset=asset)
        return {k: row[k] for k in (
            "asset", "contract_id", "opens_at", "closes_at", "token_id", "slug")}

    def collect(client, *, source, identity, root, now, fidelity=1):
        collected.append((source, identity["opens_at"]))
        return [point(source, timestamp(identity["opens_at"]) + timedelta(minutes=5),
                      origin="backfill")], SimpleNamespace()

    from parlay_bot.research.xmarket_features import timestamp
    monkeypatch.setattr(module, "_json_get", gamma)
    monkeypatch.setattr(module, "kalshi_public_json", provider)
    monkeypatch.setattr(module, "_identity_pm", identity)
    monkeypatch.setattr(module, "collect_contract", collect)

    history, publications, cursor, errors = module.backfill(
        None, asset="BTC", bundles=[], root=tmp_path, before=NOW,
        max_windows=2)
    assert cursor == last_closed - timedelta(minutes=30)
    assert not errors and len(history) == len(publications) == 4
    assert [s for s, _ in collected] == ["polymarket", "kalshi"] * 2
    archive_calls = [p for url, p in paths if url.endswith("/historical/markets")]
    assert len(archive_calls) == 2
    assert "cursor" not in archive_calls[0]
    assert archive_calls[1]["cursor"] == "second-page"


def test_historical_kalshi_incomplete_pages_fail_closed(monkeypatch):
    from parlay_bot.research import xmarket_history as module

    seen = []
    def never_finishes(client, url, params, **kwargs):
        seen.append(params.get("cursor"))
        return {"markets": [], "cursor": "next"}
    monkeypatch.setattr(module, "kalshi_public_json", never_finishes)
    with pytest.raises(ValueError, match="cursor repeated"):
        module._archived_kalshi_windows(None, series="KXBTC15M")
    assert seen == [None, "next"]



def test_missing_native_windows_advance_and_remain_repairable(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    close = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC)
    previous = close - timedelta(minutes=15)
    pending = {"polymarket": set(), "kalshi": set()}
    collected = []
    available = {previous}
    def gamma(client, url, params, **kwargs):
        opened = close - timedelta(minutes=15)
        if opened.isoformat() in url:
            raise AssertionError("unexpected source URL")
        return {"found": True}
    def identity(event, *, asset, opens):
        if opens + timedelta(minutes=15) not in available:
            raise ValueError("exact native contract absent")
        row = point("polymarket", opens + timedelta(minutes=5), asset=asset)
        return {k: row[k] for k in (
            "asset", "contract_id", "opens_at", "closes_at", "token_id", "slug"
        )}
    def kalshi(client, url, params, **kwargs):
        candidate = previous if params.get("max_close_ts") == int(previous.timestamp()) + 1 else close
        if candidate not in available:
            return {"markets": []}
        start = candidate - timedelta(minutes=15)
        return {"markets": [{
            "ticker": "KXBTC15M-" + start.strftime("%y%m%d%H%M"),
            "open_time": start.isoformat(), "close_time": candidate.isoformat(),
        }]}
    def collect(client, *, source, identity, root, now, fidelity=1):
        collected.append(source)
        return [point(source, module.timestamp(identity["opens_at"]) + timedelta(minutes=5),
                      origin="backfill")], SimpleNamespace()

    monkeypatch.setattr(module, "_json_get", gamma)
    monkeypatch.setattr(module, "_identity_pm", identity)
    monkeypatch.setattr(module, "kalshi_public_json", kalshi)
    monkeypatch.setattr(module, "_archived_kalshi_windows", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "collect_contract", collect)

    history, publications, cursor, errors = module.backfill(
        None, asset="BTC", bundles=[], root=tmp_path, before=NOW,
        max_windows=2, pending=pending, retry_gaps=0,
    )
    assert cursor == close - timedelta(minutes=30), (
        "an absent contract must not permanently strand earlier native windows"
    )
    assert len(history) == len(publications) == 2
    assert collected == ["polymarket", "kalshi"]
    assert pending == {
        "polymarket": {close.isoformat()}, "kalshi": {close.isoformat()}
    }
    assert {(e["source"], e["window_close"]) for e in errors} == {
        ("polymarket", close.isoformat()), ("kalshi", close.isoformat())
    }

    # An independently reverified historical window clears only its own
    # source-specific outstanding gaps, without altering a training label.
    available.add(close)
    _, _, same_cursor, repaired_errors = module.backfill(
        None, asset="BTC", bundles=[], root=tmp_path, before=cursor,
        max_windows=0, pending=pending, retry_gaps=2,
    )
    assert same_cursor == cursor and repaired_errors == []
    assert pending == {"polymarket": set(), "kalshi": set()}


def test_rate_limit_preserves_cursor_and_records_missing_identity(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module
    from parlay_bot.research.free_history import HistoryImportError

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    close = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC)
    def transient(client, url, params, **kwargs):
        response = httpx.Response(429, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("rate limited", request=response.request,
                                    response=response)
    monkeypatch.setattr(module, "_json_get", transient)
    monkeypatch.setattr(module, "kalshi_public_json", transient)
    pending = {"polymarket": set(), "kalshi": set()}
    _, _, cursor, errors = module.backfill(
        None, asset="BTC", bundles=[], root=tmp_path, before=NOW,
        max_windows=5, pending=pending, retry_gaps=0,
    )
    assert cursor == close
    assert len(errors) == 2
    assert all(e["retryable"] and e["failure_code"] == "http_429" for e in errors)
    assert all(gaps == {close.isoformat()} for gaps in pending.values())


@pytest.mark.asyncio
async def test_backfill_checkpoint_persists_zero_row_progress_and_gap_recovery(
    tmp_path, monkeypatch,
):
    from parlay_bot.research import xmarket_history as module

    now = NOW.replace(second=0, microsecond=0)
    checkpoints, observed, calls = [], [], []
    class Drive:
        async def read_verified_research_reports(self, **kwargs):
            return [(str(i), doc) for i, doc in enumerate(checkpoints)], None
        async def ensure_research_output(self, **kwargs):
            doc = kwargs["artifact"].path.read_bytes()
            checkpoints.append(doc)
            return SimpleNamespace(file_id=str(len(checkpoints)))

    def acquire(client, *, asset, bundles, root, before, max_windows, pending,
                retry_gaps=12, days=90):
        calls.append(before)
        observed.append({s: sorted(pending[s]) for s in pending})
        if len(calls) == 1:
            pending["kalshi"].add(before.isoformat())
        else:
            pending["kalshi"].clear()
        return [], [], before - timedelta(minutes=15), []

    monkeypatch.setattr(module, "backfill", acquire)
    drive = Drive()
    await module.weekly_inputs(drive, asset="BTC", through=now,
                               cache=tmp_path / "cache", max_windows=1)
    await module.weekly_inputs(drive, asset="BTC", through=now,
                               cache=tmp_path / "cache", max_windows=1)
    assert calls[1] == calls[0] - timedelta(minutes=15)
    assert observed == [
        {"polymarket": [], "kalshi": []},
        {"polymarket": [], "kalshi": [calls[0].isoformat()]},
    ]
    assert len(checkpoints) == 2, "checkpoint even when source rows are absent"
    assert all(json.loads(p)["backfill_checkpoint"] is True for p in checkpoints)
    assert json.loads(checkpoints[1])["backfill_pending"] == {
        "polymarket": [], "kalshi": []
    }



def test_backfill_never_requests_a_future_native_contract(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module
    from parlay_bot.research.free_history import HistoryImportError

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    calls = []

    def provider(client, url, params, **kwargs):
        calls.append((url, params))
        if "/events/slug/" in url:
            raise HistoryImportError("not available")
        return {"markets": []}

    monkeypatch.setattr(module, "_json_get", provider)
    monkeypatch.setattr(module, "kalshi_public_json", provider)
    _, _, cursor, errors = backfill(
        None, asset="BTC", bundles=[], root=tmp_path,
        before=NOW + timedelta(hours=2), max_windows=1)
    expected = datetime.fromtimestamp(int(NOW.timestamp()) // 900 * 900, UTC)
    assert cursor == expected
    assert len(calls) == 3  # One PM lookup and the bounded live/archive Kalshi pair
    assert {e["source"] for e in errors} == {"polymarket", "kalshi"}
    assert all(e["window_close"] == expected.isoformat() for e in errors)


def test_one_failed_overlap_contract_does_not_hide_other_contracts(tmp_path, monkeypatch):
    from parlay_bot.research import xmarket_history as module
    from parlay_bot.research.free_history import HistoryImportError

    at = NOW.replace(second=0, microsecond=0)
    rows = [point("polymarket", at - timedelta(minutes=i)) for i in (0, 15)]
    bundle = {"schema": BUNDLE_SCHEMA, "asset": "BTC", "samples": rows}
    calls = []

    def collect(client, *, source, identity, root, now, fidelity=1):
        calls.append(identity["contract_id"])
        if len(calls) == 1:
            raise HistoryImportError("first contract unavailable")
        return [{**rows[1], "origin": "backfill"}], SimpleNamespace()

    monkeypatch.setattr(module, "collect_contract", collect)
    history, publications, _, errors = backfill(
        None, asset="BTC", bundles=[bundle], root=tmp_path,
        before=NOW, max_windows=0)
    assert len(calls) == 2
    assert len(history) == 1 and len(publications) == 1
    assert len(errors) == 1
    assert errors[0]["stage"] == "overlap"
    assert errors[0]["failure_code"] == "history_integrity"
