"""Replay workbench: frozen criteria, walk-forward integrity, fail-closed.

Synthetic fixtures by design: the maps and verdict logic are pinned
against hand-computed cases, and the walk-forward embargo is proven
with a poison row whose outcome is observed only after the score day
opens -- including it would change the fit, so identical parameters
with and without it prove the purge.
"""
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals import replay_workbench as wb
from parlay_bot.perpetuals.polymarket_learning import after_fee_call_net
from parlay_bot.storage.hot import SQLiteHotStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _features(up=0.5, down=0.5, up_off=0.0, down_off=0.0):
    return [up, down, up / (up + down), up + down - 1.0,
            up_off, down_off, 0.5, 1.0, up - down, up_off - down_off]


def _row(*, day_offset=0, p=0.7, baseline=0.5, label=1, asset="BTC",
         horizon=15, up_price=0.5, down_price=0.5, fee_rate=None,
         observed_offset=timedelta(hours=2), minute_offset=0):
    # minute_offset staggers issuance within the day so each row is a
    # distinct market (distinct slug = distinct cluster); without it
    # every row in a day shared one slug and no cluster floor could
    # ever be exercised.
    issued = T0 + timedelta(days=day_offset, minutes=minute_offset)
    return {
        "slug": f"{asset.lower()}-updown-15m-{int(issued.timestamp())}",
        "asset": asset, "horizon": horizon, "issued_at": issued,
        "day": issued.date().isoformat(), "probability_up": p,
        "market_baseline": baseline,
        "direction": "UP" if label else "DOWN", "label": label,
        "outcome_observed_at": issued + observed_offset,
        "fee_rate": fee_rate, "up_price": up_price,
        "down_price": down_price,
    }


# --- Book pricing: parity with production's after_fee_call_net ---

def test_call_for_matches_production_when_fee_unrecorded():
    features = _features(up=0.6, down=0.45, up_off=0.02, down_off=0.03)
    baseline = 0.6 / 1.05
    issued = datetime(2026, 11, 10, tzinfo=UTC)
    expected = after_fee_call_net(
        probability_up=0.7, direction="UP", features=features,
        market_baseline=baseline, asset="BTC", horizon=15,
        issued_at=issued)
    up_price, down_price = wb.book_prices(features, baseline)
    row = {"asset": "BTC", "horizon": 15, "issued_at": issued,
           "direction": "UP", "fee_rate": None,
           "up_price": up_price, "down_price": down_price}
    got = wb.call_for(row, 0.7)
    assert got is not None and expected is not None
    assert math.isclose(got[0], expected, rel_tol=0, abs_tol=1e-12)


def test_call_for_uses_recorded_fee_rate():
    issued = datetime(2026, 11, 10, tzinfo=UTC)
    row = {"asset": "BTC", "horizon": 15, "issued_at": issued,
           "direction": "UP", "fee_rate": 0.0,
           "up_price": 0.62, "down_price": 0.48}
    net, cost = wb.call_for(row, 0.7)
    assert math.isclose(net, 1.0 - 0.62, abs_tol=1e-12)
    assert math.isclose(cost, 0.62, abs_tol=1e-12)


def test_book_prices_rejects_mismatched_book():
    features = _features(up=0.6, down=0.45)
    assert wb.book_prices(features, 0.5) == (None, None)


def test_no_call_at_exactly_even_probability():
    row = _row(p=0.5)
    assert wb.call_for(row, 0.5) is None


# --- Maps ---

def test_platt_recovers_miscalibration():
    # Model says 0.9/0.1 but the truth is 0.7/0.3: Platt must soften it.
    pairs = [(0.9, 1)] * 700 + [(0.9, 0)] * 300 \
        + [(0.1, 0)] * 700 + [(0.1, 1)] * 300
    a, b = wb.fit_platt(pairs)
    g = wb.apply_platt(0.9, a, b)
    assert 0.6 < g < 0.8
    raw_ll = sum(wb._log_loss(p, y) for p, y in pairs) / len(pairs)
    fit_ll = sum(wb._log_loss(wb.apply_platt(p, a, b), y)
                 for p, y in pairs) / len(pairs)
    assert fit_ll < raw_ll


