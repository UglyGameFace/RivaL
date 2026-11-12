"""Readable model names must not merge distinct designs or invent missing metadata."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from parlay_bot.discord_app import trial_board
from parlay_bot.discord_app.research_dashboard import _ModelSelect
from parlay_bot.discord_app.status_card import family_words, prediction_lines
from parlay_bot.perpetuals import model_lab


@pytest.fixture(autouse=True)
def designs(monkeypatch):
    monkeypatch.setattr(model_lab, "_GENOMES", {})
    monkeypatch.setattr(model_lab, "_CLOSED", set())


def _remember(genome, horizon=5):
    family = model_lab.family_of(genome, horizon)
    model_lab._GENOMES[family] = model_lab.canonical(genome, horizon)
    return family


def _info(n):
    return {"days": 2, "targets": n, "forward_gain": .002}


def test_screenshot_models_have_names_based_on_their_actual_inputs_and_speed():
    main = dict(model_lab.SEEDS[0])
    slow = {**main, "lr": .01, "l2": .01}
    volume = {**main, "inputs": ("momentum", "volume")}
    trend = dict(model_lab.SEEDS[2])
    families = [_remember(g) for g in (main, slow, volume, trend)]
    assert [f.rsplit("_", 1)[-1][:6] for f in families] == ["d91792", "7350e7", "24a17f", "b8a8b1"]
    assert [trial_board.short_name(f) for f in families] == [
        "All signals · steady", "All signals · slow", "Price + volume · steady",
        "Trend price + volatility · fast"]
    text = trial_board.table(trial_board.fold([
        ("hyperliquid", "BTC", {f: _info(n) for f, n in zip(families, (215, 152, 134, 19))})], []))
    assert all(trial_board.short_name(f) in text for f in families)
    assert all(len(line) <= trial_board.PHONE_WIDTH for line in text.splitlines())
    assert all(code not in text for code in ("Lab ", "d91792", "7350e7", "24a17f", "b8a8b1"))
    assert all(str(n) in text for n in (215, 152, 134, 19))


def test_similar_names_keep_separate_evidence_and_meaningful_settings():
    first = _remember({**model_lab.SEEDS[0], "l2": .0001})
    second = _remember({**model_lab.SEEDS[0], "l2": .01})
    rows = trial_board.fold([("okx", "BTC", {first: _info(101), second: _info(202)})], [])
    assert len(rows) == 2
    assert {r.results for r in rows} == {101, 202}
    assert len({r.name for r in rows}) == 2
    assert {r.name.rsplit(" · ", 1)[-1] for r in rows} == {"light pull", "strong pull"}
    picker = _ModelSelect([first, second], [second])
    assert {o.value for o in picker.options} == {first, second}
    assert len({o.label for o in picker.options}) == 2
    assert all("lab_" not in o.label and len(o.description) <= 100 for o in picker.options)
    assert next(o for o in picker.options if o.value == second).default
    messages = prediction_lines([(f"shadow-progress:{f}",
        f"📊 {f}: day 2/21 · {n}/150 targets · gain +0.002")
        for f, n in ((first, 101), (second, 202))])
    assert "2 model groups" in messages[0]
    assert "light pull" in "\n".join(messages) and "strong pull" in "\n".join(messages)


def test_cold_name_lookup_reads_validated_saved_design_without_creating_tables(learning_store):
    family = model_lab.family_of(model_lab.SEEDS[0], 5)
    model_lab.load_family_designs(learning_store, [family])
    assert trial_board.short_name(family) == "Design unavailable"
    # The read above must not initialize the lab or a second storage path.
    with learning_store.connect() as conn:
        conn.execute(model_lab.GENOME_SCHEMA)
        conn.execute("INSERT INTO rival_lab_genomes "
            "(family,horizon_minutes,genome_json,status,opened_at) VALUES (?,?,?,?,?)",
            (family, 5, json.dumps(model_lab.canonical(model_lab.SEEDS[0], 5)),
             "active", datetime.now(UTC).isoformat()))
    model_lab.load_family_designs(learning_store, [family])
    assert trial_board.short_name(family) == "All signals · steady"
    assert family_words(family) == "All signals · steady (5-min)"
    model_lab._GENOMES.clear()
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_lab_genomes SET genome_json=? WHERE family=?",
                     (json.dumps(model_lab.canonical(model_lab.SEEDS[2], 5)), family))
    model_lab.load_family_designs(learning_store, [family])
    assert trial_board.short_name(family) == "Design unavailable"


def test_missing_designs_never_collapse_or_expose_hashes():
    families = ["lab_5m_aaaaaaaaaa", "lab_5m_bbbbbbbbbb"]
    rows = trial_board.fold([("okx", "BTC", {f: _info(50) for f in families})], [])
    assert len(rows) == 2
    assert {r.family for r in rows} == set(families)
    assert all(r.name == "Design unavailable" for r in rows)


def test_retired_legacy_watch_names_still_describe_the_saved_design():
    from parlay_bot.discord_app.model_detail import model_embed
    from parlay_bot.discord_app.model_watch import compare_embed

    _remember(model_lab.SEEDS[0])
    old = "Lab d91792"
    assert trial_board.short_name(old) == "All signals · steady"
    assert "All signals" in model_embed(None, asset="BTC", name=old).title
    text = str(compare_embed({old: None}, asset="BTC").to_dict())
    assert "All signals" in text and old not in text
    assert trial_board.short_name("Lab 000000") == "Design unavailable"


def test_full_phone_table_and_picker_keep_disambiguated_names_within_discord_limits():
    base = {**model_lab.SEEDS[0], "regime": "quiet",
            "inputs": ("momentum", "volatility", "oscillators")}
    variants = [base,
        {**base, "shape": "linear", "warm": "zero", "l2": .01, "shrink": .5},
        {**base, "shape": "linear", "l2": .0001, "shrink": 1.},
        {**base, "warm": "zero", "l2": .01, "shrink": 1.}]
    families = [_remember(g) for g in variants]
    entries = {f: _info(12345) for f in families}
    entries.update({f: _info(12345) for f in (
        "regime_v1", "regime_live_v1", "regime_live_fast_v1", "cross_v1", "cross_all_v1",
        "calib_incumbent_v1", "archive_v1", "gbm_v1", "volcal_v1", "xmarket_v1")})
    rows = trial_board.fold([("okx", "BTC", entries)], [])
    text = trial_board.table(rows)
    assert len(rows) == 14
    assert len(text) <= 1024
    assert max(len(line) for line in text.splitlines()) <= 34
    picker = _ModelSelect(families, [])
    assert len({o.label for o in picker.options}) == 4
    assert all(len(o.label) <= 100 for o in picker.options)


def test_similar_designs_across_all_timeframes_fit_picker_labels_without_losing_identity():
    base = {**model_lab.SEEDS[0], "regime": "quiet",
            "inputs": ("momentum", "volatility", "oscillators")}
    designs = [base,
        {**base, "shape": "linear", "warm": "zero", "l2": .01, "shrink": .5},
        {**base, "shape": "linear", "l2": .0001, "shrink": 1.},
        {**base, "warm": "zero", "l2": .01, "shrink": 1.}]
    families = [_remember(g, h) for h in (5, 15, 60) for g in designs]
    picker = _ModelSelect(families, [])
    assert len(picker.options) == len({o.label for o in picker.options}) == 12
    assert all(len(o.label) <= 100 for o in picker.options)
    assert {o.value for o in picker.options} == set(families)
