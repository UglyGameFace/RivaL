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

## Continuous qualification policy (sequential-betting-v4-replayed-book-cost)

There is no 28-day batch, 21-day minimum, future calendar anchor, or mandatory
48-hour settlement delay. A versioned policy start is saved once when this
version initializes on the host. Only forecasts issued after that start can
qualify; old outcomes are never reused to select a more favorable new test.
**Version 4 does not retroactively promote version 2 or version 3.**
Only source receipts created with replayable ask-depth traces under this policy
can enter new significance tests. Existing model weights and older research
history remain intact.
The prior probability and paper-cost history remains visible as research, but
old or missing issue-time source receipts cannot count toward version 4
significance, paper profit, or a qualified call.
Restarts preserve the policy, enrollment slots, source scopes and forecasts.

Requirements remain at least 300 matched resolved contracts, 150 predetermined
priced paper entries, 80% forecast coverage over expected contract windows,
80% known fee/depth coverage, **80% frozen source-proven cost coverage**,
positive Brier improvement, positive stressed net paper payout, and both
statistical tests passing. Only the exact-contract source-proven subset enters
the Brier and economic betting processes. Missing fee/book provenance is
counted against coverage and never evaluated by hindsight. These are sample requirements,
not a promise that a model will qualify at a particular time. For continuous
coverage, 300 contracts take about 25 hours at 5m, 75 hours at 15m, and 12.5 days
at 60m. Weaker evidence can take longer or never qualify. No synthetic or older
legacy calls count toward these minimums. A cost-source record is inserted
atomically only for a new first issue with a matching condition, paired token
IDs, book hashes, Gamma fee schedule, 10-share paper costs, issue and source
clocks, and a checked receipt digest. Additionally, the exact 10-share
ask-level fills and contemporaneous best bids are frozen in that receipt and
checked against the issued quote. The scoring reader independently replays
the per-level five-decimal published fee formula and refuses unpriced,
out-of-order or mismatched depth, even if the stored numeric cost exceeds
the best ask. Full-book hashes identify what RivaL recorded but, without
exchange signatures, do not prove what the venue served.
The score and display path revalidates
the saved cost columns and exact receipt before treating it as eligible. A
changed SQL cost or fee receipt cannot manufacture an after-fee winner.
These hashes prove RivaL's recorded data integrity, **not** exchange-signed
books, actual fills, or account-specific rounding.

Each model is paired with the exact market snapshot for every contract on which
that model issued a forecast. An absent new challenger cannot retroactively
remove another model's observations by changing a common intersection. Within
each settlement scope, consume the model's resolved contracts chronologically;
a missing or invalid earlier outcome blocks later scores until it is resolved.
Overlapping contracts fail closed. Outcomes require matching condition IDs,
resolution at/after expiry, and observation at/after resolution and by now.
Missing forecast windows and downtime reduce coverage. Source scopes start at
their first observed checkpoint, with model enrollment and the policy start
also bounding eligibility. Evidence over two hours out of date cannot qualify.

The primary proper score is Brier improvement `g = (market-y)^2 - (model-y)^2`.
A paper side is chosen once if its probability exceeds the recorded all-in cost
by 0.03; otherwise WAIT. Stressed net payout is `z = correct - cost - 0.01` for
that side and zero for WAIT. Prices and fees are never chosen after the result.

For each sequence, use a fixed equal mixture of six betting capital processes,
with fractions `(0.01, 0.03, 0.1, 0.3, 0.5, 0.8)`. Each component multiplies
`1 + fraction * value / downside`. Brier downside is the exact worst negative
score difference from the issued probability pair. Economic downside is the
recorded `cost + 0.01`; WAIT leaves capital unchanged. All factors are positive.
Capital is mixed, never selected by the best observed fraction. Log arithmetic
avoids overflow. Under the conditional null that expected score improvement
(or stressed net payout) is nonpositive given the information available before
the outcome, each process is a nonnegative supermartingale. Ville's inequality
therefore allows repeated checks at arbitrary stopping times without spending
another significance level per refresh. No iid assumption or normal/t-reference
approximation is used. This tests conditional forecast dominance; it does not
prove stationary unconditional alpha or performance in every future regime.

Both Brier and net capital must exceed `1 / allocated_alpha`. This is an
intersection-union test: either null being true bounds a joint false pass.
The total lifetime error budget is 0.05. Half is divided equally among the four
declared algorithms; the other half is reserved for pooled artifacts. Artifact
slot j receives weight `0.5 / (j*(j+1))`, fixed before its new-policy outcomes.
Divide each model's budget equally among all twelve markets. Source scope k
within a market receives `1 / (k*(k+1))`, allocated atomically at first capture.
These summable weights and a union bound cover missing markets, future models,
future source scopes and all repeated checks. Existing weights never increase
when other models disappear. Allocation uses enrollment only, never accuracy.

The qualified candidate with greatest observed Brier improvement serves future
cards. A displayed contract's model and initial qualification status are pinned
durably, so a promotion cannot change its direction or upgrade an unqualified
research card into an alert halfway through. Qualification is reevaluated as evidence
arrives and can be lost; stale state expires after twenty minutes. Paper test
success is not verified fills or a guarantee of future profit.

Pooled inference retains the first two enrolled recipes in each period of 301
expected contracts, the preceding pair, and the current two lineages (at most
six artifacts per horizon). These are inference retention periods only; a test
can pass at any point. Selection is independent of outcomes and nightly imports
cannot displace an ongoing trial before enough observations can accumulate.

## Serving and failure behavior

The main menu and persistent DM panel share one card renderer. An available
frozen forecast appears prominently as `Research UP/DOWN • unqualified` until
its model qualifies. The direction reflects which outcome the model considers
more likely; it is not a fee-qualified entry recommendation. Probability and
qualification progress are shown separately. The fixed research display order
is formula, book logistic, then other model IDs, without ranking by results.
There is no preference in qualification or the error budget for this display
order. Before the 20%-elapsed checkpoint, the card shows its expected time;
a missed checkpoint is explicitly unavailable rather than a generic WAIT.

The card still says "No model beats Polymarket's price here" in its qualification
field until a candidate passes. No unqualified prediction ping is sent. The
separate early-call route and independent formula unlock remain retired.

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
- RiskMetrics (1996): https://www.msci.com/research-and-insights/paper/1996-riskmetrics-technical-document
- Merton (1976), discontinuous returns: https://doi.org/10.1016/0304-405X(76)90022-2
- Cont, Kukanov & Stoikov, order-book events: https://arxiv.org/abs/1011.6402
- Coinbase book/candle formats: https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-book
- Bitstamp USD books/OHLC: https://www.bitstamp.net/api/
- Gemini books/candles: https://developer.gemini.com/trading/rest-api/market-data

- Bitfinex USD books/candles: https://docs.bitfinex.com/reference/rest-public-book

- Binance spot order book and candles: https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market

- Henzi & Ziegel, *Valid sequential inference on probability forecast performance*,
  Biometrika (2022): https://arxiv.org/abs/2103.08402
- Waudby-Smith & Ramdas, *Estimating means of bounded random variables by betting*,
  JRSS B (2024), Section 4: https://doi.org/10.1093/jrsssb/qkad009

The existing read-only hosted learning receipt now includes `polymarket_contest`:
policy start, per-market capture/forecast counts, last checkpoint and pinned
card counts. These distinguish capture failures from unqualified forecasts;
a pinned record is not proof of Discord delivery or profitable predictions.
