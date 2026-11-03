# Official public funding history collectors

BTC, ETH, SOL and XRP have source-specific, original-page acquisition lanes
for **Hyperliquid, KuCoin, Phemex and Deribit**.

- Hyperliquid: official POST /info with type=fundingHistory and exact coin.
- KuCoin Futures: official GET /api/v1/contract/funding-rates with exact contract,
  including XBTUSDTM for BTC.
- Phemex: official public GET /api-data/public/data/funding-rate-history with
  source funding-rate symbols (e.g. .BTCUSDTFR8H), limit=100.
- Deribit: official public GET /api/v2/public/get_funding_rate_history with
  exact requested perpetual instrument. Historical SOL/XRP availability is
  unverified and a failed request is **not** evidence of no market listing.

All four use disjoint, bounded four-day UTC windows, original exchange JSON,
strict contract/time/rate/cap verification, immutable SHA-256 provenance and
post-upload byte readbacks from the private RivaL Drive archive.

The 640-scope historical coordinator now records **16 newly routed family/coin
scopes**, not completed data. Original source page names in the Drive census
remain partial evidence, never certification of complete UTC months, exchange
listing dates, market data availability, sealed holdouts or qualified models.

Scheduled GitHub Actions run once per day with four separate provider-limited
jobs. The existing Binance, Bitget, OKX, Gate and other workflows are untouched.
A failed or throttled original API request remains blocked and is retried
under subsequent scheduled runs, not fabricated or labeled complete.

For manual selection, run GitHub Actions 'Official four-venue funding-history
originals' with a venue and completed `YYYY-MM` (or `auto`).

Official API documentation:
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-public-funding-history
- https://phemex-docs.github.io/
- https://docs.rs/deribit-http/latest/deribit_http/client/struct.DeribitHttpClient.html

No external AI, paid feeds, automated orders, live model promotion or claims
that unsupported historical orderbook/trade/liquidation data exists.
