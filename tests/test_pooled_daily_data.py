"""Nightly six-year training reads daily files through yesterday, not month ends."""
from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import feature_signal_experiment as source


def _zip(rows: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.csv", "\n".join(rows))
    return buf.getvalue()


def _klines(start: datetime, end: datetime) -> bytes:
    rows, t, price = [], start, 100.0
    while t < end:
        ms = int(t.timestamp() * 1000)
        price += 0.01
        rows.append(f"{ms},{price},{price + .1},{price - .1},{price},5,{ms + 299999},"
                    f"500,10,2.5,250")
        t += timedelta(minutes=5)
    return _zip(rows)


def _funding(start: datetime, end: datetime) -> bytes:
    rows, t = [], start
    while t < end:
        rows.append(f"{int(t.timestamp() * 1000)},8,0.0001")
        t += timedelta(hours=8)
    return _zip(rows)


def _day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _fake_binance(published: dict[str, bytes]):
    async def get(client, url, cache, sem):
        return published.get(url.rsplit("/", 1)[-1])
    return get


def _load(published, last_month, through_day):
    return asyncio.run(source.load_asset(None, "BTC", last_month, Path("."), None,
                                         fetcher=_fake_binance(published),
                                         through_day=through_day))


def test_trains_through_yesterday_and_stops_at_the_first_unpublished_day(monkeypatch):
    monkeypatch.setattr(source, "FIRST_MONTH", "2026-10")
    monkeypatch.setattr(source, "METRICS_FROM", date(2030, 1, 1))  # metrics not under test
    oct1, nov1 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    published = {"BTCUSDT-5m-2026-10.zip": _klines(oct1, nov1),
                 "BTCUSDT-fundingRate-2026-10.zip": _funding(oct1, nov1)}
    for d in (date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 4)):  # Nov 3 missing
        published[f"BTCUSDT-5m-{d.isoformat()}.zip"] = _klines(_day(d), _day(d) + timedelta(days=1))
    k = _load(published, "2026-10", date(2026, 11, 4))
    assert k.index.max() == datetime(2026, 11, 3, tzinfo=UTC)  # Nov 2 23:55 bar closes 00:00
    assert k.index.to_series().diff().dropna().max() == pd.Timedelta(minutes=5)  # no gap
    # Funding has no daily files: unknown after its last rate, never carried forward.
    assert k.loc[k.index >= datetime(2026, 11, 1, 12, tzinfo=UTC), "fund"].isna().all()
    assert k.loc[k.index < datetime(2026, 10, 31, 12, tzinfo=UTC), "fund"].notna().all()
    # Without --through-day nothing changes: month-end data only.
    assert _load(published, "2026-10", None).index.max() == nov1


def test_last_month_not_yet_posted_is_covered_by_its_daily_files(monkeypatch):
    monkeypatch.setattr(source, "FIRST_MONTH", "2026-09")
    monkeypatch.setattr(source, "METRICS_FROM", date(2030, 1, 1))
    sep1, oct1 = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    published = {"BTCUSDT-5m-2026-09.zip": _klines(sep1, oct1)}  # October monthly not out
    d = date(2026, 10, 1)
    while d <= date(2026, 11, 2):
        published[f"BTCUSDT-5m-{d.isoformat()}.zip"] = _klines(_day(d), _day(d) + timedelta(days=1))
        d += timedelta(days=1)
    k = _load(published, "2026-10", date(2026, 11, 2))
    assert k.index.min() == sep1 + timedelta(minutes=5)
    assert k.index.max() == datetime(2026, 11, 3, tzinfo=UTC)
    assert k.index.to_series().diff().dropna().max() == pd.Timedelta(minutes=5)


def test_recent_not_found_is_rechecked_but_old_not_found_is_cached():
    today = datetime.now(UTC).date()
    base = "https://data.binance.vision/data/futures/um"
    assert source._maybe_unpublished(f"{base}/daily/klines/BTCUSDT/5m/BTCUSDT-5m-{today}.zip")
    assert not source._maybe_unpublished(
        f"{base}/daily/klines/BTCUSDT/5m/BTCUSDT-5m-{today - timedelta(days=30)}.zip")
    assert source._maybe_unpublished(
        f"{base}/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-{today:%Y-%m}.zip")
    assert not source._maybe_unpublished(f"{base}/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2021-03.zip")
