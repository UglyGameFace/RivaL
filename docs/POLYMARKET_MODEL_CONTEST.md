# Prospective Polymarket model contest

BTC/ETH/SOL/XRP × 5m/15m/60m are twelve separate markets. Settlement source
and payoff type are separate scopes within a market. The market price remains
the benchmark until a candidate passes the same prospective policy as every
other candidate. No LLM opinion, historical accuracy, or incumbent status can
qualify an alert.

## One shared issuance

`RivalDiscordClient._formula_loop` runs without Discord activity. It uses the
existing shared Gamma/CLOB reader and `FormulaTest`, which coordinates all
candidates through `model_league.Contest` on the existing operational database.
New tables are additive/idempotent; old forward, outcome, formula and pooled
ledgers are preserved. Old calls cannot be converted to contest evidence.

The first verified book between 20% elapsed and 20 seconds later is the only
contest checkpoint. Its source book timestamp must itself be after that
checkpoint and no more than 25 seconds old. If the window is missed, no later
call is invented. One transaction records the contract condition ID, source,
payoff, midpoint benchmark, available ten-share costs, evidence digest, and
all available candidate forecasts. A restart or concurrent attempt cannot
rewrite a round or add a later contestant. WAIT and absent models are retained
as abstentions; later prices never supply a retrospective entry.

The probability benchmark is the normalized pair of UP/DOWN bid/ask midpoints
from that book. It is not a purchasable price. Paper entries use ask depth for
ten shares, including each price level's rounded taker fee. Fee parameters
come from that market's `feesEnabled`/`feeSchedule`. Unsupported exponents,
missing fees, or insufficient depth provide no qualified cost evidence.
The published identity-exponent fee is `shares * rate * p * (1-p)`, not
`shares * rate * p*p * (1-p)`. Makers/rebates are not assumed.

## Reviewed hypotheses

- The existing online book logistic algorithm competes using its current
  validated state and this checkpoint's features. Its old first-call ledger
  remains research history, never the contest's evidence.
- Reviewed pooled Binance and multi-venue lineages compete as separate
  artifact-SHA identities. Their start-limited exchange features can be less
  informative than the checkpoint price; that is part of the hypothesis.
  These are prospective challengers, not oracle-native training labels.
- Zero-arithmetic-drift GBM digital uses
  `Phi((log(S/K) - sigma^2*T/2)/(sigma*sqrt(T)))`. This is an assumed
  martingale baseline, not a claim that risk-neutral prices equal physical
  probabilities.
- Fixed EWMA decay 0.94 on closed consecutive one-minute returns is a research
  setting inspired by RiskMetrics; it has not been optimized for these coins.
- Merton-style Poisson/Gaussian jumps form a separate challenger. A fixed
  robust three-scale threshold estimates jump frequency and moments from at
  least 119 past returns and two tail observations. This is a simple shadow
  moment estimator, not maximum-likelihood Merton calibration.
- Queue-weighted microprice is a separate pressure challenger with no invented
  pressure-to-return coefficient. This is queue imbalance, not event-based
  order-flow imbalance; the cited microstructure results do not establish
  crypto prediction skill for RivaL.

Chainlink/USD formula inputs require at least three independent USD spot references from
Coinbase, Bitstamp, Gemini and Bitfinex. Use a median of fresh books and aligned closed minute bars;
reject gaps, stale bars and >1% cross-source divergence. No futures or USDT
price is silently labeled USD spot. Gemini explicitly marks level timestamps
as dummy values: only a fresh, uncached HTTP server snapshot is accepted and
its clock is labeled `http-snapshot`, not an exchange event timestamp.
A missing quorum disables formula candidates while book learning continues.