def test_isotonic_hand_case():
    pairs = [(0.2, 0), (0.2, 1), (0.5, 0), (0.8, 1), (0.8, 1)]
    blocks = wb.fit_isotonic(pairs)
    # PAVA: 0.2 -> 0.5 pooled with 0.5's 0? means: 0.2:0.5, 0.5:0.0
    # violates monotonicity, pools to (0.2..0.5) -> 1/3; 0.8 -> 1.0.
    assert blocks == [(0.5, 1 / 3), (0.8, 1.0)]
    assert wb.apply_isotonic(blocks, 0.3) == 1 / 3
    assert wb.apply_isotonic(blocks, 0.9) == 1.0


def test_shrink_exact():
    assert wb.shrink(0.9, 0.5, 0.25) == 0.6
    assert wb.shrink(0.1, 0.5, 0.5) == 0.3


# --- Walk-forward embargo (design 4.2) ---

def _history(days=9, per_day=200, *, skill=0.8):
    rows = []
    for d in range(days):
        for i in range(per_day):
            label = 1 if (i % 10) < skill * 10 else 0
            p = 0.85 if label else 0.3
            rows.append(_row(day_offset=d, p=p, label=label,
                             minute_offset=i))
    return rows


def test_walk_forward_embargo_excludes_late_outcomes():
    rows = _history()
    poison = _row(day_offset=0, p=0.99, label=0,
                  observed_offset=timedelta(days=30))
    clean = wb.screen_recalibration(rows)
    poisoned = wb.screen_recalibration(rows + [poison])
    # The poison row's outcome is observed long after every score day
    # opens, so no day's Platt parameters may move.
    assert clean["platt_parameters_by_day"] \
        == poisoned["platt_parameters_by_day"]
    # Sanity: the history itself does move the fit away from identity.
    assert clean["platt_parameters_by_day"]
    assert any(abs(a - 1.0) > 0.01 or abs(b) > 0.01
               for a, b in clean["platt_parameters_by_day"].values())


# --- R1 verdict constructions ---

def _skilled_history(days=21, per_day=300):
    rows = []
    for d in range(days):
        for i in range(per_day):
            label = 1 if (i % 5) < 4 else 0  # 80% up
            p = 0.8 if label else 0.2
            rows.append(_row(day_offset=d, p=p, label=label,
                             minute_offset=i))
    return rows


def test_recalibration_pass_when_model_beats_market():
    result = wb.screen_recalibration(_skilled_history())
    raw = result["candidates"]["recal-raw"]
    assert raw["status"] == "EVALUATED"
    assert raw["verdict"] == "PASS"
    assert raw["score"]["delta_log_loss"] < 0
    assert raw["score"]["calls"]["mean_net"] > 0


def test_recalibration_kill_when_model_is_the_market():
    rows = []
    for d in range(21):
        for i in range(200):
            label = 1 if i % 2 == 0 else 0
            rows.append(_row(day_offset=d, p=0.5 + (0.1 if label else -0.1),
                             baseline=0.5 + (0.1 if label else -0.1),
                             label=label, minute_offset=i))
    result = wb.screen_recalibration(rows)
    shrink = result["candidates"]["recal-shrink50"]
    # p == baseline everywhere: shrink is the identity, deltas are 0,
    # leg 1 (CI entirely below 0) cannot pass.
    assert shrink["verdict"] == "KILL"


def test_recalibration_not_measurable_below_floors():
    result = wb.screen_recalibration(_history(days=9, per_day=50))
    raw = result["candidates"]["recal-raw"]
    assert raw["status"] == "NOT_MEASURABLE"
    assert raw["verdict"] is None


# --- X1-X5: value-call rule and eligibility ---

def _shadow_row(*, p=0.65, baseline=0.5, outcome=1, issued=None,
                opened=None, settled=True):
    issued = issued or datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    opened = opened or (issued - timedelta(minutes=5))
    return {"issued_at": issued, "target_opened_at": opened,
            "probability_up": p, "baseline_up": baseline,
            "settled_at": issued + timedelta(hours=1) if settled else None,
            "outcome_up": outcome}


