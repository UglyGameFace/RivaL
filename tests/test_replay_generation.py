"""Phase-2 generation grid: frozen fit spec, embargo, intake, board.

Synthetic fixtures in the phase-1 style: the walk-forward embargo is
proven with a poison row whose outcome is observed only after the
score day opens, and the conditioning legs are pinned against
hand-computed call blocks.
"""
import hashlib
import json
from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.home import workbench_text
from parlay_bot.perpetuals import replay_generation as gen
from parlay_bot.perpetuals.research_hypotheses import MODEL as LLM_MODEL
from parlay_bot.storage.hot import SQLiteHotStore

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _features(ratio=0.7, frac=0.25, overround=0.05):
    up = ratio
    down = 1.0 - ratio + overround
    return (up, down, up / (up + down), up + down - 1.0,
            0.01, 0.01, frac, 1.0, up - down, 0.0)


def _row(*, day_offset=0, ratio=0.7, label=1, p=0.6, baseline=0.5,
         horizon=15, price=0.5, fee_rate=0.0, frac=0.25, overround=0.05,
         observed_offset=timedelta(hours=2), minute_offset=0):
    # minute_offset: distinct market per row (see test_replay_workbench).
    issued = T0 + timedelta(days=day_offset, minutes=minute_offset)
    return {
        "slug": f"btc-updown-15m-{int(issued.timestamp())}-{day_offset}",
        "asset": "BTC", "horizon": horizon, "issued_at": issued,
        "day": issued.date().isoformat(), "probability_up": p,
        "market_baseline": baseline,
        "direction": "UP" if label else "DOWN", "label": label,
        "outcome_observed_at": issued + observed_offset,
        "fee_rate": fee_rate, "up_price": price, "down_price": price,
        "features": _features(ratio, frac, overround),
    }


def _signal_rows(days=21, per_day=150, *, price=0.5, flip_every=0):
    """Label follows the book ratio; the baseline is flat 0.5."""
    rows = []
    for d in range(days):
        for i in range(per_day):
            ratio = 0.7 if i % 2 == 0 else 0.3
            label = 1 if ratio > 0.5 else 0
            if flip_every and (i + 1) % flip_every == 0:
                label = 1 - label
            rows.append(_row(day_offset=d, ratio=ratio, label=label,
                             price=price, minute_offset=i))
    return rows


# --- The batch logistic fit ---

def test_fit_logistic_recovers_the_signal_feature():
    samples = []
    for i in range(400):
        ratio = 0.7 if i % 2 == 0 else 0.3
        samples.append((_features(ratio), 1 if ratio > 0.5 else 0))
    weights = gen.fit_logistic(samples, (2,))
    assert weights[1] > 1.0
    assert gen.predict_logistic(weights, (2,), _features(0.7)) > 0.8
    assert gen.predict_logistic(weights, (2,), _features(0.3)) < 0.2


def test_fit_logistic_ignores_an_unrelated_feature():
    samples = [(_features(0.7 if i % 2 == 0 else 0.3), i % 2)
               for i in range(400)]
    weights = gen.fit_logistic(samples, (6,))  # timing only: constant 0.25
    assert gen.predict_logistic(weights, (6,), _features(0.7)) < 0.6


# --- Screen G-A ---

def test_mask_screen_passes_a_real_signal():
    result = gen.screen_generation_masks(_signal_rows())
    cand = result["candidates"]["gen-book-all"]
    assert cand["verdict"] == "PASS"
    assert cand["score"]["rows"] >= 1000
    assert cand["legs"]["paired_brier"] is True


def test_mask_screen_kills_when_calls_lose_money():
    # Same signal, but the book is so expensive that correct calls
    # still lose after the price: leg 4 fails, family verdict KILL.
    rows = _signal_rows(price=0.95, flip_every=10)
    cand = gen.screen_generation_masks(rows)["candidates"]["gen-book-all"]
    assert cand["verdict"] == "KILL"
    assert cand["legs"]["paired_log_loss"] is True
    assert cand["legs"]["both_halves_call_net"] is False


def test_mask_screen_not_measurable_below_fit_floor():
    rows = _signal_rows(days=10, per_day=10)
    cand = gen.screen_generation_masks(rows)["candidates"]["gen-book-all"]
    assert cand["status"] == "NOT_MEASURABLE"
    assert cand["verdict"] is None


