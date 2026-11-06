"""Round-5 N3: pooled training must say what it trained on.

A pooled model used to train on an unknown, truncated subset: missing
months were filtered out silently, local cache hits were trusted blindly,
and the 400-day records cap dropped older days without a word. These tests
pin the disclosure: missing months are reported, the cap states what it
dropped, and a stale cache hit is detected and re-fetched, never used.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import sys
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
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


def _fake_binance(published: dict[str, bytes]):
    """Fake fetcher keyed on the FULL url: the monthly klines and premium
    files share a basename, so a basename key could not tell them apart."""
    async def get(client, url, cache, sem):
        return published.get(url)
    return get


def _load(published, last_month, through_day=None):
    return asyncio.run(source.load_asset(None, "BTC", last_month, Path("."), None,
                                         fetcher=_fake_binance(published),
                                         through_day=through_day))


def test_load_asset_reports_missing_months_per_family(monkeypatch):
    """A month whose file never arrived is missing from training, not
    silently filtered out: the frame's coverage names it per family, and
    the families are independent (klines can miss September while premium
    misses October)."""
    monkeypatch.setattr(source, "FIRST_MONTH", "2026-08")
    monkeypatch.setattr(source, "METRICS_FROM", date(2030, 1, 1))  # not under test
    months = {"2026-08": (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
              "2026-09": (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
              "2026-10": (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))}
    published = {}
    for m, (start, end) in months.items():
        if m != "2026-09":  # September klines never arrived
            published[f"{source.BASE}/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-{m}.zip"] = \
                _klines(start, end)
        if m != "2026-10":  # October premium never arrived
            published[f"{source.BASE}/monthly/premiumIndexKlines/BTCUSDT/5m/BTCUSDT-5m-{m}.zip"] = \
                _klines(start, end)
        published[f"{source.BASE}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{m}.zip"] = \
            _funding(start, end)
    k = _load(published, "2026-10")
    cov = k.attrs["coverage"]
    assert cov["months_requested"] == ["2026-08", "2026-09", "2026-10"]
    assert cov["klines"]["months_missing"] == ["2026-09"]
    assert cov["klines"]["months_loaded"] == ["2026-08", "2026-10"]
    assert cov["premium"]["months_missing"] == ["2026-10"]
    assert cov["funding"]["months_missing"] == []
    # The frame itself still spans the gap: the report, not the data, changed.
    assert cov["bars"] == len(k) > 0
    assert cov["first_bar"].startswith("2026-08-01")
    assert cov["last_bar"].startswith("2026-11-01")


# --- 400-day cap -----------------------------------------------------------

def _day_file(day: str) -> bytes:
    return f"payload-{day}".encode()


class _DriveResponse:
    def __init__(self, payload=None, content=b""):
        self._payload, self.content = payload, content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _DriveClient:
    def __init__(self, files):
        self.files = files

    async def get(self, url, params=None, headers=None):
        if params.get("alt") == "media":
            return _DriveResponse(content=self.files[url.rsplit("/", 1)[-1]][1])
        return _DriveResponse({"files": [
            {"id": i, "name": n, "parents": ["hist"],
             "md5Checksum": hashlib.md5(b, usedforsecurity=False).hexdigest()}
            for i, (n, b) in self.files.items()]})


class _Drive:
    DRIVE_URL = "https://drive"
    folders = type("F", (), {"history": "hist"})()

    def __init__(self, files):
        self._client = _DriveClient(files)

    async def _verify_folder_boundary(self):
        return None

    @staticmethod
    def _escape_query(value):
        return value

    async def _headers(self):
        return {}


def test_records_cap_states_what_it_dropped(tmp_path):
    """The 400-day cap used to drop older days silently. It now reports
    the cap, how many days were dropped, and the dropped range."""
    from parlay_bot.research.training_warehouse import TrainingWarehouse

    days = [f"2026-11-0{d}" for d in range(1, 6)]
    files = {f"f{i}": (f"rival-records__perp_market_candles__{d}.ndjson.gz", _day_file(d))
             for i, d in enumerate(days)}
    warehouse = TrainingWarehouse(_Drive(files), Path(tmp_path))
    got = asyncio.run(warehouse.records("perp_market_candles", max_files=2))
    assert got == [_day_file("2026-11-04"), _day_file("2026-11-05")]  # newest two
    cov = warehouse.stats["records_perp_market_candles_coverage"]
    assert cov == {"cap_days": 2, "days_found": 5, "days_returned": 2,
                   "days_dropped_by_cap": 3,
                   "dropped_first_day": "2026-11-01", "dropped_last_day": "2026-11-03"}
    # No drop, no alarm: the cap is still stated.
    warehouse2 = TrainingWarehouse(_Drive(files), Path(tmp_path) / "again")
    asyncio.run(warehouse2.records("perp_market_candles", max_files=400))
    cov2 = warehouse2.stats["records_perp_market_candles_coverage"]
    assert cov2["days_dropped_by_cap"] == 0
    assert cov2["dropped_first_day"] is None and cov2["dropped_last_day"] is None


# --- stale local cache -----------------------------------------------------

class _HttpResponse:
    def __init__(self, status_code=200, content=b"", text=""):
        self.status_code, self.content, self.text = status_code, content, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f"HTTP {self.status_code}")


class _HttpClient:
    """Fake httpx client: serves one zip (plus its .CHECKSUM), or raises."""

    def __init__(self, raw: bytes):
        self.raw, self.calls = raw, []

    async def get(self, url):
        self.calls.append(url)
        if url.endswith(".CHECKSUM"):
            return _HttpResponse(200, text=hashlib.sha256(self.raw).hexdigest() + "  x.zip")
        return _HttpResponse(200, content=self.raw)


def test_fetch_rejects_a_corrupt_cache_hit_and_redownloads(tmp_path):
    """A cache file that is not a valid zip is stale, not data: fetch drops
    it and re-downloads instead of training on the wrong bytes."""
    good = _klines(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    url = "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2026-10.zip"
    cache = Path(tmp_path)
    stale = cache / hashlib.sha1(url.encode()).hexdigest()
    stale.write_bytes(b"this is not a zip file")
    got = asyncio.run(source.fetch(_HttpClient(good), url, cache, asyncio.Semaphore(4)))
    assert got == good
    assert stale.read_bytes() == good  # the stale entry was replaced, not reused


def test_fetch_trusts_a_valid_cache_hit_without_network(tmp_path):
    good = _klines(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    url = "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2026-10.zip"
    cache = Path(tmp_path)
    (cache / hashlib.sha1(url.encode()).hexdigest()).write_bytes(good)

    class _NoNetwork:
        async def get(self, url):
            raise AssertionError("network used for a valid cache hit")

    got = asyncio.run(source.fetch(_NoNetwork(), url, cache, asyncio.Semaphore(4)))
    assert got == good


def test_warehouse_fetcher_rejects_cache_bytes_that_fail_the_index_md5(tmp_path):
    """The warehouse index knows the file's MD5. Cached bytes that do not
    match it are stale: the fetcher re-downloads from the warehouse."""
    from parlay_bot.research.training_warehouse import TrainingWarehouse

    good = _klines(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    url = ("https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/5m/"
           "BTCUSDT-5m-2026-10.zip")
    name = "BTCUSDT-5m-2026-10.zip"
    files = {"fid": (name, good)}
    warehouse = TrainingWarehouse(_Drive(files), Path(tmp_path))
    warehouse.index = {name: ("fid", hashlib.md5(good, usedforsecurity=False).hexdigest(),
                              len(good))}
    cache = Path(tmp_path)
    (cache / hashlib.sha1(url.encode()).hexdigest()).write_bytes(b"corrupt bytes")

    async def _never_official(client, url, cache, sem):
        raise AssertionError("official download used for a warehoused file")

    fetch = warehouse.fetcher(_never_official)
    got = asyncio.run(fetch(None, url, cache, asyncio.Semaphore(4)))
    assert got == good
    assert warehouse.stats["cache_stale"] == 1
    assert (cache / hashlib.sha1(url.encode()).hexdigest()).read_bytes() == good


def test_warehouse_fetcher_trusts_cache_bytes_that_match_the_index_md5(tmp_path):
    from parlay_bot.research.training_warehouse import TrainingWarehouse

    good = _klines(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    url = ("https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/5m/"
           "BTCUSDT-5m-2026-10.zip")
    name = "BTCUSDT-5m-2026-10.zip"
    warehouse = TrainingWarehouse(_Drive({"fid": (name, good)}), Path(tmp_path))
    warehouse.index = {name: ("fid", hashlib.md5(good, usedforsecurity=False).hexdigest(),
                              len(good))}
    cache = Path(tmp_path)
    (cache / hashlib.sha1(url.encode()).hexdigest()).write_bytes(good)

    class _NoDrive:
        async def get(self, *args, **kwargs):
            raise AssertionError("warehouse read for a verified cache hit")

    warehouse.drive._client = _NoDrive()

    async def _never_official(client, url, cache, sem):
        raise AssertionError("official download used for a verified cache hit")

    fetch = warehouse.fetcher(_never_official)
    got = asyncio.run(fetch(None, url, cache, asyncio.Semaphore(4)))
    assert got == good
    assert warehouse.stats["already_cached"] == 1
    assert warehouse.stats["cache_stale"] == 0


# --- train_horizon coverage -------------------------------------------------

def _gap_frames():
    """Two 10-day months (Jan and Mar 2025) with February entirely absent."""
    from parlay_bot.perpetuals.pooled_features import FEATURES

    rng = np.random.default_rng(7)
    parts = []
    for start in (datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC)):
        parts.append(pd.date_range(start, periods=2880, freq="5min", tz=UTC))
    index = parts[0].union(parts[1])
    n = len(index)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    k = pd.DataFrame({"open": closes, "high": closes * 1.001, "low": closes * 0.999,
                      "close": closes, "volume": rng.uniform(5, 50, n)}, index=index)
    f = pd.DataFrame(rng.normal(size=(n, len(FEATURES))).astype("float32"),
                     index=index, columns=list(FEATURES))
    return {"BTC": (k, f)}


def test_train_horizon_reports_the_gap_month_it_never_saw():
    """train_horizon used to have no month/coverage check at all. Its
    artifact now states which requested months were loaded, which were
    missing, and which months the shipped weights actually trained on."""
    pytest.importorskip("lightgbm")
    from parlay_bot.research import pooled_training

    frames = _gap_frames()
    start = frames["BTC"][0].index[0]
    doc = pooled_training.train_horizon(
        frames, 5, train_end=start + pd.Timedelta(days=7),
        test_start=start + pd.Timedelta(days=14),
        trained_through=frames["BTC"][0].index[-1],
        months_requested=["2025-01", "2025-02", "2025-03"])
    cov = doc["coverage"]
    assert cov["months_requested"] == ["2025-01", "2025-02", "2025-03"]
    assert cov["months_loaded"] == ["2025-01", "2025-03"]
    assert cov["months_missing"] == ["2025-02"]
    assert cov["streams"]["BTC"]["months"] == ["2025-01", "2025-03"]
    assert cov["streams"]["BTC"]["months_missing"] == ["2025-02"]
    assert cov["streams"]["BTC"]["days"] == 20
    # The weights saw only the train slice (January here), never March.
    assert cov["months_in_train"] == ["2025-01"]
    assert cov["days_in_train"] == 7
    # Coverage is disclosure only: the artifact still validates and the
    # gates still decide publication.
    from parlay_bot.perpetuals.pooled_model import validate

    validate(doc)


def test_train_horizon_without_requested_months_reports_what_it_loaded():
    """With no requested months to compare against, the artifact states
    what it loaded and leaves months_missing unknown (null), rather than
    claiming nothing was missing."""
    pytest.importorskip("lightgbm")
    from parlay_bot.research import pooled_training

    frames = _gap_frames()
    start = frames["BTC"][0].index[0]
    doc = pooled_training.train_horizon(
        frames, 5, train_end=start + pd.Timedelta(days=7),
        test_start=start + pd.Timedelta(days=14),
        trained_through=frames["BTC"][0].index[-1])
    assert doc["coverage"]["months_requested"] is None
    assert doc["coverage"]["months_missing"] is None
    assert doc["coverage"]["months_loaded"] == ["2025-01", "2025-03"]
