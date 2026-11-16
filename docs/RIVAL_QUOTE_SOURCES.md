# Source-matched perpetual quote evidence

This extends existing paper research. A book observation is not an executed fill, and no real-money order is placed. The same source, exact instrument, original forecast/weights identity, genuine exchange and receipt timestamps, base-coin size and bounded entry/exit windows remain mandatory. Every model retains its independent target/day, calibration, baseline and after-cost gates.

| Venue | Native instrument | Public stream | Quantity sent to proof |
|---|---|---|---|
| Hyperliquid | asset / USDC | l2Book | base coins (existing) |
| Binance | assetUSDT / USDT | USD-M bookTicker | base coins (existing) |
| OKX | asset-USDT-SWAP / USDT | books5 snapshots | native contract count × official face value |
| Bitget | assetUSDT / USDT | Classic v2 ticker snapshots | base coins |

OKX's existing order-flow socket is reused for books5, trades and liquidations; there is no duplicate OKX subscription worker. One bounded Bitget public socket is added to the existing hub. Both new sources use application ping/pong and the existing reconnect, source-clock and stale-book bounds. Existing Discord signal reads reuse current RAM snapshots and keep their REST fallback. The proof worker does not poll REST.

## Protocol and unit verification

Actual public endpoints were read on November 15, 2026. OKX instruments confirmed live linear USDT swaps with ctMult=1 and ctValCcy equal to the asset: BTC .01, ETH .1, SOL 1 and XRP 100 base coins per contract. The existing liquidity decoder and quote parser share this one face-value table. An unsupported instrument or malformed/non-finite/crossed/empty/stale/future book is rejected.

Bitget REST /api/v2/mix/market/ticker returned bidPr, askPr, bidSz, askSz and ts. Its actual wss://ws.bitget.com/v2/ws/public endpoint acknowledged USDT-FUTURES/channel=ticker/instId subscriptions and produced matching timestamped full snapshots. The separate UTA lower-case instType/topic/symbol request was rejected by that v2 endpoint; do not substitute it merely because an older documentation URL redirects to UTA. The bounded local running-hub probe observed all four assets on both sources before and after a heartbeat, with no reconnects. This is local provider connectivity evidence, not Discloud acceptance.

## Cost and evidence policy

The original extra-cost scenarios are 14 bps conservative and 80 bps severe, including an 8-bps round-trip fee component. Published standard taker rates are .05% per leg on OKX and .06% on Bitget. The new sources therefore add 2 and 4 bps respectively to both scenarios, preserving all existing slippage/funding stresses: OKX 16/82 and Bitget 18/84 bps. These are explicit undiscounted paper assumptions, not account-specific fee claims or a fill guarantee. CASH is not charged an invented trading fee. Mixed-source aggregates weight each source's actual premium; exact-source family evaluation uses its own venue cost. Numerical qualification minima are unchanged.

The existing watchdog persists only bounded source-health counters and fresh-asset lists in rival_runtime_diagnostics. The authenticated host receipt reads those snapshots without writes or inference. New source registration alone is not proof of eligible entries, paired exits, useful models, or profits. Six other perpetual venues still need verified adapters and source-specific acceptance.

Primary references:
- https://www.okx.com/docs-v5/en/ — public instruments, contract face values, books5 wire format.
- https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee — standard taker fee example and notional-based calculation.
- https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market — public Classic contract tickers.
- https://www.bitget.com/support/articles/12560603892734 — standard futures fee policy.
- https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel — distinct UTA protocol; actual Classic v2 wire was independently probed.