def test_walk_forward_embargo_excludes_late_observed_outcomes():
    base = []
    for d in range(8):
        for i in range(50):
            ratio = 0.7 if i % 2 == 0 else 0.3
            base.append(_row(day_offset=d, ratio=ratio,
                             label=1 if ratio > 0.5 else 0))
    poison = _row(day_offset=6, ratio=0.99, label=0,
                  observed_offset=timedelta(days=3))
    cand = {"id": "gen-test", "factory": "lab", "screen": "G-A",
            "tier": "B", "display": "t", "mask": (2,), "horizon": None,
            "mechanism": "embargo probe"}
    clean = gen.screen_generation_masks(base, (cand,))["candidates"]["gen-test"]
    poisoned = gen.screen_generation_masks(
        base + [poison], (cand,))["candidates"]["gen-test"]
    assert poisoned["score"] == clean["score"]
    assert poisoned["coverage"]["scope_rows"] == clean["coverage"]["scope_rows"] + 1


# --- Screen G-B ---

def _conditioning_rows(subset_win=True, pooled_win=False):
    rows = []
    for d in range(8):
        for i in range(40):
            rows.append(_row(day_offset=d, label=1, frac=0.75))  # subset
        for i in range(40):
            rows.append(_row(day_offset=d, label=1 if pooled_win else 0,
                             frac=0.25))
    if not subset_win:
        for row in rows:
            if row["features"][6] >= 0.5:
                row["label"] = 0
                row["direction"] = "DOWN"
    return rows


def test_conditioning_passes_only_when_the_pocket_is_isolated():
    cand = gen.screen_conditioning(_conditioning_rows())["candidates"]["cond-late-window"]
    assert cand["verdict"] == "PASS"
    assert cand["subset"]["mean_net"] == 0.5
    assert cand["pooled"]["mean_net"] == 0.0


def test_conditioning_kills_when_the_whole_scope_wins_too():
    cand = gen.screen_conditioning(
        _conditioning_rows(pooled_win=True))["candidates"]["cond-late-window"]
    assert cand["verdict"] == "KILL"


def test_conditioning_not_measurable_below_call_floor():
    rows = [_row(day_offset=d, label=1, frac=0.75) for d in range(8)]
    cand = gen.screen_conditioning(rows)["candidates"]["cond-late-window"]
    assert cand["status"] == "NOT_MEASURABLE"


# --- Screen G-C: estate loaders ---

def _estate_store(tmp_path):
    store = SQLiteHotStore(tmp_path / "estate.sqlite")
    with store.connect() as conn:
        conn.execute("CREATE TABLE rival_options_surface_snapshots"
                     " (snapshot_at TEXT, asset TEXT)", ())
        conn.execute("CREATE TABLE rival_threshold_market_snapshots"
                     " (snapshot_at TEXT, slug TEXT)", ())
        conn.execute("CREATE TABLE rival_kalshi_threshold_snapshots"
                     " (snapshot_at TEXT, ticker TEXT)", ())
        conn.execute("CREATE TABLE rival_settlement_index_ticks"
                     " (venue TEXT, source_at TEXT)", ())
        for i in range(3):
            conn.execute("INSERT INTO rival_options_surface_snapshots"
                         " VALUES (?, 'BTC')", (f"2026-11-20T0{i}:00:00Z",))
        conn.execute("INSERT INTO rival_threshold_market_snapshots"
                     " VALUES ('2026-11-20T04:00:00Z', 'x')", ())
        conn.execute("INSERT INTO rival_kalshi_threshold_snapshots"
                     " VALUES ('2026-11-20T04:00:00Z', 'y')", ())
        conn.execute("INSERT INTO rival_settlement_index_ticks"
                     " VALUES ('polymarket', '2026-11-20T03:00:00Z')", ())
        conn.execute("INSERT INTO rival_settlement_index_ticks"
                     " VALUES ('kalshi', '2026-11-20T05:00:00Z')", ())
    return store


def test_estate_coverage_and_not_measurable(tmp_path):
    store = _estate_store(tmp_path)
    result = gen.screen_estate(store)
    assert result["coverage"]["options_surface"]["rows"] == 3
    assert result["coverage"]["settlement_index_ticks"]["kalshi"]["rows"] == 1
    assert result["coverage"]["settled_threshold_outcomes"] == 0
    for cand in result["candidates"].values():
        assert cand["status"] == "NOT_MEASURABLE"
        assert cand["cause"] == "no_settled_threshold_outcomes"


