"""Official auxiliary source ZIP must be authentic, exact and explicitly scoped."""
import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.research.binance_auxiliary_backfill import (
    checksum,
    inspect_original_zip,
    source_url,
)
from parlay_bot.research.drive_source_census import parse_name
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.historical_coordinator import ROUTES


def _zip(month="2026-02", missing=None):
    first = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    rows = []
    for index in range(int((end - first).total_seconds() // 3600)):
        if index == missing:
            continue
        tick = int((first + timedelta(hours=index)).timestamp() * 1000)
        rows.append(f"{tick},100,105,95,102,0")
    name = f"BTCUSDT-1h-{month}.csv"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, "\n".join(rows) + "\n")
    return buf.getvalue()


def test_original_binance_aux_archive_urls_and_source_integrity():
    url = source_url("BTC", "candles_1h", "2026-02")
    assert url.endswith("/klines/BTCUSDT/1h/BTCUSDT-1h-2026-02.zip")
    assert "/premiumPriceKlines/" in source_url("ETH", "premium", "2026-02")
    raw = _zip()
    digest = hashlib.sha256(raw).hexdigest()
    assert checksum(raw, (digest + "  BTCUSDT-1h-2026-02.zip").encode(),
                    file_name="BTCUSDT-1h-2026-02.zip") == digest
    with pytest.raises(HistoryImportError, match="SHA256 mismatch"):
        checksum(raw, ("0" * 64 + "  BTCUSDT-1h-2026-02.zip").encode(),
                 file_name="BTCUSDT-1h-2026-02.zip")
    with pytest.raises(ValueError):
        source_url("../ETH", "candles_1h", "2026-02")


def test_exact_utc_grid_is_independent_of_authentic_original_checksum():
    proof = inspect_original_zip(_zip(), asset="BTC",
                                 family="candles_1h", month="2026-02")
    assert proof["complete_utc_grid"] is True
    assert proof["expected_rows"] == 672
    gap = inspect_original_zip(_zip(missing=17), asset="BTC",
                               family="candles_1h", month="2026-02")
    assert gap["complete_utc_grid"] is False
    assert gap["missing_slots"] == 1


def test_census_and_coordinator_recognize_new_real_aux_sources():
    for family in (
        "mark", "index", "premium", "candles_1m", "candles_15m",
        "candles_1h", "candles_4h", "candles_1d", "funding",
    ):
        raw = f"binance-aux-BTC-{family}-2026-02--" + "a" * 16 + ".manifest.json"
        assert parse_name(raw) == ("binance", "BTC", family, "2026-02", "a" * 16)
        assert ROUTES[("binance", family)] == (
            "binance-original-auxiliary.yml", "official_checksum_zip",
        )
