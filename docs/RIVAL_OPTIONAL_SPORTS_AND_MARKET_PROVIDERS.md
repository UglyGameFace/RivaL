# Optional sports jobs and verified prediction-market sources

## Production behavior

**Default crypto-only behavior:** `RIVAL_SPORTS_BACKGROUND_ENABLED=false` and `RIVAL_SPORTS_ARCHIVE_ENABLED=false`. The bot does not instantiate SportsGameOdds, OddsPapi, or TheOddsAPI clients or start sports polling and sports-results jobs merely because unused API keys exist. Sports collection does not consume provider quota and does not block BTC/ETH/SOL/XRP perpetual collectors, live WebSockets or Polymarket Global boards.

Sports-related **Drive archive scanning** is separately opt-in with `RIVAL_SPORTS_ARCHIVE_ENABLED=true`. The existing crypto/perpetual Drive copy remains enabled independently. No sports rows are removed and no credentials need deleting. If sports are wanted later, enable `RIVAL_SPORTS_BACKGROUND_ENABLED=true` explicitly and configure the appropriate provider keys and monthly reserves; historical sports archiving stays off until separately enabled. This separates the user-facing sports feature from background resource consumption.

The previous `archive/queue.py` executed a full-history GROUP BY `selection_key` twice each archive batch even when there were no sports writes, causing SQL calls as long as **82 seconds**. The new archive candidate check uses the preexisting indexed `(selection_key,id)` correlation to check that **a newer row exists**, protecting the newest state without building a whole-table aggregate. This query is only run when sports archiving is enabled; the query improvement is not an excuse to keep idle sports archiving enabled.

## Prediction market sources

- **Polymarket Global**: already supported live 5m and 15m Gamma/CLOB events and separate exchange-candle research. Not Polymarket US. No approved contract-specific calibrated direction yet.
- **Kalshi**: on-demand read-only BTC/ETH/SOL/XRP public Crypto series and its verified open market listings. Source-exact title, close, YES/NO ask, resolution rules. It is NOT another automatic tracker or independent trading prediction. No keys, no wagers, no unverified events; errors show unavailable. Kalshi market descriptions and settlement must be kept separate from Polymarket/Chainlink.
- **DraftKings Predictions**: offers its own prediction exchange, including crypto and sports, but public market data/API access with equivalent verified contract semantics has **not been implemented or validated** in this change. Sports betting DraftKings prices from SportsGameOdds are not DraftKings Predictions contracts. No scraping or proxying the bookmaker feed as the prediction exchange.

## Discord interaction

Live Mode now names **Polymarket Global** as provider and links to a separate Kalshi browse screen. **Notification Options** sends an immediate new ephemeral advanced-options page without touching the old tracker or waiting for PostgreSQL, Gamma or the shared redraw lock. Start/Stop Auto-Track still only control the original Polymarket Global private board. No automatic Kalshi DMs are added.

## Proof and remaining work

Run Ruff/full pytest/temporary PostgreSQL, verify archived sports newest-state safety, interaction acknowledgement, actual deployed SHA and background job list. Post-deploy seek disappearance of `sports collection paused` and `queue.connect WITH` notices (unless explicitly enabled), and measure PostgreSQL load and Discord action latency. History audits and Polymarket parsing errors are separate unresolved paths; a sports isolation fix alone does not solve them. 
