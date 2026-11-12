# XMarket: native prediction-market prices as perpetual-direction covariates

`xmarket_v1` is an independent offline Platt-calibrated logistic per asset
(BTC, ETH, SOL, XRP) and target horizon (5, 15, 60 minutes). It issues only
immutable shadow forecasts. It does not replace a live model, enter Out
The Gate, send research alerts or DMs, place orders, change promotion gates,
or merge into the separate unified-model design.

The uploaded November 12 spec's named training features add up to **18**,
not 17. The implementation preserves every named input. Source coverage
and usable fidelity must be measured; the existence of an ingest command
is not evidence that the requested history exists.

## The pinned feature contract

| Positions | Inputs | Definition |
|---|---|---|
| 1–10 | Incumbent candle inputs | Reuse the first ten archive features and the live incumbent extractor, with their existing formulas and clamps. |
| 11 | `pm_up` | The verified Up token's price returned by Polymarket `/prices-history`, not `up_ask / (up_ask + down_ask)`. |
| 12–13 | `pm_up_chg_3`, `pm_up_chg_12` | Differences against exact feature timestamps 15 and 60 minutes earlier. Missing lag observations abstain. |
| 14 | `kalshi_up` | The native one-minute Kalshi YES-ask close, read from the actual `yes_ask` candle field. |
| 15 | `kalshi_pm_gap` | Kalshi price minus Polymarket price on the same feature timestamp. These contracts have different settlement sources; the gap is a covariate, not an executable arbitrage. |
| 16–18 | `venue_spread_ret1`, `venue_up_share`, `venue_spread_ret5` | Max/min return dispersion and positive-return share over the artifact's pinned common venue set. |

Only Binance, Bitget, OKX and Gate are dispersion candidates. The set is
the intersection of supplied verified archive frames and actually observed
live eligible streams. Binance is not enabled as a new live provider. The
measured set is frozen in the artifact. If fewer than two venues are
available, all three dispersion features are omitted: the explicitly
identified reduced contract has 15 inputs. A missing pinned venue later
causes abstention; inference cannot silently change the venue denominator.

The feature digest commits to feature order, price definitions, venue set
and PM fidelity. Weights have one additional intercept coefficient.

Both sources are native **15-minute contracts**, including when the model
predicts a separately trained 5- or 60-minute perpetual label. Their
probabilities, confidence and settlement labels are never borrowed as
target-horizon forecasts. Historical prices carry verified asset, native
window, token/ticker identity, observation time and provider hashes.

A source point can supply a feature bar only if it falls in that bar's
final minute and is at or before the bar close. There is no nearest-future
match, interpolation, zero fill or carry to another bar. At a native event
boundary the just-completed window is used (`opens < bar <= closes`).
No whole 15-minute candle is read early. A verified PM 15-minute-fidelity
fallback restricts both offline and live features to the native grid;
the other five-minute rows remain absent.

`pm_spread` and `kalshi_spread` are optional, timestamped diagnostics in
`rival_xmarket_diagnostics`, beside the exact frozen forecast identity.
They never enter the fitted vector. Missing bid evidence remains null.
`pm_vol_z` is omitted because the PM history contract lacks volume.

## Collection and the existing archive pipeline

The existing Polymarket formula loop reuses its shared reader/client to
capture native history, capped at once per minute per contract. Near a
native rollover it also finishes the previous verified contract, so the
reader's final-90-second alert cutoff does not erase the final input bar.
The existing Kalshi Up/Down loop captures native one-minute candles for
the entire currently open 15-minute window, independently of its existing
first-forecast ledger. Those original learning and grading records remain
immutable and retain their existing behavior.

Native observations live in the existing database. Inserts are batched;
changed source prices at an already captured identity fail without
rewriting the first observation. Observation availability is checked at
issuance. HTTP work remains asynchronous, and database work stays off the
Discord event loop.

Polymarket can repeat the final timestamp/price in an otherwise ordered
response. The shared live/history parser collapses only identical adjacent
samples before measuring cadence. Conflicting duplicates, reversed clocks,
invalid prices and future points remain rejected. Provider hashes retain
the original response, including duplicates; no source price is altered.

The existing records-export loop publishes completed UTC-day input bundles
to the existing private Drive report folder. It exports at most three
pending days per asset per pass and resumes older unexported days after an
interruption. Content-addressed uploads survive a crash between upload
and recording the export receipt. **This export now runs independently before
the larger records-backup job**: a failed, slow or timed-out ordinary records
backup cannot suppress the prospective native-source publication in that
cycle. The original memory-pressure guard still avoids running either export
under pressure, and failed exports retry on the existing hourly cycle.
The read-only host receipt separately reports uploaded completed days and
the recent eligible-day backlog; an upload acknowledgment is not proof that
the archive trainer later restored and compared that file.

