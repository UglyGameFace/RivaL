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
    # Binance publishes premium index klines as "premiumIndexKlines".
    assert source_url("ETH", "premium", "2026-02").endswith(
        "/premiumIndexKlines/ETHUSDT/5m/ETHUSDT-5m-2026-02.zip")
    assert "/premiumPriceKlines/" not in source_url("ETH", "premium", "2026-02")
    # Pre-2021 official months are requested; nothing before 2019-09.
    assert source_url("BTC", "funding", "2019-09").endswith(
        "/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2019-09.zip")
    with pytest.raises(ValueError):
        source_url("BTC", "funding", "2019-08")
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


def _funding_zip(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    content = "calc_time,funding_interval_hours,last_funding_rate\n"
    content += "\n".join(rows) + "\n"
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("BTCUSDT-fundingRate-2026-02.csv", content)
    return buf.getvalue()


def test_official_monthly_funding_event_source_preserves_original_semantics():
    assert source_url("BTC", "funding", "2026-02").endswith(
        "/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-02.zip"
    )
    at = int(datetime(2026, 2, 2, 8, tzinfo=UTC).timestamp() * 1000)
    raw = _funding_zip([f"{at},8,-0.0001", f"{at + 8 * 3600000},8,0.0002"])
    info = inspect_original_zip(raw, asset="BTC", family="funding", month="2026-02")
    assert info["rows"] == 2
    assert info["source_event_timestamps_validated"] is True
    assert info["expected_rows"] is None
    assert info["complete_utc_grid"] is False
    assert info["complete_funding_settlement_history_proven"] is False
    with pytest.raises(HistoryImportError, match="duplicate"):
        inspect_original_zip(
            _funding_zip([f"{at},8,0.1", f"{at},8,0.2"]),
            asset="BTC", family="funding", month="2026-02",
        )
    with pytest.raises(HistoryImportError, match="no source events"):
        inspect_original_zip(_funding_zip([]), asset="BTC",
                             family="funding", month="2026-02")
