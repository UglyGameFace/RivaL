"""Post-close Polymarket reference resolution for the convergence collector.

Gamma publishes ``eventMetadata.priceToBeat`` only after a window
closes (measured live 2026-11-19), so the in-window notices the
collector records never carry it -- after PR #707 the production proof
landed Kalshi references 134/134 but Polymarket 0/112. The collector
therefore resolves each just-closed contract's published level with
one dedicated fetch and stores it as its own record; these tests pin
that path end to end: helper -> reference store -> evaluator loader.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.bot import _resolve_closed_pm_reference
from parlay_bot.perpetuals.convergence_evaluation import (
    evaluate_windows,
    load_windows,
)
from parlay_bot.perpetuals.convergence_quotes import ConvergenceQuotes
from parlay_bot.storage.hot import SQLiteHotStore

OPEN = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)  # post-fee-change window
CLOSE = OPEN + timedelta(minutes=15)
NOW = datetime(2026, 11, 22, 0, 0, tzinfo=UTC)
SLUG = "eth-updown-15m-1"
# The exact shape Gamma served for a closed window in the live probe
# (alongside a context_requires_regen marker): a plain float.
PUBLISHED = 100_000.0


def _notice():
    return SimpleNamespace(
        asset="ETH", slug=SLUG, issued=OPEN, expires=CLOSE)


def _quotes(tmp_path):
    quotes = ConvergenceQuotes(SQLiteHotStore(tmp_path / "hot.sqlite"))
    quotes.initialize()
    return quotes


def test_resolve_stores_the_published_level_once(tmp_path):
    quotes = _quotes(tmp_path)
    reader = SimpleNamespace(
        published_price_to_beat=AsyncMock(return_value=PUBLISHED))
    resolved_at = CLOSE + timedelta(seconds=45)
    assert asyncio.run(_resolve_closed_pm_reference(
        quotes, reader, _notice(), now=resolved_at))
    assert quotes.reference_level_for("polymarket", SLUG) == PUBLISHED
    # A second pass is served from the store: no second Gamma fetch for
    # a contract whose level is already resolved.
    reader.published_price_to_beat = AsyncMock(
        side_effect=AssertionError("must not refetch a resolved contract"))
    assert asyncio.run(_resolve_closed_pm_reference(
        quotes, reader, _notice(), now=resolved_at + timedelta(seconds=30)))


def test_resolve_waits_while_gamma_has_not_published(tmp_path):
    quotes = _quotes(tmp_path)
    reader = SimpleNamespace(
        published_price_to_beat=AsyncMock(return_value=None))
    assert not asyncio.run(_resolve_closed_pm_reference(
        quotes, reader, _notice(), now=CLOSE + timedelta(seconds=30)))
    assert quotes.reference_level_for("polymarket", SLUG) is None


def _record_full_window(quotes):
    """One complete ETH window recorded as the broken collector did:
    snapshots in-window (so no Polymarket reference anywhere), Kalshi
    carrying its floor_strike from the live listing as in production."""
    entry_book = {
        "timestamp": str(int((OPEN + timedelta(seconds=298)).timestamp() * 1000)),
        "bids": [{"price": "0.39", "size": "20"}],
        "asks": [{"price": "0.40", "size": "20"}],
    }
    assert quotes.record_polymarket(
        asset="ETH", slug=SLUG, window_open=OPEN, window_close=CLOSE,
        fee_rate=0.01, book=entry_book,
        received_at=OPEN + timedelta(seconds=300))
    exit_book = {
        "timestamp": str(int((OPEN + timedelta(seconds=598)).timestamp() * 1000)),
        "bids": [{"price": "0.46", "size": "20"}],
        "asks": [{"price": "0.47", "size": "20"}],
    }
    assert quotes.record_polymarket(
        asset="ETH", slug=SLUG, window_open=OPEN, window_close=CLOSE,
        fee_rate=0.01, book=exit_book,
        received_at=OPEN + timedelta(seconds=600))
    early = {"orderbook_fp": {
        "yes_dollars": [["0.4000", "20.0"]],
        "no_dollars": [["0.5800", "20.0"]]}}
    assert quotes.record_kalshi(
        asset="ETH", ticker="KXETH15M-1", window_open=OPEN,
        window_close=CLOSE, payload=early,
        received_at=OPEN + timedelta(seconds=270),
        reference_level=100_050.0)
    entry = {"orderbook_fp": {
        "yes_dollars": [["0.4700", "20.0"]],
        "no_dollars": [["0.5100", "20.0"]]}}
    assert quotes.record_kalshi(
        asset="ETH", ticker="KXETH15M-1", window_open=OPEN,
        window_close=CLOSE, payload=entry,
        received_at=OPEN + timedelta(seconds=300),
        reference_level=100_050.0)


def test_resolved_reference_pairs_the_window_end_to_end(tmp_path):
    store = SQLiteHotStore(tmp_path / "hot.sqlite")
    quotes = ConvergenceQuotes(store)
    quotes.initialize()
    _record_full_window(quotes)
    # Before resolution the window is exactly the production failure:
    # snapshots present, Kalshi reference carried, PM reference absent.
    windows = load_windows(store)
    assert windows[0]["pm_reference"] is None
    report = evaluate_windows(windows, now=NOW)
    assert report["coverage"]["exclusions"]["strike_unavailable"] == 1

    reader = SimpleNamespace(
        published_price_to_beat=AsyncMock(return_value=PUBLISHED))
    assert asyncio.run(_resolve_closed_pm_reference(
        quotes, reader, _notice(), now=CLOSE + timedelta(seconds=45)))

    # The snapshot rows themselves were never modified...
    rows = quotes.snapshots(venue="polymarket", contract_id=SLUG)
    assert all(row["reference_level"] is None for row in rows)
    # ...but the loader now pairs the window with the published level.
    windows = load_windows(store)
    assert len(windows) == 1
    assert windows[0]["pm_reference"] == PUBLISHED
    assert windows[0]["kalshi_reference"] == 100_050.0
    report = evaluate_windows(windows, now=NOW)
    assert report["coverage"]["exclusions"]["strike_unavailable"] == 0
    assert report["coverage"]["trigger_windows"] == 1


def test_record_reference_first_published_value_wins(tmp_path):
    quotes = _quotes(tmp_path)
    assert quotes.record_reference(
        venue="polymarket", asset="ETH", contract_id=SLUG,
        window_open=OPEN, window_close=CLOSE,
        reference_level=PUBLISHED, resolved_at=CLOSE + timedelta(seconds=45))
    assert not quotes.record_reference(
        venue="polymarket", asset="ETH", contract_id=SLUG,
        window_open=OPEN, window_close=CLOSE,
        reference_level=999.0, resolved_at=CLOSE + timedelta(seconds=90))
    assert quotes.reference_level_for("polymarket", SLUG) == PUBLISHED


@pytest.mark.parametrize("bad", [
    None, 0, -5, float("nan"), float("inf"), "100000.0", True,
])
def test_record_reference_rejects_unusable_values(tmp_path, bad):
    quotes = _quotes(tmp_path)
    assert not quotes.record_reference(
        venue="polymarket", asset="ETH", contract_id=SLUG,
        window_open=OPEN, window_close=CLOSE,
        reference_level=bad, resolved_at=CLOSE + timedelta(seconds=45))
    assert quotes.reference_level_for("polymarket", SLUG) is None
