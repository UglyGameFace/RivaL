"""Round-5 batch 10 (P2s) pins.

(i) promotion.py says twelve slots (4 assets x 3 horizons), not six.
(ii) evaluate.py --asset accepts XRP.
(iii) The legacy bot.py quote-profit observer covers XRP, not just BTC/ETH/SOL.
(iv) Gemini is gone as an AI-provider label/column/log-string: the analytics
    payload key is historical_external_ai, the heartbeat column is auto_ai_ok
    (with a rename migration for pre-existing databases), and the retired
    Gemini log strings/comments are reworded. (Gemini the *exchange* venue in
    spot_reference.py is a different thing and is untouched.)
(v) No unattributed skips: every skip call in tests/ carries a
    non-empty reason (verified against the 775dfd3 suite: -rs showed 125
    skips, all with Postgres reasons, zero reason-less).
(vi) The audioop DeprecationWarning from discord.py is filtered in the
    pytest config (discord.py 2.7.1 is the latest release and still imports
    audioop at player.py:30; the bot runs on Python 3.11, where it is not
    deprecated).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --- (i) promotion slots ---------------------------------------------------

def test_promotion_docstring_says_twelve_slots():
    from parlay_bot.perpetuals import promotion
    from parlay_bot.perpetuals.markets import ASSETS, HORIZONS

    assert len(ASSETS) * len(HORIZONS) == 12
    assert "twelve independent slots" in promotion.__doc__
    assert "six independent slots" not in promotion.__doc__


# --- (ii) evaluate.py XRP ---------------------------------------------------

def test_evaluate_cli_accepts_xrp(monkeypatch):
    from parlay_bot.perpetuals.evaluate import main as evaluate_main

    monkeypatch.setattr(
        sys, "argv",
        ["evaluate", "--provider", "hyperliquid", "--asset", "XRP"],
    )
    # argparse must accept XRP (a SystemExit(2) here would mean "invalid
    # choice"); the run then stops at the PostgreSQL requirement, which is
    # the expected sandbox outcome.
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        evaluate_main()


def test_evaluate_cli_still_rejects_unknown_asset(monkeypatch):
    from parlay_bot.perpetuals.evaluate import main as evaluate_main

    monkeypatch.setattr(
        sys, "argv",
        ["evaluate", "--provider", "hyperliquid", "--asset", "DOGE"],
    )
    with pytest.raises(SystemExit) as exc:
        evaluate_main()
    assert exc.value.code == 2


# --- (iii) legacy bot.py XRP observer ----------------------------------------

def test_legacy_quote_profit_observer_covers_xrp():
    code = (ROOT / "src" / "parlay_bot" / "discord_app" / "bot.py").read_text()
    anchor = code.index("self.quote_profit.observe")
    window = code[max(0, anchor - 700):anchor]
    assert 'if asset not in ("BTC", "ETH", "SOL", "XRP"):' in window
    assert "Three assets" not in code


# --- (iv) Gemini label/column/log-string cleanup ------------------------------

def test_analytics_payload_key_is_historical_external_ai_not_gemini(tmp_path):
    from parlay_bot.perpetuals.analytics import analytics_snapshot
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    market = PerpetualMarketStore(store)
    try:
        report = analytics_snapshot(market, asset="SOL")
    finally:
        store.close()
    assert "gemini" not in report
    assert report["historical_external_ai"]["directional_accuracy"] is None


def test_retired_gemini_log_strings_are_gone():
    code = (ROOT / "src" / "parlay_bot" / "perpetuals" / "autonomous.py").read_text()
    assert "RivaL Gemini outcome settlement failed" not in code
    assert "RivaL automatic Gemini research cycle failed" not in code
    assert "Global Gemini API spacing" not in code
    assert "auto_ai_ok" in code
    audit = (ROOT / "tools" / "discloud_llm_edge_audit.py").read_text()
    assert "rival_gemini_auto_enabled" not in audit


def test_heartbeat_column_is_auto_ai_ok_on_fresh_database(tmp_path):
    from parlay_bot.perpetuals.autonomous import AutonomousResearch
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "fresh.sqlite")
    market = PerpetualMarketStore(store)
    system = AutonomousResearch(market, ai=None, venues=())
    try:
        system.initialize()
        with store.connect() as conn:
            columns = store.database.column_names(conn, "perp_learning_cycle_health")
        assert "auto_ai_ok" in columns
        assert "gemini_ok" not in columns
    finally:
        store.close()


def test_heartbeat_column_renamed_in_place_on_legacy_database(tmp_path):
    from parlay_bot.perpetuals.autonomous import AutonomousResearch
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from parlay_bot.storage.hot import SQLiteHotStore

    store = SQLiteHotStore(tmp_path / "legacy.sqlite")
    market = PerpetualMarketStore(store)
    try:
        with store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE perp_learning_cycle_health (
                    slot_utc TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    math_issued INTEGER NOT NULL,
                    math_settled INTEGER NOT NULL,
                    ai_issued INTEGER NOT NULL,
                    math_ok INTEGER NOT NULL,
                    ai_settlement_ok INTEGER NOT NULL,
                    model_review_ok INTEGER NOT NULL,
                    gemini_ok INTEGER NOT NULL
                );
                INSERT INTO perp_learning_cycle_health VALUES (
                    '2026-11-06T00:00:00+00:00', '2026-11-06T00:00:00+00:00',
                    '2026-11-06T00:05:00+00:00', 0, 0, 0, 1, 1, 1, 0
                );
                """
            )
        system = AutonomousResearch(market, ai=None, venues=())
        system.initialize()
        with store.connect() as conn:
            columns = store.database.column_names(conn, "perp_learning_cycle_health")
            row = conn.execute(
                "SELECT auto_ai_ok FROM perp_learning_cycle_health"
            ).fetchone()
        assert "auto_ai_ok" in columns
        assert "gemini_ok" not in columns
        assert int(row["auto_ai_ok"]) == 0  # pre-rename history preserved
        assert system.learning_heartbeat()["auto_ai_ok"] == 0
    finally:
        store.close()


# --- (v) no unattributed skips -------------------------------------------------

def test_every_pytest_skip_carries_a_reason():
    bare = re.compile(r"pytest\.skip\(\s*\)")
    empty_reason = re.compile(r"pytest\.skip\(\s*[\"']\s*[\"']")
    offenders = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text()
        if bare.search(text) or empty_reason.search(text):
            offenders.append(path.name)
    assert offenders == []


# --- (vi) audioop warning filtered ----------------------------------------------

def test_audioop_deprecation_warning_is_filtered_in_pytest_config():
    text = (ROOT / "pyproject.toml").read_text()
    assert "ignore:'audioop' is deprecated:DeprecationWarning" in text