def test_xmarket_value_call_pricing():
    rows = [_shadow_row() for _ in range(10)]
    result = wb.screen_xmarket(rows, asset="BTC", horizon=15)
    calls = result["score"]["calls"]
    assert calls["n"] == 10
    # net = 1 - 0.5 - fee(0.5 @ 0.07 schedule, pre-2026-11-20) = 0.4825
    assert math.isclose(calls["mean_net"], 0.4825, abs_tol=1e-9)
    assert result["pricing"].startswith("IMPLIED-PRICE")


def test_xmarket_exclusions_counted():
    late = _shadow_row(issued=datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
                       opened=datetime(2026, 11, 16, 10, 0, tzinfo=UTC))
    unsettled = _shadow_row(settled=False)
    out_of_range = _shadow_row(p=1.0)
    result = wb.screen_xmarket([late, unsettled, out_of_range],
                               asset="BTC", horizon=15)
    assert result["coverage"]["exclusions"] == {
        "outcome_missing": 1, "probability_out_of_range": 1,
        "issued_after_target_close": 1}
    assert result["status"] == "NOT_MEASURABLE"


def test_xmarket_no_value_call_below_edge():
    rows = [_shadow_row(p=0.53, baseline=0.5) for _ in range(5)]
    result = wb.screen_xmarket(rows, asset="BTC", horizon=15)
    assert result["score"]["calls"]["n"] == 0


# --- R3: premium regimes, no look-ahead ---

def _premium_points(day_offset, premium):
    close = T0 + timedelta(days=day_offset)
    return [(close, premium)]


def test_premium_uses_last_closed_candle_only():
    rows = [_row(day_offset=3, p=0.9, label=1)]
    issued = rows[0]["issued_at"]
    points = [(issued - timedelta(seconds=300), 0.005),
              (issued + timedelta(seconds=300), -0.005)]
    result = wb.screen_premium(rows, points)
    assert result["regimes"]["POS"]["rows"] == 1
    assert result["regimes"]["NEG"]["rows"] == 0


def test_premium_missing_series_excludes():
    rows = [_row(day_offset=0)]
    result = wb.screen_premium(rows, [])
    assert result["coverage"]["excluded_no_premium"] == 1
    assert result["status"] == "NOT_MEASURABLE"


def _regime_history(*, pos_win=0.9, neg_win=0.05, days=14, per_day=40):
    rows, points = [], []
    for d in range(days):
        points.append((T0 + timedelta(days=d) - timedelta(seconds=300),
                       0.002 if d % 2 == 0 else -0.002))
        for i in range(per_day):
            win_rate = pos_win if d % 2 == 0 else neg_win
            label = 1 if (i % 20) < win_rate * 20 else 0
            rows.append(_row(day_offset=d, p=0.9, label=label))
    return rows, points


def test_premium_pass_isolates_profitable_regime():
    rows, points = _regime_history()
    result = wb.screen_premium(rows, points)
    assert result["verdict"] == "PASS"
    assert result["qualifying_regimes"] == ["POS"]
    assert result["pooled"]["mean_net"] <= 0


def test_premium_kill_when_no_regime_qualifies():
    rows, points = _regime_history(pos_win=0.3, neg_win=0.3)
    result = wb.screen_premium(rows, points)
    assert result["verdict"] == "KILL"


# --- Registry integrity ---

def test_registry_entries_carry_mechanisms():
    report = wb.registry_report()
    ids = {c["id"] for c in report["candidates"]}
    assert {"recal-platt", "recal-isotonic", "cb-premium-v1",
            "liq-cascade-v1"} <= ids
    for candidate in report["candidates"]:
        assert candidate["mechanism"].strip()
        assert candidate["factory"] in ("lab", "xmarket", "screen")
    blocked = next(c for c in report["candidates"]
                   if c["id"] == "liq-cascade-v1")
    assert blocked["status"] == "DATA-BLOCKED"