The existing weekly archive entry point restores these byte-verified
bundles. It independently queries native history for recent captured
contracts to measure fidelity. It also discovers and backfills a bounded
96 earlier native windows per asset/pass, with a saved cursor over a
90-day maximum lookback. An incomplete window keeps its cursor for retry.
XMarket's Kalshi overlap and backfill requests now prefer the live candlestick
tier first, falling back to the archived tier **only on a source 404**;
the ordinary supplemental importer keeps its original archived-first default.
The XMarket path paces public Kalshi calls and makes at most two bounded
HTTP 429 retries with exponential backoff (honoring only a bounded Retry-After).
403, invalid metadata, unavailable candle prices and repeated 429s still
fail closed with typed provider reports, never fabricated native points.
These repairs do not guarantee historical availability or enough
train/validation/test overlap. The four assets run in separate Actions
workers, so server-wide rate limits may still block a pass.
All acquisition uses the existing supplemental manifest, normalization,
provider/normalized hash verification and private publishing functions.
Bound identities distinguish identical Polymarket price paths belonging
to different tokens. There is no second collector service, database,
authentication system, paid vendor or new secret.

Fidelity comparison uses actual feature bars and exact native contracts,
not an inflated count of minute rows. It requires at least 30 matched bars,
80% overlap coverage and no more than 5% price mismatches at a two-cent
tolerance. These are input-quality checks, not a replacement for model or
promotion gates. Reports retain comparison counts, actual cadence and
source errors. Sparse unresolved evidence stays unverified.

Raw provider hashes remain in the verified input bundles. Artifacts keep
actual example provider hashes, per-source hash-set commitments/counts,
and input-bundle SHA receipts. They do not copy months of minute-level
hashes into every model and exceed the existing 2.2 MB import ceiling.
The set commitment is labeled separately from actual provider hashes.

## Training, import and governance

The archive runner adds the family beside archive, GBM and VolCal. Missing
or unverified input history yields a blocked report and no model file;
peer-family training continues. No workflow file is changed. The existing
upload step already includes the complete output directory.

Training reuses the established batch logistic, train-only standardizer,
chronological splits with horizon purge, validation-only Platt fit, and
disjoint test evaluation. Ordinary archive execution supplies the same
28-day validation and test windows as VolCal. The unchanged offline gates
are validation/test Brier gain above 0.005, at least 55% positive test days,
test ECE at most 0.03, and evaluated calibration. All fitted rows must have
complete finite inputs before the shared standardizer runs.

Reports state the effective overlap dates, row count, unique bars,
per-venue frame coverage, fidelity comparisons and one-bar lead/lag
correlation diagnostic. Future returns appear only in that diagnostic and
labels, never in model inputs. Failed gates publish only a report.

Drive import validates scope, schema, model/feature contract, digest,
finite coefficient and standardizer widths, positive standardizer scales,
weight digest, source provenance, price/fidelity semantics, calibration,
reported evaluation metrics and passed gates. The runtime registry mirrors
VolCal's pinned-artifact queue. Refit weights cannot inherit another fit's
shadow evidence or paper promotion.

`ISSUE_XMARKET_V1_CHALLENGERS` defaults to true and controls only this
family's issuance. Missing artifacts or inputs issue zero rows. Existing
rows keep settling through the shared ledger. Replay cannot replace a
forecast or attach a newer artifact's diagnostic to an older forecast.

Route B retains its existing source-exact perpetual quote/cost tests,
150-window/21-day floors, coverage, calibration, baseline and stress
requirements. Prediction-market prices are covariates, not hypothetical
prediction-market purchases; no second fee model is invented. XMarket may
earn a paper-governance receipt, while the explicit live opportunity and
OTG exclusions continue to apply. Lab-grid ablations remain outside this
build, as specified.

## Validation and production evidence

Targeted regressions cover offline/live feature parity; named arithmetic;
missing, future, coarse and conflicting inputs; separately pinned venue
contracts; training/calibration/gates; exact-source provenance; live
capture; private archive restore/export; replay; pin replacement; the
actual forecast-cycle kill switch; alert and live-lean exclusions. Shared
SQLite/PostgreSQL fixtures exercise database behavior in CI.

The existing read-only live-learning host receipt now includes `xmarket`:
captured source counts/timestamps, imported artifact scopes, actual shadow
rows from the last 24 hours and an explicit cold-start status. An imported
artifact without rows is not reported as shadow success. Missing tables
are detected before optional queries; the receipt never initializes them.
Source coverage lists all eight expected asset/provider scopes, missing
scopes and scopes older than 20 minutes. Partial or stale capture remains
`waiting_for_inputs`; a few BTC points cannot imply family-wide input readiness.

Production completion still requires a real passing fit, verified private
publish/import and observed immutable shadow rows. Synthetic tests, a PR,
a merge or a successful restart do not prove those events or a trading edge.

Public API probes on November 12 verified native-history reads for BTC,
ETH, SOL and XRP on both providers, including distinct provider/normalized
hashes. One sampled closed native window returned 15 one-minute points per
source. This is schema/access evidence for sampled contracts, not a full
history census, an independent live-host fidelity comparison or a fitted
model. Temporary provider failures preserve the incomplete backfill cursor.
