# Source-matched perpetual quote evidence

This extends existing paper research. A book observation is not an executed fill, and no real-money order is placed. The same source, exact instrument, original forecast/weights identity, genuine exchange and receipt timestamps, base-coin size and bounded entry/exit windows remain mandatory. Every model retains its independent target/day, calibration, baseline and after-cost gates.

| Venue | Native instrument | Public stream | Quantity sent to proof |
|---|---|---|---|
| Hyperliquid | asset / USDC | l2Book | base coins (existing) |
| Binance | assetUSDT / USDT | USD-M bookTicker | base coins (existing) |
| OKX | asset-USDT-SWAP / USDT | books5 snapshots | native contract count × official face value |
| Bitget | assetUSDT / USDT | Classic v2 ticker snapshots | base coins |
| Gate | asset_USDT / USDT | futures.book_ticker | native contract count × verified quanto multiplier |
| Deribit | asset_USDC-PERPETUAL / USDC | book.asset_USDC-PERPETUAL.none.1.100ms | base coins, never inverse USD amounts |
| Lighter | asset / USD (existing research identity) | ticker:verified_perp_market_id | base coins |

OKX's existing order-flow socket is reused for books5, trades and liquidations; there is no duplicate OKX subscription worker. One bounded Bitget public socket is added to the existing hub. Both new sources use application ping/pong and the existing reconnect, source-clock and stale-book bounds. Existing Discord signal reads reuse current RAM snapshots and keep their REST fallback. The proof worker does not poll REST.

## Protocol and unit verification

Actual public endpoints were read on November 15, 2026. OKX instruments confirmed live linear USDT swaps with ctMult=1 and ctValCcy equal to the asset: BTC .01, ETH .1, SOL 1 and XRP 100 base coins per contract. The existing liquidity decoder and quote parser share this one face-value table. An unsupported instrument or malformed/non-finite/crossed/empty/stale/future book is rejected.

Bitget REST /api/v2/mix/market/ticker returned bidPr, askPr, bidSz, askSz and ts. Its actual wss://ws.bitget.com/v2/ws/public endpoint acknowledged USDT-FUTURES/channel=ticker/instId subscriptions and produced matching timestamped full snapshots. The separate UTA lower-case instType/topic/symbol request was rejected by that v2 endpoint; do not substitute it merely because an older documentation URL redirects to UTA. The bounded local running-hub probe observed all four assets on both sources before and after a heartbeat, with no reconnects. This is local provider connectivity evidence, not Discloud acceptance.

## Cost and evidence policy

The original extra-cost scenarios are 14 bps conservative and 80 bps severe, including an 8-bps round-trip fee component. Published standard taker rates are .05% per leg on OKX and .06% on Bitget. The new sources therefore add 2 and 4 bps respectively to both scenarios, preserving all existing slippage/funding stresses: OKX 16/82 and Bitget 18/84 bps. These are explicit undiscounted paper assumptions, not account-specific fee claims or a fill guarantee. CASH is not charged an invented trading fee. Mixed-source aggregates weight each source's actual premium; exact-source family evaluation uses its own venue cost. Numerical qualification minima are unchanged.

The existing watchdog persists only bounded source-health counters and fresh-asset lists in rival_runtime_diagnostics. The authenticated host receipt reads those snapshots without writes or inference. New source registration alone is not proof of eligible entries, paired exits, useful models, or profits.

## Gate, Deribit and Lighter extension — November 16

Actual Gate /api/v4/futures/usdt/contracts metadata confirmed active direct contracts for all four assets, with base units per contract BTC .0001, ETH .01, SOL 1 and XRP 10. futures.book_ticker supplies complete BBO updates with a genuine millisecond result.t clock. Only the USDT endpoint and exact asset_USDT contracts are accepted. Current public contract metadata lists .00075 taker fee per leg, so a conservative 7-bps round-trip premium is added to both existing scenarios: 21/87 bps. No account discount is assumed.

Deribit public instrument metadata confirmed all four asset_USDC-PERPETUAL instruments are active, linear and USDC settled. Its linear-perpetual documentation explicitly denominates order amounts in underlying coins; the contract-size increments are not multiplied into book amounts. The tested none.1.100ms grouped-depth subscription provides full [price, amount] snapshots, not raw incremental actions. Channel and payload instrument identities must agree. The observed .00035 standard taker commission per leg stays below the existing 8-bps round-trip floor, which is preserved along with both 14/80-bps total scenarios.

