"""Bounded, reconnecting public perpetual order-book streams.

Official native perpetual snapshots for the shared source registry.
Snapshots remain in RAM, never upload raw ticks to PostgreSQL or Google Drive.
REST and existing five-minute candle collection remain independent fallbacks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import websockets

from parlay_bot.perpetuals.live_quotes import (
    ASSETS,
    GATE_CONTRACT_SIZE,
    LIGHTER_PERP_MARKET_IDS,
    SOURCE_QUOTE_CURRENCIES,
    VENUE_BOOK_SYMBOLS,
    BookQuote,
    PerpReferenceBook,
    QuoteUnavailable,
    VenueBookQuote,
    parse_bitget_ticker,
    parse_hyperliquid_reference,
    parse_kucoin_ticker,
    parse_okx_ticker,
)

_LOG = logging.getLogger(__name__)
HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"
# Binance USD-M migrated high-frequency bookTicker to /public in 2026.
# Build default and scoped subscriptions from one canonical route.
def binance_book_stream_url(assets: tuple[str, ...]) -> str:
    return "wss://fstream.binance.com/public/stream?streams=" + "/".join(
        f"{ASSETS[asset].lower()}@bookTicker" for asset in assets
    )


BINANCE_WS = binance_book_stream_url(tuple(ASSETS))
VENUES = tuple(SOURCE_QUOTE_CURRENCIES)
RECONNECT_CAP_SECONDS = 60.0
BOOK_SILENCE_SECONDS = 120
RECEIVE_CHECK_SECONDS = 30
# Liquidity-only Binance feeds (issue #295), on Binance's documented routes:
# partial depth is a /public stream, aggregate trades a /market stream.
OKX_PUBLIC_WS = "wss://ws.okx.com:443/ws/v5/public"
BITGET_PUBLIC_WS = "wss://ws.bitget.com/v2/ws/public"
GATE_PUBLIC_WS = "wss://fx-ws.gateio.ws/v4/ws/usdt"
DERIBIT_PUBLIC_WS = "wss://www.deribit.com/ws/api/v2"
# Official public read-only mode; no account, signer, auth or order channel.
LIGHTER_PUBLIC_WS = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BINANCE_DEPTH_WS = "wss://fstream.binance.com/public/stream?streams="
BINANCE_TRADES_WS = "wss://fstream.binance.com/market/stream?streams="
# KuCoin Futures public websocket. The bullet-public token is fetched fresh
# on every connection attempt (24h expiry); the endpoint below is the
# documented default and is replaced by the bullet response when it differs.
KUCOIN_FUTURES_WS = "wss://ws-api-futures.kucoin.com/endpoint"
KUCOIN_BULLET_PUBLIC_URL = "https://api-futures.kucoin.com/api/v1/bullet-public"
# Phemex public websocket. No token, no auth: one orderbook_p.subscribe
# per USDT perpetual symbol. The plain {"method", "params"} shape is what
# the venue accepts — wrapping it in a jsonrpc envelope is rejected with
# error 6001 (verified live 2026-11-17). orderbook.subscribe (no _p)
# returns the same 6001 for USDT perps and is never used here.
PHEMEX_PUBLIC_WS = "wss://ws.phemex.com"


async def fetch_kucoin_bullet_token(
    *, transport: httpx.AsyncTransport | None = None,
) -> tuple[str, str]:
    """Public KuCoin Futures bullet token + websocket endpoint; no auth.

    Returns (token, endpoint). A failed fetch raises and the hub's backoff
    applies; the caller fetches again on every reconnect attempt, so a
    reconnect never reuses an expired token.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0), transport=transport,
    ) as client:
        resp = await client.post(KUCOIN_BULLET_PUBLIC_URL, json={})
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, dict) or payload.get("code") != "200000":
        raise ValueError("KuCoin bullet-public request rejected.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TypeError("KuCoin bullet-public response malformed.")
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("KuCoin bullet-public token missing.")
    endpoint = KUCOIN_FUTURES_WS
    servers = data.get("instanceServers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        candidate = servers[0].get("endpoint")
        if isinstance(candidate, str) and candidate.startswith("wss://"):
            endpoint = candidate
    return token, endpoint


def _kucoin_is_ping(message: str | bytes) -> bool:
    if isinstance(message, bytes):
        return False
    try:
        frame = json.loads(message)
    except (ValueError, TypeError):
        return False
    return isinstance(frame, dict) and frame.get("type") == "ping"


class PhemexBookResync(RuntimeError):
    """A Phemex delta arrived with no snapshot: the book state is unknown."""

    def __init__(self, asset: str) -> None:
        super().__init__(f"Phemex book for {asset} has no snapshot yet")
        self.asset = asset


def _phemex_book_levels(raw: Any) -> list[tuple[float, float]]:
    """One side of a Phemex WS book frame: [[price, size], ...]."""
    if not isinstance(raw, list):
        raise TypeError("order book side is not a list")
    levels = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("malformed order book level")
        price, quantity = float(row[0]), float(row[1])
        if quantity < 0:
            raise ValueError("negative order book size")
        levels.append((price, quantity))
    return levels


class PhemexBook:
    """One Phemex asset's order book: snapshot replace + delta merge.

    The venue publishes a 30-level snapshot on subscribe (and again every
    60s for self-verification), then incremental deltas only. A delta
    with size 0 deletes its level; any other size replaces it; a new
    price inserts sorted. Only the top 30 levels per side are kept, so
    the book stays bounded no matter how long the socket lives.

    The sequence is an event counter, NOT +1 per message (verified live
    2026-11-17: jumps of 1–381 between consecutive messages), so no +1
    continuity is assumed. The only sequence rules are monotonicity — a
    non-increasing sequence is a stale replay and is discarded — and
    that a delta may never apply before the first snapshot: that state
    gap raises PhemexBookResync so the caller re-subscribes for a fresh
    snapshot instead of scoring against a half-known book.
    """

    _DEPTH = 30

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_sequence: int | None = None
        self.synced = False

    def apply(self, frame: dict, *, asset: str, received_at: datetime
              ) -> VenueBookQuote | None:
        kind = frame.get("type")
        if kind not in ("snapshot", "incremental"):
            raise QuoteUnavailable("Phemex book frame has no snapshot/delta type.")
        sequence = frame.get("sequence")
        if type(sequence) is not int or sequence <= 0:
            raise QuoteUnavailable("Phemex book frame has no sequence.")
        nanoseconds = frame.get("timestamp")
        if isinstance(nanoseconds, str):
            nanoseconds = int(nanoseconds)
        if type(nanoseconds) is not int or nanoseconds <= 0:
            raise QuoteUnavailable("Phemex book frame has no exchange clock.")
        book = frame.get("orderbook_p")
        if not isinstance(book, dict):
            raise QuoteUnavailable("Phemex book frame has no order book.")
        try:
            bids = _phemex_book_levels(book.get("bids"))
            asks = _phemex_book_levels(book.get("asks"))
        except (ValueError, TypeError) as exc:
            raise QuoteUnavailable("Malformed Phemex order book level.") from exc
        if kind == "snapshot":
            # A snapshot always replaces: it is a complete vendor state,
            # and a delayed older snapshot rewinds to a still-complete
            # state (later deltas re-apply idempotently). The hub's own
            # exchange_at guard keeps the stored quote monotonic.
            # Validate before assigning so a bad snapshot never empties
            # a good book.
            new_bids = {p: q for p, q in bids if q > 0}
            new_asks = {p: q for p, q in asks if q > 0}
            if not new_bids or not new_asks:
                raise QuoteUnavailable("Phemex order book is empty.")
            self.bids, self.asks = new_bids, new_asks
            self.last_sequence = sequence
            self.synced = True
        else:
            if not self.synced:
                raise PhemexBookResync(asset)
            if sequence <= self.last_sequence:
                return None  # stale replay; the newer book stands
            for price, quantity in bids:
                if quantity == 0:
                    self.bids.pop(price, None)
                else:
                    self.bids[price] = quantity
            for price, quantity in asks:
                if quantity == 0:
                    self.asks.pop(price, None)
                else:
                    self.asks[price] = quantity
            self._trim()
            self.last_sequence = sequence
        if not self.bids or not self.asks:
            raise QuoteUnavailable("Phemex order book is empty.")
        bid_price, bid_quantity = max(self.bids.items())
        ask_price, ask_quantity = min(self.asks.items())
        try:
            quote = VenueBookQuote(
                asset, "phemex", VENUE_BOOK_SYMBOLS["phemex"][asset],
                bid_price, bid_quantity, ask_price, ask_quantity,
                datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=UTC),
                received_at,
            )
            quote.validate(now=received_at)
        except (ValueError, TypeError, OverflowError) as exc:
            raise QuoteUnavailable("Invalid Phemex book quote.") from exc
        return quote

    def _trim(self) -> None:
        if len(self.bids) > self._DEPTH:
            self.bids = dict(sorted(self.bids.items(), reverse=True)[: self._DEPTH])
        if len(self.asks) > self._DEPTH:
            self.asks = dict(sorted(self.asks.items())[: self._DEPTH])


def parse_stream_message(
    venue: str, message: str | bytes, *, received_at: datetime,
    phemex_books: dict[str, PhemexBook] | None = None,
) -> PerpReferenceBook | BookQuote | VenueBookQuote | None:
    """Only admit timestamped, source-identified, viable best-book updates."""
    if isinstance(message, bytes):
        raise QuoteUnavailable("Binary market stream frame not supported.")
    if venue in ("okx", "bitget") and message == "pong":
        return None
    try:
        wrapper: Any = json.loads(message)
    except (ValueError, TypeError) as exc:
        raise QuoteUnavailable("Invalid market stream JSON.") from exc
    if not isinstance(wrapper, dict):
        raise QuoteUnavailable("Market stream must be an object.")
    if venue in ("gate", "deribit", "lighter"):
        try:
            if venue == "gate":
                if wrapper.get("channel") != "futures.book_ticker" or wrapper.get("event") != "update":
                    return None
                row = wrapper["result"]
                symbol = row["s"]
                asset = next((a for a,s in VENUE_BOOK_SYMBOLS[venue].items() if s == symbol), None)
                if asset is None:
                    return None
                stamp, scale = row["t"], 1000
                bid, ask = row["b"], row["a"]
                bid_qty = float(row["B"])*GATE_CONTRACT_SIZE[asset]
                ask_qty = float(row["A"])*GATE_CONTRACT_SIZE[asset]
            elif venue == "deribit":
                if wrapper.get("method") != "subscription":
                    return None
                params = wrapper["params"]
                row = params["data"]
                symbol = row["instrument_name"]
                asset = next((a for a,s in VENUE_BOOK_SYMBOLS[venue].items() if s == symbol), None)
                if asset is None or params["channel"] != f"book.{symbol}.none.1.100ms":
                    return None
                stamp, scale = row["timestamp"], 1000
                # This grouped-depth channel sends complete [price, amount]
                # snapshots. USDC-linear amounts are already base coins.
                (bid,bid_qty), (ask,ask_qty) = row["bids"][0], row["asks"][0]
            else:
                if wrapper.get("type") not in ("subscribed/ticker", "update/ticker"):
                    return None
                row = wrapper["ticker"]
                asset = row["s"]
                if asset not in LIGHTER_PERP_MARKET_IDS:
                    return None
                if wrapper["channel"] != f"ticker:{LIGHTER_PERP_MARKET_IDS[asset]}":
                    raise ValueError("wrong perpetual market ID")
                symbol = VENUE_BOOK_SYMBOLS[venue][asset]
                stamp, scale = row["last_updated_at"], 1_000_000
                if wrapper["last_updated_at"] != stamp:
                    raise ValueError("conflicting source clocks")
                bid, bid_qty = row["b"]["price"], row["b"]["size"]
                ask, ask_qty = row["a"]["price"], row["a"]["size"]
            if type(stamp) is not int or stamp <= 0:
                raise ValueError("missing source clock")
            quote = VenueBookQuote(
                asset, venue, symbol, float(bid), float(bid_qty), float(ask), float(ask_qty),
                datetime.fromtimestamp(stamp/scale, tz=UTC), received_at,
            )
            quote.validate(now=received_at)
            return quote
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            raise QuoteUnavailable("Invalid native perpetual book.") from exc
    if venue in ("okx", "bitget"):
        arg = wrapper.get("arg")
        data = wrapper.get("data")
        if data is None:
            return None  # subscription acknowledgements and notices
        if not isinstance(arg, dict):
            raise QuoteUnavailable("Missing venue subscription identity.")
        channel = "books5" if venue == "okx" else "ticker"
        if arg.get("channel") != channel:
            return None
        if venue == "bitget" and arg.get("instType") != "USDT-FUTURES":
            return None
        symbol = arg.get("instId")
        asset = next((asset for asset, expected in VENUE_BOOK_SYMBOLS[venue].items()
                      if expected == symbol), None)
        if asset is None:
            return None
        try:
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise ValueError("invalid book snapshot")
            row = data[0]
            if row.get("instId", symbol) != symbol or row.get("symbol", symbol) != symbol:
                raise ValueError("snapshot identity mismatch")
            if venue == "okx":
                bid, ask = row["bids"][0], row["asks"][0]
                ticker = {"instId": symbol, "ts": row["ts"],
                          "bidPx": bid[0], "bidSz": bid[1],
                          "askPx": ask[0], "askSz": ask[1]}
                return parse_okx_ticker({"code": "0", "data": [ticker]},
                                        asset=asset, received_at=received_at)
            return parse_bitget_ticker({"code": "00000", "data": [{**row, "symbol": symbol}]},
                                       asset=asset, received_at=received_at)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            raise QuoteUnavailable("Invalid source-matched venue book.") from exc
    if venue == "kucoin":
        # Control frames carry no book; tickerV2 is the only book channel.
        # The data row has the same shape as the REST ticker, so the same
        # parser (nanosecond clock, contract-count sizes) applies.
        if wrapper.get("type") in ("welcome", "ack", "ping", "pong"):
            return None
        if wrapper.get("type") != "message":
            return None
        topic = wrapper.get("topic")
        if not isinstance(topic, str) or not topic.startswith("/contractMarket/tickerV2:"):
            return None
        symbol = topic.removeprefix("/contractMarket/tickerV2:")
        asset = next((a for a, s in VENUE_BOOK_SYMBOLS["kucoin"].items()
                      if s == symbol), None)
        if asset is None:
            return None
        data = wrapper.get("data")
        if not isinstance(data, dict):
            raise QuoteUnavailable("Malformed KuCoin ticker push.")
        return parse_kucoin_ticker({"code": "200000", "data": data},
                                   asset=asset, received_at=received_at)
    if venue == "phemex":
        # Subscribe acknowledgements carry no book; an error dict means
        # the subscribe was rejected (6001) and the socket stays up.
        # Book frames are bare objects: {"depth": 30, "dts": <ns>,
        # "orderbook_p": {"asks": [[px, qty], ...], "bids": [...]},
        # "sequence": <int>, "symbol": "BTCUSDT", "timestamp": <ns>,
        # "type": "snapshot" | "incremental"} — verified live 2026-11-17.
        # Deltas apply only through the per-asset book; a bare
        # parse_stream_message call gets a transient book, so a lone
        # delta without a snapshot raises PhemexBookResync there too.
        if "orderbook_p" not in wrapper:
            if wrapper.get("error") is not None:
                raise QuoteUnavailable("Phemex subscription rejected.")
            return None
        symbol = wrapper.get("symbol")
        asset = next((a for a, s in VENUE_BOOK_SYMBOLS["phemex"].items()
                      if s == symbol), None)
        if asset is None:
            return None
        books = phemex_books if phemex_books is not None else {}
        return books.setdefault(asset, PhemexBook()).apply(
            wrapper, asset=asset, received_at=received_at)
    if venue == "hyperliquid":
        if wrapper.get("channel") != "l2Book":
            return None
        payload = wrapper.get("data")
        if not isinstance(payload, dict):
            raise QuoteUnavailable("Malformed Hyperliquid book event.")
        asset = payload.get("coin")
        if asset not in ASSETS:
            return None
        return parse_hyperliquid_reference(
            payload, asset=asset, received_at=received_at,
        )
    if venue != "binance":
        raise QuoteUnavailable("Unsupported market stream.")
    raw = wrapper.get("data", wrapper)
    if not isinstance(raw, dict) or raw.get("e") != "bookTicker":
        return None
    if raw.get("st") not in (None, 1):
        return None  # COIN-M rows cannot masquerade as USD-M perp books.
    symbol = raw.get("s")
    if symbol not in ASSETS.values():
        return None
    if type(raw.get("E")) is not int or raw["E"] <= 0:
        raise QuoteUnavailable("Missing Binance book event time.")
    try:
        quote = BookQuote(
            asset=symbol.removesuffix("USDT"),
            venue="binance_usdm",
            symbol=symbol,
            bid=float(raw["b"]), bid_quantity=float(raw["B"]),
            ask=float(raw["a"]), ask_quantity=float(raw["A"]),
            exchange_at=datetime.fromtimestamp(raw["E"] / 1000, tz=UTC),
            received_at=received_at,
        )
        quote.validate(now=received_at)
        return quote
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise QuoteUnavailable("Corrupt Binance book event.") from exc


class StreamingBookHub:
    """One bounded socket per provider, no per-user subscription fanout."""

    def __init__(self, *, assets: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP")) -> None:
        if not assets or len(set(assets)) != len(assets) or any(
            asset not in ASSETS for asset in assets
        ):
            raise ValueError("Invalid perpetual stream assets")
        self.assets = assets
        self._quotes: dict[tuple[str, str], BookQuote | PerpReferenceBook | VenueBookQuote] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._connected: dict[str, bool] = dict.fromkeys(VENUES, False)
        self._reconnects: dict[str, int] = dict.fromkeys(VENUES, 0)
        self._accepted: dict[str, int] = dict.fromkeys(VENUES, 0)
        # When each venue last delivered an accepted book. "Fresh" per coin
        # uses a 10 s quote age, but Hyperliquid pushes each coin only about
        # every 5 s, so a coin can be briefly stale while the stream is fine.
        self._last_accept: dict[str, datetime | None] = dict.fromkeys(VENUES, None)
        self._blocked_until: dict[str, datetime | None] = dict.fromkeys(VENUES, None)
        self._blocked_status: dict[str, int | None] = dict.fromkeys(VENUES, None)
        self._silence_restarts: dict[str, int] = dict.fromkeys(VENUES, 0)
        # Phemex delta state: one snapshot+merge book per asset, held
        # here (never in the module) so tests and reconnects start clean.
        # A delta with no snapshot is a state gap, never a half-known
        # book; _run re-subscribes the flagged asset for a fresh snapshot.
        self._phemex_books: dict[str, PhemexBook] = {}
        self._phemex_resync: set[str] = set()
        # Optional 5-minute liquidity recorder; attach before start().
        self.recorder = None
        self._liquidity_frames = 0

    def snapshot(self, venue: str, asset: str, *, now: datetime | None = None
                 ) -> BookQuote | PerpReferenceBook | VenueBookQuote | None:
        if venue not in VENUES or asset not in self.assets:
            return None
        clock = now or datetime.now(UTC)
        item = self._quotes.get((venue, asset))
        if item is None:
            return None
        try:
            item.validate(now=clock)
        except QuoteUnavailable:
            return None
        return item

    def health(self, *, now: datetime | None = None) -> dict:
        clock = now or datetime.now(UTC)
        return {
            venue: {
                "connected": self._connected[venue],
                "updates": self._accepted[venue],
                "reconnects": self._reconnects[venue],
                "blocked": bool(self._blocked_until[venue]
                                and clock < self._blocked_until[venue]),
                "blocked_status": self._blocked_status[venue],
                "silent_reconnects": self._silence_restarts[venue],
                "fresh_assets": tuple(
                    a for a in self.assets
                    if self.snapshot(venue, a, now=clock) is not None
                ),
                "last_update_age_s": (
                    None if self._last_accept[venue] is None
                    else max(0.0, (clock - self._last_accept[venue]).total_seconds())
                ),
            }
            for venue in VENUES
        }

    def accept(
        self, venue: str, message: str | bytes, *,
        received_at: datetime | None = None,
    ) -> bool:
        now = received_at or datetime.now(UTC)
        if self.recorder is not None and venue in ("hyperliquid", "okx"):
            # Same socket carries l2Book and trades; the recorder takes both.
            self.recorder.ingest(venue, message)
            if venue == "okx":
                self._liquidity_frames += 1
        try:
            quote = parse_stream_message(
                venue, message, received_at=now,
                phemex_books=self._phemex_books if venue == "phemex" else None,
            )
        except PhemexBookResync as exc:
            # A delta arrived with no snapshot: the book is unknown.
            # Drop any stored quote so snapshot() stops serving it and
            # flag the asset; _run re-subscribes for a fresh snapshot.
            self._phemex_resync.add(exc.asset)
            self._quotes.pop(("phemex", exc.asset), None)
            return False
        except QuoteUnavailable:
            return False
        if quote is None or quote.asset not in self.assets:
            return False
        key = (venue, quote.asset)
        previous = self._quotes.get(key)
        if previous is not None and quote.exchange_at <= previous.exchange_at:
            return False  # No replayed/out-of-order or stale snapshot.
        self._quotes[key] = quote
        self._accepted[venue] += 1
        self._last_accept[venue] = now
        return True

    async def _run(self, venue: str) -> None:
        if venue == "hyperliquid":
            url = HYPERLIQUID_WS
        elif venue == "binance":
            url = binance_book_stream_url(self.assets)
        elif venue == "okx":
            url = OKX_PUBLIC_WS
        elif venue == "bitget":
            url = BITGET_PUBLIC_WS
        elif venue == "gate":
            url = GATE_PUBLIC_WS
        elif venue == "deribit":
            url = DERIBIT_PUBLIC_WS
        elif venue == "lighter":
            url = LIGHTER_PUBLIC_WS
        elif venue == "kucoin":
            url = KUCOIN_FUTURES_WS
        elif venue == "phemex":
            url = PHEMEX_PUBLIC_WS
        else:
            raise ValueError("Unsupported quote stream venue")
        attempts = 0
        while True:
            backoff_override = 0.0
            try:
                connect_url = url
                if venue == "kucoin":
                    # Bullet token is valid 24h; fetch a fresh one on every
                    # connection attempt so a reconnect never reuses an
                    # expired token. A failed fetch raises into the hub's
                    # backoff below instead of opening an unauthenticated socket.
                    token, endpoint = await fetch_kucoin_bullet_token()
                    connect_url = f"{endpoint}?token={token}&connectId={uuid.uuid4().hex}"
                async with websockets.connect(
                    connect_url, open_timeout=10, close_timeout=5,
                    ping_interval=20, ping_timeout=20,
                    max_size=1_048_576, max_queue=32,
                ) as socket:
                    self._connected[venue] = True
                    self._blocked_until[venue] = None
                    self._blocked_status[venue] = None
                    if venue == "phemex":
                        # A fresh socket re-snapshots every symbol; drop
                        # the old books so a stale delta can never apply
                        # to a pre-reconnect book.
                        self._phemex_books = {}
                        self._phemex_resync = set()
                    last_book = datetime.now(UTC)
                    last_ping = time.monotonic()
                    attempts = 0
                    if venue == "hyperliquid":
                        for asset in self.assets:
                            await socket.send(json.dumps({
                                "method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": asset},
                            }))
                            if self.recorder is not None:
                                await socket.send(json.dumps({
                                    "method": "subscribe",
                                    "subscription": {"type": "trades", "coin": asset},
                                }))
                    elif venue == "okx":
                        args = [{"channel": "books5", "instId": f"{asset}-USDT-SWAP"}
                                for asset in self.assets]
                        if self.recorder is not None:
                            args += [{"channel": "trades", "instId": f"{asset}-USDT-SWAP"}
                                     for asset in self.assets]
                            args.append({"channel": "liquidation-orders", "instType": "SWAP"})
                        await socket.send(json.dumps({"op": "subscribe", "args": args}))
                    elif venue == "bitget":
                        # Classic public v2 format, checked against an actual
                        # live acknowledgement + ticker on 2026-11-15. The
                        # distinct UTA topic/symbol format is not this endpoint.
                        await socket.send(json.dumps({"op": "subscribe", "args": [
                            {"instType": "USDT-FUTURES", "channel": "ticker", "instId": ASSETS[a]}
                            for a in self.assets]}))
                    elif venue == "gate":
                        await socket.send(json.dumps({"time": int(time.time()),
                            "channel": "futures.book_ticker", "event": "subscribe",
                            "payload": [VENUE_BOOK_SYMBOLS[venue][a] for a in self.assets]}))
                    elif venue == "deribit":
                        await socket.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                            "method": "public/subscribe", "params": {"channels": [
                                f"book.{VENUE_BOOK_SYMBOLS[venue][a]}.none.1.100ms"
                                for a in self.assets]}}))
                    elif venue == "lighter":
                        for asset in self.assets:
                            await socket.send(json.dumps({"type": "subscribe",
                                "channel": f"ticker/{LIGHTER_PERP_MARKET_IDS[asset]}"}))
                    elif venue == "kucoin":
                        # One tickerV2 topic per asset on the single socket.
                        # tickerV2 pushes on any top-of-book change, so a
                        # quiet market may trip the 120s silence restart
                        # below; reconnects are cheap and the token is
                        # refreshed, so no new silence rule is invented.
                        for asset in self.assets:
                            await socket.send(json.dumps({
                                "type": "subscribe",
                                "topic": f"/contractMarket/tickerV2:{VENUE_BOOK_SYMBOLS[venue][asset]}",
                                "privateChannel": False, "response": True,
                            }))
                    elif venue == "phemex":
                        # One orderbook_p.subscribe per asset on the
                        # single socket. The first frame per symbol is a
                        # 30-level snapshot, then incremental deltas only.
                        for asset in self.assets:
                            await socket.send(json.dumps({
                                "id": 1, "method": "orderbook_p.subscribe",
                                "params": [VENUE_BOOK_SYMBOLS[venue][asset]],
                            }))
                    while True:
                        try:
                            message = await asyncio.wait_for(socket.recv(),
                                                             timeout=RECEIVE_CHECK_SECONDS)
                            if venue == "kucoin" and _kucoin_is_ping(message):
                                await socket.send(json.dumps({"type": "pong"}))
                            elif self.accept(venue, message):
                                last_book = datetime.now(UTC)
                            if venue == "phemex" and self._phemex_resync:
                                # A delta arrived with no snapshot: ask
                                # for a fresh snapshot for that asset
                                # instead of scoring a half-known book.
                                for asset in sorted(self._phemex_resync):
                                    await socket.send(json.dumps({
                                        "id": 1, "method": "orderbook_p.subscribe",
                                        "params": [VENUE_BOOK_SYMBOLS[venue][asset]],
                                    }))
                                self._phemex_resync.clear()
                        except TimeoutError:
                            pass
                        if venue in ("okx", "bitget") and time.monotonic()-last_ping >= 20:
                            await socket.send("ping")
                            last_ping = time.monotonic()
                        if (datetime.now(UTC) - last_book).total_seconds() >= BOOK_SILENCE_SECONDS:
                            self._silence_restarts[venue] += 1
                            _LOG.warning("RivaL %s book stream silent for %ds; reconnecting",
                                         venue, BOOK_SILENCE_SECONDS)
                            break
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.InvalidStatus as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (401, 403, 418, 451):
                    backoff_override = 3600.0
                    self._blocked_until[venue] = datetime.now(UTC) + timedelta(hours=1)
                    self._blocked_status[venue] = status
                    _LOG.warning("RivaL %s stream region/access blocked (HTTP %s)",
                                 venue, status)
                elif status == 429:
                    backoff_override = 300.0
                    _LOG.warning("RivaL %s stream rate limited", venue)
                else:
                    _LOG.warning("RivaL %s stream handshake unavailable", venue)
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException,
                    ValueError):
                _LOG.warning("RivaL %s stream disconnected; retrying safely", venue)
            finally:
                self._connected[venue] = False
            attempts += 1
            self._reconnects[venue] += 1
            await asyncio.sleep(
                max(backoff_override, min(RECONNECT_CAP_SECONDS, 2 ** min(attempts, 5)))
                + random.uniform(0, 1)
            )

    async def _run_liquidity(self, name: str, url: str, venue: str = "binance",
                             subscribe: str | None = None) -> None:
        """Binance depth/trade socket feeding only the liquidity recorder.

        Separate from the quote socket so a liquidity-feed fault can never
        interrupt the order-book quotes used for forecasts and cost proof.
        """
        attempts = 0
        while True:
            backoff_override = 0.0
            try:
                async with websockets.connect(
                    url, open_timeout=10, close_timeout=5,
                    ping_interval=20, ping_timeout=20,
                    max_size=1_048_576, max_queue=64,
                ) as socket:
                    attempts = 0
                    if subscribe is not None:
                        await socket.send(subscribe)
                    async for message in socket:
                        if self.recorder is not None:
                            self.recorder.ingest(venue, message)
                            self._liquidity_frames += 1
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.InvalidStatus as exc:
                status = getattr(exc.response, "status_code", None)
                backoff_override = (3600.0 if status in (401, 403, 418, 451)
                                    else 300.0 if status == 429 else 0.0)
                _LOG.warning("RivaL %s liquidity stream handshake HTTP %s", name, status)
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException,
                    ValueError):
                _LOG.warning("RivaL %s liquidity stream disconnected; retrying", name)
            attempts += 1
            await asyncio.sleep(
                max(backoff_override, min(RECONNECT_CAP_SECONDS, 2 ** min(attempts, 5)))
                + random.uniform(0, 1)
            )

    def start(self) -> None:
        if self._tasks:
            return
        for venue in VENUES:
            self._tasks.append(asyncio.create_task(
                self._run(venue), name=f"rival-{venue}-book-stream",
            ))
        if self.recorder is not None:
            symbols = [ASSETS[asset].lower() for asset in self.assets]
            for name, url in (
                ("binance-depth", BINANCE_DEPTH_WS
                 + "/".join(f"{s}@depth20@500ms" for s in symbols)),
                ("binance-trades", BINANCE_TRADES_WS
                 + "/".join(f"{s}@aggTrade" for s in symbols)),
            ):
                self._tasks.append(asyncio.create_task(
                    self._run_liquidity(name, url), name=f"rival-{name}-liquidity",
                ))
            # Order flow from US-reachable venues: OKX perps and Coinbase spot.
            coinbase = json.dumps({"type": "subscribe",
                                   "product_ids": [f"{a}-USD" for a in self.assets],
                                   "channels": ["matches", "ticker"]})
            for name, url, venue, message in (
                ("coinbase-orderflow", COINBASE_WS, "coinbase", coinbase),
            ):
                self._tasks.append(asyncio.create_task(
                    self._run_liquidity(name, url, venue, message),
                    name=f"rival-{name}-liquidity",
                ))

    async def aclose(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connected = dict.fromkeys(VENUES, False)