def test_xmarket_registry_matches_frozen_scopes():
    prefixes = {s["sha_prefix"] for s in wb.XMARKET_SCOPES}
    assert prefixes == {"6cf3886d42f7", "425d4990003e", "43e5cce04729",
                        "182bbf474664", "b799ee12dd4b"}
    scopes = {(s["asset"], s["horizon"]) for s in wb.XMARKET_SCOPES}
    assert scopes == {("BTC", 15), ("BTC", 60), ("SOL", 15),
                      ("XRP", 15), ("XRP", 60)}


# --- Premium fetcher: Binance Vision row shapes ---

def test_vision_timestamp_units():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "fetch_coinbase_premium",
        Path(__file__).resolve().parents[1] / "tools"
        / "fetch_coinbase_premium.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Milliseconds (older files) and microseconds (current files)
    # normalize to the same instant.
    assert module.vision_seconds(1762000000000) == 1762000000.0
    assert module.vision_seconds(1762000000000000) == 1762000000.0


# --- Loader: eligibility on a synthetic store ---

def _seed_store(tmp_path):
    store = SQLiteHotStore(tmp_path / "wb.sqlite")
    with store.connect() as conn:
        conn.executescript("""
        CREATE TABLE rival_polymarket_model_forward (
            slug TEXT NOT NULL, model_version TEXT NOT NULL,
            issued_at TEXT NOT NULL, weights_sha256 TEXT NOT NULL,
            probability_up DOUBLE PRECISION NOT NULL,
            market_baseline DOUBLE PRECISION NOT NULL,
            fee_rate DOUBLE PRECISION, up_average_ask DOUBLE PRECISION,
            down_average_ask DOUBLE PRECISION,
            PRIMARY KEY(slug, model_version));
        CREATE TABLE rival_polymarket_observations (
            slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
            asset TEXT NOT NULL, horizon INTEGER NOT NULL,
            resolution_source TEXT NOT NULL, expires_at TEXT NOT NULL,
            observed_at TEXT NOT NULL, features_json TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            learned_at TEXT);
        CREATE TABLE rival_polymarket_ai_outcomes (
            slug TEXT NOT NULL, condition_id TEXT NOT NULL,
            direction TEXT NOT NULL, resolved_at TEXT NOT NULL,
            observed_at TEXT NOT NULL);
        """)
    return store


def _insert_settled(store, *, slug, p, baseline, features, direction="UP",
                    issued):
    import json as _json
    expires = issued + timedelta(minutes=15)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_observations VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?)",
            (slug, "0xabc", "BTC", 15, "chainlink", expires.isoformat(),
             issued.isoformat(), _json.dumps(features), "e" * 64, "{}", None))
        conn.execute(
            "INSERT INTO rival_polymarket_model_forward VALUES"
            " (?,?,?,?,?,?,?,?,?)",
            (slug, wb.MODEL, issued.isoformat(), "w" * 64, p, baseline,
             None, None, None))
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?)",
            (slug, "0xabc", direction, expires.isoformat(),
             (expires + timedelta(minutes=5)).isoformat()))


def test_loader_eligibility_and_book_verification(tmp_path):
    from parlay_bot.perpetuals.polymarket_learning import MODEL
    store = _seed_store(tmp_path)
    issued = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    _insert_settled(store, slug="ok-1", p=0.7, baseline=0.5,
                    features=_features(), issued=issued)
    _insert_settled(store, slug="bad-book", p=0.7, baseline=0.5,
                    features=_features(up=0.6, down=0.45), issued=issued)
    rows, coverage = wb.load_live_record(store)
    assert [r["slug"] for r in rows] == ["ok-1"]
    assert coverage["settled_rows_seen"] == 2
    assert coverage["unpriceable_book"] == 1
    assert rows[0]["label"] == 1
    assert MODEL == wb.MODEL


# --- Corrected template legs (2026-11-24 defect sweep) ---