Lighter's current official documentation supplies a public read-only endpoint, wss://mainnet.zklighter.elliot.ai/stream?readonly=true. The ordinary endpoint rejected this executor; the documented read-only endpoint successfully acknowledged actual public subscriptions. The full BBO ticker includes prices, base sizes, matching top-level/payload last_updated_at microsecond committer clocks, market ID and symbol. Active perpetual metadata confirmed BTC 1, ETH 0, SOL 2 and XRP 7. Spot/inverse/wrong-ID books cannot enter the registry. The existing candle/forecast identity asset/USD is retained; no stablecoin conversion or cross-source substitution is introduced. Published public fees do not reduce existing 14/80-bps stresses or imply that observed books are actual fills.

Three bounded public sockets are added, bringing the hub to seven quote sockets plus the same three optional existing order-flow sockets. Only the latest 28 books remain in RAM; there are no tick uploads, new tables or REST polling in proof collection. Signal reads use genuine current native books and fail closed without one. Existing watchdog coverage caching now checks supported source/currency pairs. The actual running-hub probe observed all four assets fresh on all three new sources at 8 and 33 seconds, with zero reconnects. This is provider connectivity evidence from the executor, not hosted acceptance.

One source/asset/horizon review runs per twenty-minute boundary on the existing five-minute learning driver. All 84 scopes rotate in 28 hours, inside the unchanged 48-hour freshness limit, with no bulk startup evaluation. Numerical qualification minima, entry/exit windows, freshness bounds and source/artifact joins are unchanged.

Hosted operation and genuine eligible entry/exit pairs must still be checked after release. Phemex, KuCoin and dYdX remain unsupported for quote proof: the executor could not establish the official Phemex WebSocket upgrade or KuCoin public bootstrap; observed dYdX order books lacked the required source clock. These observations do not establish a provider outage. Binance's observed connection without usable books also remains unresolved. No local timestamp or alternative venue may fill those gaps.

Primary references:
- https://www.okx.com/docs-v5/en/ — public instruments, contract face values, books5 wire format.
- https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee — standard taker fee example and notional-based calculation.
- https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market — public Classic contract tickers.
- https://www.bitget.com/support/articles/12560603892734 — standard futures fee policy.
- https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel — distinct UTA protocol; actual Classic v2 wire was independently probed.
- https://www.gate.com/docs/developers/futures/ws/en/ — public native futures.book_ticker format.
- https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT — actual public contract metadata (all four assets checked).
- https://docs.deribit.com/subscriptions/orderbook/bookinstrument_namegroupdepthinterval — full grouped-depth snapshots and source clock.
- https://support.deribit.com/hc/en-us/articles/31424969384605-Linear-Perpetual — underlying-coin order amounts for linear perpetuals.
- https://www.deribit.com/api/v2/public/get_instrument?instrument_name=BTC_USDC-PERPETUAL — actual public instrument/commission metadata (all four assets checked).
- https://docs.lighter.xyz/api/websocket.md — public read-only endpoint, full BBO and microsecond last_updated_at.
- https://mainnet.zklighter.elliot.ai/api/v1/orderBooks — active native perpetual IDs.
- https://docs.lighter.xyz/trading/trading-fees.md — published public fee policy; existing stresses are not reduced.

## Binance USD-M bookTicker public route — November 16 follow-up

Binance's 2026 USD-M WebSocket endpoint split assigns individual `{symbol}@bookTicker` to the **public**, not the market, category. The authoritative route is `wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/...`, while /market/stream is for regular market streams such as aggregate trades and mark prices. Official reference: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public#individual-symbol-book-ticker-streams

The deployed quote socket previously used `/market/stream` in both its default constant and its per-asset constructor, explaining a concrete subscription mismatch. The correction builds both URLs from one public-route function; the separate Binance trade collector continues using `/market`. Existing source clocks, `st=1` USD-M filtering (including explicit rejection of `st=2` COIN-M books), max-age bounds, coin sizes, source joins and cost assumptions remain unchanged. A mocked WebSocket integration regression checks actual routing, accepts one fresh USD-M event, rejects COIN-M, and verifies cancellation. This identifies a plausible cause of connected-without-books; only fresh post-release authenticated host evidence can establish recovery.
