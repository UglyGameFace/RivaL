# Live Polymarket oracle, cross-market math and DraftKings evidence boundary

## Official source integration

Official Polymarket [PolyBolt WebSocket](https://docs.polymarket.com/api-reference/live-data/overview) runs at `wss://ws-live-v2.polymarket.com/ws`. The `price.crypto.twap` channel returns 60-second Chainlink BTC/ETH/SOL/XRP TWAP prices, **not** cryptocurrency perpetual futures or tradeable YES prices. Auth uses the *three CLOB API credentials* (key, secret, passphrase), never a wallet private key.

Configure only in the **Discloud environment**, not GitHub code, DMs, logs or public files:

```env
RIVAL_POLYBOLT_ENABLED=true
POLYMARKET_CLOB_API_KEY=<your authenticated CLOB API key>
POLYMARKET_CLOB_API_SECRET=<your authenticated CLOB API secret>
POLYMARKET_CLOB_API_PASSPHRASE=<your CLOB API passphrase>
```

The bot does **not** derive keys, access wallets, trade, share credentials or automate account registration. All three are required, and credentials must have permission to receive licensed PolyBolt quotes. Without them the new worker does not start and its status correctly says 'credentials needed'. Official auth rejection similarly leaves the feed inactive, preserving book-only studies. `/rival-tools health` reports the actual configured/connected/fresh state without disclosing secrets.

A single read-only socket authenticates, subscribes to 60-second Chainlink TWAPs for four coins, validates exact source, channel, symbol, timestamps, sequence continuity and dropped frames. Missing sequence numbers reset the volatility sample; reconnects reauthenticate, discard stale observations and use bounded backoff. Snapshots may supply at most the preceding two minutes and are **not** execution history. The ephemeral buffer is bounded, no tick-by-tick PostgreSQL writes.

The PolyBolt cache supplies a reference only when the current, published Polymarket event has the matching Chainlink TWAP source and published 60-second rule, a verified opening price-to-beat, a recently observed TWAP (<=8 seconds old), fresh matching contract books and a valid settlement window. The last ~120 seconds of original same-source 60s TWAP is sampled in non-overlapping ten-second steps and converted into an exploratory *remaining-to-expiry* volatility estimate. Thin, constant, stale, wrong-source or gapped feeds abstain. The sha256 stored is a digest of provider-delivered data over TLS, **not a cryptographically signed Chainlink proof**; these data still require independent hosted observation for production validation.

## Mathematical evidence, not fabricated wins

The existing `polymarket-oracle-context-shadow-v2` logistic model now receives genuinely source-aligned strike-distance and measured volatility inputs when available. Its versioned weights, first observations and verified future outcomes remain isolated from exchange and Kalshi model weights. No override is made of its 100-label warmup or the existing promotion barriers.

An additional `polymarket-oracle-threshold-normal-shadow-v1` candidate freezes a *probability* using a lognormal digital threshold model. Formula: `p_UP = Phi((ln(S/K) - sigma^2/2) / sigma)`, with source TWAP `S`, published strike `K` and measured remaining-horizon sigma. This is a research **hypothesis**, and TWAP increments are autocorrelated; Gaussian tails and zero drift may be wrong in the wild. The first per-contract prediction, original input hash, oracle-source receipt, market-price baseline and genuine future condition settlement are compared using Brier loss. This never becomes a trade recommendation or gets silently promoted because a closed-form formula looks fancy.

`research/contract_probability.py` also reports YES/NO expected value from actual executable asks *only when both fee and slippage inputs are known*. Otherwise it returns `unknown_cost`; even positive mathematical EV is marked `verified_edge=False`. Shared prospectively frozen Brier, log loss and calibration screens can be applied to any provider once exact outcomes exist; training weights, market regimes and results remain provider-specific. Do not pool unrelated Kalshi/Polymarket/DraftKings contracts.

## DraftKings Predictions blockers

DraftKings' June 26 2026 [official DKeX announcement](https://ir.aboutdraftkings.com/news/news-details/2026/DraftKings-Launches-Proprietary-Exchange-to-Bolster-Differentiated-Predictions-Experience/default.aspx) confirms DraftKings Predictions on Sports & Casino and DKeX exchange. The public DKeX `daily-market-reports` and `daily-settlement-reports` are **next-day** publications; they do not supply live, executable DraftKings app contract YES/NO bids/asks, customer fees or routing for every contract. No verified public DKeX live developer market-data API was available when this PR was authored. Do not invent one, use authenticated private web endpoints without authorization, scrape a personal account, or treat historical report prints as fresh quotes.

A DraftKings-compatible mathematical predictor still requires an **authorized** live contract-data entitlement identifying routing exchange, exact contract/condition, source oracle and strike, near-real-time asks/depth, rules, payout receipts, and actual fee schedule. Then build a separately versioned DraftKings-specific point-in-time training and scored forward ledger, with no Polymarket/Kalshi outcome substitution. Until verified access is obtained, DraftKings calls stay **NOT CONNECTED**. DKeX historical report ingestion from PR #129 remains usable, but it is not a model-accuracy claim.

## Acceptance

1. Exact-head Ruff + Python/PostgreSQL full suite and main commit CI, followed by exact Discloud deployment workflow.
2. Verify CLOB credentials are **present only in host environment**, authenticate successfully, and observe real in-sequence Chainlink TWAP data across BTC, ETH, SOL and XRP. Check timestamps/provider and rule-to-market mapping.
3. Verify actual first source-matched oracle v2 observations and math shadow rows in PostgreSQL. With missing credentials the expected count is zero, not an invented observation.
4. Accumulate and independently verify official terminal payouts for the exact condition IDs. Compare per-market/horizon Brier, log loss, calibration and market-implied baseline on **prospective** decisions, using conservative confidence and after-cost executable quotes. Only approve improvement with genuine independent evidence; do not lower existing forward gates.
5. Obtain documented authorized DraftKings live price + settlement access before implementing/activating a DraftKings contract model. Leave it labeled unconnected until a real contract route is verified.