def _synth_obs(*, day, idx, dll=-0.01, db=-0.003, call_net=0.02,
               cluster=None, asset="BTC", horizon=60):
    issued = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(
        days=day, minutes=idx)
    return {"issued_at": issued, "day": issued.date().isoformat(),
            "delta_log_loss": dll, "delta_brier": db,
            "call_net": call_net,
            "call_cost": 0.5 if call_net is not None else None,
            "cluster": cluster if cluster is not None else f"m-{day}-{idx}",
            "asset": asset, "horizon": horizon}


def _verdict_for(observations, **floors):
    block = wb._paired_block(observations)
    floors.setdefault("min_rows", 100)
    floors.setdefault("min_days", 4)
    floors.setdefault("min_calls", 100)
    floors.setdefault("min_clusters", 40)
    return block, wb._four_leg_verdict(block, **floors)


def test_brier_leg_is_the_corrected_ci_form():
    # X2's shape: delta_brier -0.00283, tight across days. The old
    # -0.005 point leg killed it; the corrected CI leg passes it.
    obs = [_synth_obs(day=d, idx=i, db=-0.00283, dll=-0.0057)
           for d in range(8) for i in range(250)]
    block, verdict = _verdict_for(obs)
    assert block["delta_brier"] == -0.00283
    assert block["delta_brier_ci95"]["upper"] < 0
    assert verdict["status"] == "EVALUATED"
    assert verdict["legs"]["paired_brier"] is True


def test_brier_leg_rejects_a_noisy_point_estimate_the_old_leg_passed():
    # Mean delta_brier -0.006 (would pass the old -0.005 point leg)
    # but alternating days make the cluster CI cross zero.
    obs = []
    for d in range(8):
        db = -0.030 if d % 2 == 0 else 0.018
        obs += [_synth_obs(day=d, idx=i, db=db) for i in range(250)]
    block, verdict = _verdict_for(obs)
    assert block["delta_brier"] <= -0.005
    assert block["delta_brier_ci95"]["upper"] > 0
    assert verdict["legs"]["paired_brier"] is False


def test_one_call_second_half_is_not_measurable():
    # The audit F4 reproduction: 200 calls in the first half, exactly
    # one call in the second. Pooled floors are met; the per-half
    # floor must refuse a verdict.
    obs = []
    for i in range(400):
        call_net = 0.02 if i < 200 else None
        if i == 399:
            call_net = 0.48
        obs.append(_synth_obs(day=i // 50, idx=i, call_net=call_net))
    block, verdict = _verdict_for(obs)
    assert block["calls"]["n"] == 201
    assert verdict["status"] == "NOT_MEASURABLE"
    assert verdict["floors"]["calls_second_half"] == {"have": 1,
                                                      "need": 25}


def test_cluster_floor_detects_concentration():
    # 400 graded rows on ONE market: plenty of calls, one independent
    # unit. The cluster floor must refuse a verdict.
    obs = [_synth_obs(day=i // 50, idx=i, cluster="one-market")
           for i in range(400)]
    block, verdict = _verdict_for(obs)
    assert block["clusters"] == 1
    assert verdict["status"] == "NOT_MEASURABLE"
    assert verdict["floors"]["clusters"] == {"have": 1, "need": 40}


def test_stratified_reporting_shows_the_split():
    obs = ([_synth_obs(day=d, idx=i, db=-0.02, asset="BTC")
            for d in range(4) for i in range(100)]
           + [_synth_obs(day=d, idx=i, db=0.01, asset="ETH")
              for d in range(4, 8) for i in range(100)])
    block, _ = _verdict_for(obs)
    cuts = block["stratified"]["asset"]
    assert cuts["BTC"]["delta_brier"] == -0.02
    assert cuts["ETH"]["delta_brier"] == 0.01
    assert cuts["BTC"]["rows"] + cuts["ETH"]["rows"] == block["rows"]


def test_family_size_widens_cluster_intervals():
    obs = [_synth_obs(day=d, idx=i, dll=-0.01 - 0.002 * d)
           for d in range(8) for i in range(50)]
    single = wb._paired_block(obs)
    family = wb._paired_block(obs, family_size=5)
    width = lambda ci: ci["upper"] - ci["lower"]
    assert width(family["delta_log_loss_ci95"]) \
        > width(single["delta_log_loss_ci95"])
