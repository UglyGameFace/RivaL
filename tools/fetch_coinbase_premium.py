"""Assemble the Coinbase premium series for replay screen R3.

Premium at a 5-minute close = Coinbase BTC-USD close / Binance
BTCUSDT spot close - 1, over the same candle. Sources are the free
public archives only: Coinbase Exchange's public candles API and
Binance Vision's spot klines zips. The series is written as JSONL of
{"at": <candle close time, ISO>, "premium": <float>} -- the workbench
joins it to the settled record by taking the last candle closed at or
before each issuance (no look-ahead). Read-only; writes only the
output file.

Usage:
    python tools/fetch_coinbase_premium.py \
        --start 2026-10-31T00:00:00Z --end 2026-11-20T23:59:59Z \
        --output premium.jsonl
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
VISION = ("https://data.binance.vision/data/spot/daily/klines/"
          "BTCUSDT/5m/BTCUSDT-5m-{day}.zip")
CANDLE_SECONDS = 300
_UA = {"User-Agent": "rival-replay-workbench/1.0"}


def _get_json(url: str):
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def _get_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def coinbase_closes(start: datetime, end: datetime) -> dict[datetime, float]:
    """{candle close time: close} from Coinbase BTC-USD 5m candles."""
    closes: dict[datetime, float] = {}
    step = timedelta(seconds=CANDLE_SECONDS * 300)  # 300 candles/request
    cursor = start
    while cursor < end:
        window_end = min(cursor + step, end)
        url = (f"{COINBASE}?granularity={CANDLE_SECONDS}"
               f"&start={cursor.isoformat()}&end={window_end.isoformat()}")
        for candle in _get_json(url):
            opened = datetime.fromtimestamp(int(candle[0]), UTC)
            closes[opened + timedelta(seconds=CANDLE_SECONDS)] = float(
                candle[4])
        cursor = window_end
    return closes


def binance_closes(start: datetime, end: datetime) -> dict[datetime, float]:
    """{candle close time: close} from Binance Vision BTCUSDT 5m zips."""
    closes: dict[datetime, float] = {}
    day = start.date()
    while day <= end.date():
        payload = _get_bytes(VISION.format(day=day.isoformat()))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            name = archive.namelist()[0]
            text = archive.read(name).decode()
        for fields in csv.reader(io.StringIO(text)):
            try:
                open_ms, close = int(fields[0]), float(fields[4])
                close_ms = int(fields[6])
            except (ValueError, IndexError):
                continue  # header or malformed row: never guessed
            opened = datetime.fromtimestamp(open_ms / 1000, UTC)
            if not (start - timedelta(days=1) <= opened
                    <= end + timedelta(days=1)):
                continue
            closes[datetime.fromtimestamp(close_ms / 1000, UTC)] = close
        day += timedelta(days=1)
    return closes


def fetch_premium_series(start: datetime, end: datetime):
    """[(close_at, premium)] over candles both venues printed."""
    coinbase = coinbase_closes(start, end)
    binance = binance_closes(start, end)
    points = []
    for close_at in sorted(set(coinbase) & set(binance)):
        if binance[close_at] > 0:
            points.append(
                (close_at, coinbase[close_at] / binance[close_at] - 1.0))
    return points


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the Coinbase premium series (screen R3)")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    points = fetch_premium_series(start, end)
    args.output.write_text("\n".join(
        json.dumps({"at": at.isoformat(), "premium": premium})
        for at, premium in points) + "\n")
    print(json.dumps({
        "points": len(points),
        "first": points[0][0].isoformat() if points else None,
        "last": points[-1][0].isoformat() if points else None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
