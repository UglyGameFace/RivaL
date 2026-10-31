# Market selection and model evidence

RivaL's `/rival → Crypto` page is a platform-specific research selector.

| Choice | Actual product | Current RivaL support |
| --- | --- | --- |
| Polymarket | Up/Down event contracts settled under the selected contract's rules | Verified listing/book, experimental contract calls and actual-outcome learner; no demonstrated after-cost advantage |
| Crypto Perpetuals | Crypto long/short futures on Hyperliquid, Binance Futures, OKX, Bitget, Gate, etc. | Existing ten-source **exchange-candle** forecasts and forward-outcome audit, not a Polymarket/DraftKings contract call |
| Kalshi | Its own regulated event contracts | Browse verified markets; no validated Kalshi-specific RivaL forecast |
| DraftKings Predictions | Separate DraftKings crypto/sports event contracts (not the Sportsbook wallet) | Informational only, no verified DraftKings Predictions feed, matched model or claim |
| DraftKings Sportsbook futures | Sports season/tournament outright wagers | **Sports**, not crypto perpetuals; no shared crypto-contract model |
 
DraftKings confirms Predictions is a standalone event-contract product with a
separate wallet and listed crypto markets, while Sportsbook defines futures as
long-term sports wagers (championships, awards, etc.). The presence of a
DraftKings-branded page inside an app does not by itself establish that RivaL
has the contract prices, settlement rules, eligibility or outcomes.

**Why "unavailable" appeared:** The old Crypto Analysis page tried to load
both exchange and Polymarket stats regardless of the selected platform. It
caught all Polymarket errors and showed a generic unavailable statement with
no error log. It also displayed "Betting advantage: Not verified" universally.
This made zero labels, an unreachable PostgreSQL query, incomplete evidence,
and distinct providers look like the same failure.

The new page shows provider-specific results only. Zero verified Polymarket
learning labels is displayed as zero, and its unknown Brier score is explained
without fabricating a win rate. If a timed-out database call occurs, it is
logged with the coin and horizon and labeled an evidence-read problem, not a
confirmed Polymarket provider outage. A separate `not yet` statement explains
the unproven after-cost advantage.

The existing background collector, model training, alerts, model promotion,
Discord permissions and trading controls are **unchanged**. This is a narrow
usability and evidence-correctness patch. RivaL cannot claim DraftKings
Predictions support until lawful/authorized contract prices, exact matching,
settlement receipts and genuine forward model validation exist; no scraping,
guessed odds or imported Polymarket probabilities.

## Root-cause correction for unavailable Polymarket stats

Previously the Polymarket panel called `training_status`, which first queried
the offline-candidate registry, the exchange learning heartbeat, and automatic
exchange model promotion. An exception or timeout in any of those unrelated
paths could hide valid Polymarket outcome counts. The menu now reads directly
through `polymarket_learning_status` using only Polymarket research and
outcome tables. It still fails closed on genuine database errors and logs the
specific asset/timeframe without inventing zeros.