# --- Screen G-D: LLM-mask intake ---

def _proposal_row(asset="BTC", hypothesis="feature_subset_v2",
                  indices=(2, 3), slot="2026-11-05T00:00:00+00:00",
                  attempted="2026-11-05T00:05:00+00:00", tamper=False):
    record = {"asset": asset, "slot_utc": slot, "model": LLM_MODEL,
              "evidence_sha256": "e" * 64, "hypothesis": hypothesis,
              "venues": ["polymarket"]}
    if hypothesis == "feature_subset_v2":
        record["feature_indices"] = list(indices)
    digest = hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "asset": asset, "slot_utc": slot, "model": LLM_MODEL,
        "hypothesis": hypothesis,
        "feature_indices_json": (json.dumps(list(indices))
                                 if hypothesis == "feature_subset_v2" else None),
        "rationale": "book shape carries the signal",
        "falsification": "no edge after fees in replay",
        "evidence_sha256": "e" * 64,
        "record_sha256": "0" * 64 if tamper else digest,
        "source_venues_json": json.dumps(["polymarket"]),
        "attempted_at": attempted, "status": "proposed",
    }


def _llm_store(tmp_path, rows):
    store = SQLiteHotStore(tmp_path / "llm.sqlite")
    cols = list(rows[0])
    with store.connect() as conn:
        conn.execute(
            "CREATE TABLE rival_llm_feature_hypotheses ("
            + ", ".join(f"{c} TEXT" for c in cols) + ")", ())
        for row in rows:
            conn.execute(
                f"INSERT INTO rival_llm_feature_hypotheses"
                f" ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                tuple(row[c] for c in cols))
    return store


def test_llm_intake_verifies_dedupes_and_registers(tmp_path):
    rows = [_proposal_row(),
            _proposal_row(attempted="2026-11-06T00:05:00+00:00"),  # dupe
            _proposal_row(asset="ETH", indices=(2, 3, 4, 5, 6, 7, 8, 9)),
            _proposal_row(asset="SOL", tamper=True)]
    store = _llm_store(tmp_path, rows)
    proposals, rejected = gen.load_llm_proposals(store)
    assert len(proposals) == 3
    assert rejected["integrity"] == 1
    registrations = gen.llm_registrations(proposals)
    assert [r["id"] for r in registrations] == [
        "llm-mask-btc-volrange", "llm-mask-eth-wide"]
    # Registrations no longer hard-code a verdict: they carry the
    # owner-approved G-D floors and the grader decides.
    assert all("status" not in r and "cause" not in r
               for r in registrations)
    assert all(r["floors"] == gen.GD_FLOORS for r in registrations)
    assert gen.GD_FLOORS == {"min_rows": 500, "min_days": 7,
                              "min_calls": 100, "min_clusters": 40}
    # Oldest proposal wins the dedupe.
    assert registrations[0]["proposed_at"] == "2026-11-05T00:05:00+00:00"


def test_llm_intake_registers_named_hypotheses(tmp_path):
    store = _llm_store(tmp_path, [_proposal_row(hypothesis="trend_only")])
    proposals, rejected = gen.load_llm_proposals(store)
    assert rejected == {"integrity": 0, "field_rules": 0, "unparseable": 0}
    registrations = gen.llm_registrations(proposals)
    assert registrations[0]["id"] == "llm-mask-btc-trend"
    assert registrations[0]["mask"] == [0, 1]


# --- Results store + board ---

def test_results_roundtrip_and_missing_table(tmp_path):
    store = SQLiteHotStore(tmp_path / "results.sqlite")
    assert gen.read_results_for_board(store) == {"candidates": []}
    entry = {"candidate_id": "gen-book-all", "screen": "G-A",
             "factory": "lab", "display": "Book model · all book signals",
             "status": "EVALUATED", "verdict": "KILL", "graded_rows": 2100,
             "calls": 2000, "days": 14,
             "evaluated_at": "2026-11-20T17:00:00+00:00",
             "detail": {"legs": {}}}
    assert gen.record_results(store, [entry]) == 1
    board = gen.read_results_for_board(store)
    assert board["candidates"][0]["candidate_id"] == "gen-book-all"
    assert board["candidates"][0]["verdict"] == "KILL"
    newer = {**entry, "verdict": "PASS",
             "evaluated_at": "2026-11-21T17:00:00+00:00"}
    gen.record_results(store, [newer])
    board = gen.read_results_for_board(store)
    assert len(board["candidates"]) == 1
    assert board["candidates"][0]["verdict"] == "PASS"


def test_board_read_is_select_only(tmp_path):
    """The performance probe renders every view through connections
    that reject any statement not starting with SELECT/WITH; the
    board read must honour that (no PRAGMA existence probe)."""
    from contextlib import contextmanager

    inner = SQLiteHotStore(tmp_path / "guarded.sqlite")

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            assert sql.strip().split()[0].upper() in ("SELECT", "WITH")
            return self.conn.execute(sql, params)

    class Guarded:
        database = inner.database

        @contextmanager
        def connect(self):
            with inner.connect() as conn:
                yield Connection(conn)

    assert gen.read_results_for_board(Guarded()) == {"candidates": []}
    gen.record_results(inner, [{
        "candidate_id": "gen-book-all", "screen": "G-A", "factory": "lab",
        "display": "Book model · all book signals", "status": "EVALUATED",
        "verdict": "KILL", "graded_rows": 10, "calls": 9, "days": 7,
        "evaluated_at": "2026-11-20T17:00:00+00:00", "detail": {}}])
    board = gen.read_results_for_board(Guarded())
    assert board["candidates"][0]["verdict"] == "KILL"


def test_workbench_text_states():
    assert "Couldn't load" in workbench_text(None)
    assert "No graded candidates yet" in workbench_text({"candidates": []})
    record = {"candidates": [
        {"candidate_id": "a", "display": "Book model · all book signals",
         "status": "EVALUATED", "verdict": "KILL", "graded_rows": 2100,
         "calls": 2000, "days": 14, "evaluated_at": "2026-11-20T17:00:00Z"},
        {"candidate_id": "b", "display": "AI-suggested mask · BTC",
         "status": "NOT_MEASURABLE", "verdict": None, "graded_rows": 0,
         "calls": 0, "days": 0, "evaluated_at": "2026-11-20T17:00:00Z"},
    ]}
    text = workbench_text(record)
    assert "1 killed · 1 not measurable yet" in text
    assert "2,100 graded" in text
    full = workbench_text(record, full=True)
    assert "Killed: Book model · all book signals." in full
    assert len(full) <= 1024


def test_phase1_import_maps_all_four_report_shapes():
    reports = [
        {"screen": "R1", "evaluated_at": "2026-11-20T16:50:00Z",
         "candidates": {"recal-raw": {
             "status": "EVALUATED", "verdict": "KILL",
             "score": {"rows": 20636, "days": 14, "calls": {"n": 7641}},
             "legs": {}}}},
        {"screen": "X1-X5", "evaluated_at": "2026-11-20T16:55:00Z",
         "scopes": {"BTC:60": {
             "screen": "X2", "status": "EVALUATED", "verdict": "KILL",
             "score": {"rows": 4443, "days": 5, "calls": {"n": 120}},
             "artifact_sha256": "abc"}}},
        {"screen": "R3", "evaluated_at": "2026-11-20T16:40:00Z",
         "status": "NOT_MEASURABLE", "verdict": None,
         "pooled": {"rows": 7661, "calls": 7661, "days": 14}},
        {"screen": "R2", "evaluated_at": "2026-11-20T16:41:00Z",
         "status": "DATA-BLOCKED"},
    ]
    entries = gen.import_phase1_reports(reports)
    by_id = {e["candidate_id"]: e for e in entries}
    assert by_id["recal-raw"]["verdict"] == "KILL"
    assert by_id["recal-raw"]["graded_rows"] == 20636
    assert by_id["xmarket-btc-60m-425d49"]["factory"] == "xmarket"
    assert by_id["cb-premium-v1"]["status"] == "NOT_MEASURABLE"
    assert by_id["liq-cascade-v1"]["status"] == "DATA-BLOCKED"


# --- Limbo visibility: every blocked candidate names its blocker ---

def _limbo_store(tmp_path):
    store = SQLiteHotStore(tmp_path / "limbo.sqlite")
    at = "2026-11-20T17:00:00+00:00"
    gen.record_results(store, [
        {"candidate_id": "llm-mask-btc-trend", "screen": "G-D",
         "factory": "lab", "display": "AI-suggested mask · BTC",
         "status": "NOT_MEASURABLE", "verdict": None, "graded_rows": 0,
         "calls": 0, "days": 0, "evaluated_at": at,
         "detail": {"cause": "exchange_features_not_recorded"}},
        {"candidate_id": "opt-implied-btc-weekly", "screen": "G-C",
         "factory": "lab", "display": "Options-implied · BTC weekly",
         "status": "NOT_MEASURABLE", "verdict": None, "graded_rows": 0,
         "calls": 0, "days": 0, "evaluated_at": at,
         "detail": {"cause": "no_settled_threshold_outcomes"}},
        {"candidate_id": "cond-tight-book", "screen": "G-B",
         "factory": "screen", "display": "Main model's calls · tight book only",
         "status": "NOT_MEASURABLE", "verdict": None, "graded_rows": 900,
         "calls": 30, "days": 3, "evaluated_at": at,
         "detail": {"floors": {"calls": {"have": 30, "need": 250},
                               "call_days": {"have": 3, "need": 7}}}},
        {"candidate_id": "liq-cascade-v1", "screen": "R2",
         "factory": "lab", "display": "Liquidation cascade screen",
         "status": "DATA-BLOCKED", "verdict": None, "graded_rows": 0,
         "calls": 0, "days": 0, "evaluated_at": at,
         "detail": {"imported_from": "phase1-report"}},
    ])
    return store


def test_board_blockers_and_progress(tmp_path):
    board = gen.read_results_for_board(_limbo_store(tmp_path))
    by_id = {c["candidate_id"]: c for c in board["candidates"]}
    assert "G-D grader" in by_id["llm-mask-btc-trend"]["blocker"]
    assert "now reads the recorded vectors" in (
        by_id["llm-mask-btc-trend"]["blocker"])
    assert "2026-11-27" in by_id["opt-implied-btc-weekly"]["blocker"]
    assert by_id["cond-tight-book"]["blocker"] == "below its measurement floors"
    assert by_id["cond-tight-book"]["progress"] == "calls 30/250 · days 3/7"
    assert by_id["cond-tight-book"]["floors"]["calls"] == {"have": 30, "need": 250}
    assert "liquidation" in by_id["liq-cascade-v1"]["blocker"]


def test_workbench_text_names_every_blocker(tmp_path):
    record = gen.read_results_for_board(_limbo_store(tmp_path))
    full = workbench_text(record, full=True)
    assert "15 not measurable" not in full  # counts come from the record
    for needle in ("AI-suggested mask · BTC", "now reads the recorded vectors",
                   "Options-implied · BTC weekly", "2026-11-27",
                   "tight book only", "calls 30/250 · days 3/7",
                   "Liquidation cascade screen"):
        assert needle in full, needle
    compact = workbench_text(record)
    assert "calls 30/250 · days 3/7" in compact


def test_r2_import_records_the_reason():
    entries = gen.import_phase1_reports([{
        "screen": "R2", "status": "DATA-BLOCKED",
        "evaluated_at": "2026-11-20T17:00:00+00:00",
        "reason": "no liquidation archive"}])
    assert entries[0]["detail"]["reason"] == "no liquidation archive"


# --- Screen G-D: the grader over the lineage record ---

GD_T0 = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
PERP_MODEL = "prospective-logistic-persistent-v3"

# Table shapes copied from forward_audit._initialize_schema DDL.
_GD_DDL = (
    ("CREATE TABLE perp_forward_audit ("
    " venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,"
    " asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,"
    " model_version TEXT NOT NULL, reference_opened_at TEXT NOT NULL,"
    " reference_close DOUBLE PRECISION NOT NULL, issued_at TEXT NOT NULL,"
    " target_opened_at TEXT NOT NULL,"
    " probability_up DOUBLE PRECISION NOT NULL,"
    " baseline_up DOUBLE PRECISION NOT NULL,"
    " training_evaluated INTEGER NOT NULL, settled_at TEXT,"
    " outcome_up INTEGER, realized_close DOUBLE PRECISION,"
    " model_brier DOUBLE PRECISION, baseline_brier DOUBLE PRECISION,"
    " PRIMARY KEY (venue, contract, quote, horizon_minutes,"
    " model_version, reference_opened_at))"),
    ("CREATE TABLE rival_forward_model_lineage ("
    " venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,"
    " horizon_minutes INTEGER NOT NULL, model_version TEXT NOT NULL,"
    " reference_opened_at TEXT NOT NULL, issued_at TEXT NOT NULL,"
    " weights_sha256 TEXT NOT NULL, feature_sha256 TEXT NOT NULL,"
    " features_json TEXT, training_labels INTEGER NOT NULL,"
    " historical_training_labels INTEGER NOT NULL,"
    " online_training_updates INTEGER NOT NULL,"
    " historical_seed_sha256 TEXT,"
    " PRIMARY KEY (venue, contract, quote, horizon_minutes,"
    " model_version, reference_opened_at))"),
)


def _gd_features(label):
    # Exchange vector (features.feature_vector layout): index 0/1 are
    # the trend pair the trend_only mask reads; the rest are neutral.
    return [2.0 if label else -2.0, 1.0 if label else -1.0,
            0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.1, 0.0]


def _gd_insert(conn, *, issued, label, asset="BTC", contract="BTCUSDT",
               features="recorded", baseline=0.5, horizon=15):
    reference = issued - timedelta(minutes=horizon)
    target = reference + timedelta(minutes=horizon)
    settled = issued + timedelta(minutes=horizon)
    conn.execute(
        "INSERT INTO perp_forward_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?)",
        ("binance", contract, "USDT", asset, horizon, PERP_MODEL,
         reference.isoformat(), 100.0, issued.isoformat(),
         target.isoformat(), 0.6, baseline, 1, settled.isoformat(),
         label, 101.0 if label else 99.0, 0.2, 0.25))
    conn.execute(
        "INSERT INTO rival_forward_model_lineage VALUES (?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?)",
        ("binance", contract, "USDT", horizon, PERP_MODEL,
         reference.isoformat(), issued.isoformat(), "w" * 64, "f" * 64,
         None if features is None else json.dumps(
             _gd_features(label) if features == "recorded" else features),
         100, 50, 10, None))


def _gd_store(tmp_path, *, days, per_day, contracts=50, start=GD_T0):
    store = SQLiteHotStore(tmp_path / "gd.sqlite")
    with store.connect() as conn:
        for ddl in _GD_DDL:
            conn.execute(ddl, ())
        for d in range(days):
            for i in range(per_day):
                issued = start + timedelta(days=d, minutes=i * 10)
                _gd_insert(conn, issued=issued, label=i % 2,
                           contract=f"BTC-{i % contracts}")
    return store


def _gd_registration(asset="BTC", mask=(0, 1)):
    return {"id": f"llm-mask-{asset.lower()}-trend", "factory": "lab",
            "screen": "G-D", "display": f"AI-suggested mask · {asset}",
            "asset": asset, "mask": list(mask),
            "floors": dict(gen.GD_FLOORS)}


def test_gd_loader_excludes_pre_cutoff_and_unrecorded_rows(tmp_path):
    store = _gd_store(tmp_path, days=2, per_day=3)
    with store.connect() as conn:
        # Issued before the features column landed (2026-11-20T22:24Z).
        _gd_insert(conn, issued=datetime(2026, 11, 19, tzinfo=UTC),
                   label=1, contract="BTC-old")
        # Post-cutoff but no recorded vector (NULL features_json).
        _gd_insert(conn, issued=GD_T0 + timedelta(days=5), label=1,
                   contract="BTC-null", features=None)
        # Post-cutoff, recorded, but not settled yet.
        conn.execute(
            "UPDATE perp_forward_audit SET settled_at=NULL,"
            " outcome_up=NULL WHERE contract='BTC-0'", ())
    rows, coverage = gen.load_gd_record(store)
    # 6 recorded+settled rows minus the two unsettled BTC-0 rows
    # (that contract recurs on both days).
    assert coverage["eligible_rows"] == len(rows) == 4
    assert all(r["issued_at"] >= datetime.fromisoformat(
        gen.GD_FEATURES_CUTOFF) for r in rows)
    assert all(len(r["features"]) == gen.EXCHANGE_FEATURE_COUNT == 10
               for r in rows)
    assert {r["cluster"] for r in rows} <= {
        f"binance:BTC-{i}:USDT" for i in range(3)}


def test_gd_below_floor_reports_floor_progress(tmp_path):
    store = _gd_store(tmp_path, days=9, per_day=50, contracts=5)
    rows, _ = gen.load_gd_record(store)
    cand = gen.screen_llm_masks(rows, [_gd_registration()]
                                )["candidates"]["llm-mask-btc-trend"]
    assert cand["status"] == "NOT_MEASURABLE"
    assert cand["verdict"] is None
    assert "cause" not in cand  # record exists: progress, not a cause
    assert cand["floors"] == {
        "rows": {"have": 100, "need": 500},
        "days": {"have": 2, "need": 7},
        "calls": {"have": 100, "need": 100},
        "clusters": {"have": 5, "need": 40}}
    # ...and that progress is what the board surfaces.
    gen.record_results(store, [{
        "candidate_id": "llm-mask-btc-trend", "screen": "G-D",
        "factory": "lab", "display": "AI-suggested mask · BTC",
        "status": cand["status"], "verdict": None,
        "graded_rows": cand["score"]["rows"],
        "calls": cand["score"]["calls"]["n"],
        "days": cand["score"]["days"],
        "evaluated_at": "2026-11-24T17:00:00+00:00",
        "detail": {"floors": cand["floors"]}}])
    board = gen.read_results_for_board(store)
    entry = board["candidates"][0]
    assert entry["blocker"] == "below its measurement floors"
    assert entry["floors"]["rows"] == {"have": 100, "need": 500}
    assert "rows 100/500" in entry["progress"]


def test_gd_zero_post_cutoff_rows_keeps_the_named_cause(tmp_path):
    store = _gd_store(tmp_path, days=3, per_day=5)
    rows, _ = gen.load_gd_record(store)
    cand = gen.screen_llm_masks(
        rows, [_gd_registration(asset="ETH")]
    )["candidates"]["llm-mask-eth-trend"]
    assert cand["status"] == "NOT_MEASURABLE"
    assert cand["cause"] == "no_post_cutoff_lineage_rows"
    assert cand["cause"] == gen.LLM_CAUSE


def test_gd_separable_case_passes_floors_and_legs(tmp_path):
    store = _gd_store(tmp_path, days=18, per_day=60, contracts=50)
    rows, coverage = gen.load_gd_record(store)
    assert coverage["eligible_rows"] == 18 * 60
    cand = gen.screen_llm_masks(rows, [_gd_registration()]
                                )["candidates"]["llm-mask-btc-trend"]
    assert cand["status"] == "EVALUATED"
    assert cand["verdict"] == "PASS"
    assert all(cand["legs"].values())
    assert cand["score"]["rows"] == 11 * 60
    assert cand["score"]["clusters"] == 50
    assert cand["floors"]["rows"] == {"have": 660, "need": 500}


def test_gd_metrics_match_a_second_hand_computation(tmp_path):
    # Recompute the paired metrics from the walk-forward predictions
    # with plain math (no wb helpers) and compare against the block.
    import math as _math

    store = _gd_store(tmp_path, days=10, per_day=40, contracts=8)
    rows, _ = gen.load_gd_record(store)
    cand = gen.screen_llm_masks(rows, [_gd_registration()]
                                )["candidates"]["llm-mask-btc-trend"]
    scope = sorted(rows, key=lambda r: r["issued_at"])
    days = sorted({r["day"] for r in scope})
    weights = None
    deltas_ll, deltas_brier, call_nets = [], [], []
    for day in days[gen.BURN_IN_DAYS:]:
        day_start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        fit = [r for r in scope
               if r["outcome_observed_at"] < day_start][-5000:]
        if len(fit) < gen.GD_FIT_MIN_ROWS:
            continue
        weights = gen.fit_logistic(
            [(r["features"], r["label"]) for r in fit], (0, 1),
            warm=weights)
        for row in (r for r in scope if r["day"] == day):
            g = gen.predict_logistic(weights, (0, 1), row["features"])
            g = min(max(g, 1e-9), 1 - 1e-9)
            y, base = row["label"], row["market_baseline"]
            deltas_ll.append(
                -(_math.log(g) if y else _math.log(1 - g))
                + (_math.log(base) if y else _math.log(1 - base)))
            deltas_brier.append((g - y) ** 2 - (base - y) ** 2)
            price = base if g > 0.5 else 1 - base
            call_nets.append((1.0 if (y == 1) == (g > 0.5) else 0.0)
                              - price)
    score = cand["score"]
    # First score day is fit-floor-skipped (280 fit rows < 300).
    assert score["rows"] == len(deltas_ll) == 80
    assert abs(score["delta_log_loss"]
               - sum(deltas_ll) / len(deltas_ll)) < 1e-12
    assert abs(score["delta_brier"]
               - sum(deltas_brier) / len(deltas_brier)) < 1e-12
    assert score["calls"]["n"] == len(call_nets)
    assert abs(score["calls"]["mean_net"]
               - sum(call_nets) / len(call_nets)) < 1e-12