The displayed formula probability is the frozen checkpoint, restored after restart;
later live ticks do not replace it. The published exact-contract price to beat takes priority. Otherwise the
median of at least three exchanges' opening-minute prices is explicitly a proxy,
not a Chainlink receipt. Raw source clocks and a candle digest accompany the
input receipt. Hourly Binance contracts are a different source and denomination. They require
Binance **spot USDT** books and closed one-minute bars, plus the original O
from that exact hourly candle (or the contract's published price to beat).
Only its already-known opening value is read from the forming hourly candle;
its forming/future close never enters features. USD medians, perpetuals and
other exchanges cannot fill in for these inputs. A blocked/denied Binance
endpoint enters cooldown and leaves the hourly formula absent; book and
other available contestants can continue. Reference currency and venue are
validated and frozen with every formula receipt.
The public reference is never used to settle outcomes.

Trailing 60-second TWAP is an Asian payoff. Before averaging begins, GBM uses
a lognormal match to the exact first two arithmetic-average moments:
`E[A]=S`, `E[A^2]/S^2 = exp(v*(T-w))*2*(exp(v*w)-1-v*w)/(v*w)^2`.
This is an approximation to its distribution, not an exact Asian digital.
The jump challenger uses an explicitly approximate averaging-weight duration
`T-2w/3`; it is not an exact jump/TWAP solution. If any of the TWAP interval
has already accrued, the formula abstains without its observed path. Unknown
or conflicting settlement rules abstain.

## Fixed qualification policy

The policy anchor is 2026-11-07 00:00 UTC. Tests use disjoint 28-day batches
and only outcomes independently observed by two days after batch end.
A model must enroll before the batch starts. A new artifact enrolls for a
future full batch; it cannot inherit the older artifact's results. The contest retains the two latest enrolled recipes at each batch boundary
and the preceding batch's pair, alongside the current two lineages for future
enrollment (at most six artifacts per horizon). Nightly retraining therefore
cannot starve the frozen artifacts under evaluation. Missing coverage remains
unqualified.

Requirements are at least 300 matched resolved contracts, 21 observed UTC
days, 80% of expected contract windows (including downtime), 80% depth/fee
coverage, and 150 predetermined paper entries. Eligible models and the
market are compared on their common contract intersection. Missing models
and markets still consume multiple-testing slots.

Primary score: paired Brier improvement `(market-y)^2 - (model-y)^2`.
A single paper side is chosen only if its forecast probability exceeds its
recorded all-in cost by 0.03; otherwise WAIT. Stressed net paper payout
subtracts a further 0.01/share for adverse price movement. Both positive
Brier skill and positive stressed net performance are required.

Average the paired improvements and net payoffs by UTC day. A DM-style mean
test uses Newey-West/Bartlett covariance with three daily lags and a Student-t
reference. This is approximate under short-memory/stationarity assumptions;
it is not a finite-sample or distribution-free guarantee. Degenerate variance
and insufficient evidence cannot pass. The joint p-value is the larger of
the Brier and economic p-values, followed by Holm across all enrolled models,
12 markets and observed settlement scopes. Batch k spends
`0.05 / (k*(k+1))`, whose lifetime sum is at most 0.05. Restarts and ten-minute
cache refreshes cannot create another look or reset that budget. Late outcome
receipts beyond the fixed cutoff cannot alter a completed test.

Among qualifying candidates, the largest observed Brier improvement selects
the serving model. This establishes evidence against the price benchmark;
it does not establish pairwise superiority to every other qualifying model.
Qualification expires at the next batch decision. It is paper evidence, not
proof of actual fills, trading access, or future profit.

## Serving and failure behavior

The main menu and persistent DM panel share one card renderer. Both displays
and the optional ping use the selected model's immutable
checkpoint call. With no qualifying model, the card says
"No model beats Polymarket's price here" and no prediction ping is sent.
The separate early-call ping route and independent formula unlock were
removed along with their orphaned modules; there is one qualification authority. Research forecasts
can still be inspected with an explicit unqualified label.

A ping additionally needs a fresh current book, known current costs, the same
source/payoff, at least 0.03 estimated edge after the extra 0.01/share stress,
a call issued in the last minute, and over one minute to expiry. Missing or
expired contest state fails closed. Scoring runs off the Discord event loop
and loads one market at a time without raw evidence blobs to bound memory.

## Primary references

- Polymarket fees: https://docs.polymarket.com/trading/fees
- Market fee metadata: https://docs.polymarket.com/market-data/market-details
- Gneiting & Raftery (2007), proper scoring rules:
  https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf
- Diebold & Mariano (1995), predictive accuracy:
  https://doi.org/10.1080/07350015.1995.10524599
- Holm (1979), multiplicity: https://doi.org/10.2307/4615733
- RiskMetrics (1996): https://www.msci.com/research-and-insights/paper/1996-riskmetrics-technical-document
- Merton (1976), discontinuous returns: https://doi.org/10.1016/0304-405X(76)90022-2
- Cont, Kukanov & Stoikov, order-book events: https://arxiv.org/abs/1011.6402
- Coinbase book/candle formats: https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-book
- Bitstamp USD books/OHLC: https://www.bitstamp.net/api/
- Gemini books/candles: https://developer.gemini.com/trading/rest-api/market-data

- Bitfinex USD books/candles: https://docs.bitfinex.com/reference/rest-public-book

- Binance spot order book and candles: https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market
