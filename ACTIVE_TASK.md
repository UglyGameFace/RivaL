## October 28, 2026: live mathematical forecast readiness diagnostics

- Autonomous math forecasts and delayed settlement are already scheduled
  after each bounded five-minute collection. This is shadow research,
  **not** automatic bets, guaranteed accuracy or model promotion.
- Show a fresh, read-only **Mathematical forecast readiness** panel on the
  Accuracy tab, distinct from six-month historical backfill counts.
- For each available exchange stream require a recent successful poll,
  recent closed candle, a genuinely time-observed contiguous live streak,
  causal labeled samples, and at least 20 out-of-sample evaluations.
- Report eligible streams, max live streak versus a 65-bar gate, causal
  samples, evaluated examples, and stale/blocked stream counts. Do not
  interpret 9/10 API connectivity as 9/10 trained predictors.
- Historical imported candles first seen in one later batch cannot create
  past predictions or extend the genuine live observation streak.
- Recalculate on each Accuracy navigation/refresh from PostgreSQL; no
  provider calls, Gemini tokens, cached live statuses, trading or writes.
- Add SQLite regression tests for short live series, late backfills,
  causal readiness, and the actual Discord Accuracy view. CI includes
  PostgreSQL integration before merging.

## October 28, 2026: real six-month continuity audit and bounded Gate backfill

- First genuine **five-minute continuity audit** scans the actual six-calendar-month
  window for each venue/asset, checks every UTC grid opening, missing bars, gap
  segments, largest outage, off-grid candles and invalid observed/closed times.
  Snapshot results remain in separate PostgreSQL history-quality records.
- Launch a separate low-priority read-only **Gate Futures 5m recovery worker**
  with at most one 600-bar historical API request per coin every 5 minutes.
  It reads official active-direct-contract metadata, multiplies *contract*
  volume by the official quanto multiplier and imports only closed, aligned
  UTC candles with a real first_observed_at (the recovery date, NEVER
  impersonating a past live sample). Persist cursor, missing windows,
  per-asset progress, blocked-provider cooldowns and count of actual inserts.
- Retry malformed responses conservatively; HTTP 401/403/451 uses a six-hour
  pause, while regular errors pause 20 minutes. A delayed/restarted worker
  cannot move the recovery cursor backward. Provider errors cannot stop
  live collection, shadow math or Discord; no keys, trades or region bypass.
- Perform one venue-spanning asset gap audit each hour (assets rotate across
  UTC hours) and show the latest **gap-checked** state, missing counts and
  Gate recovery progress separately from the original raw row-count target
  in private Math & History.
- These controls are enabled by default for existing PostgreSQL deployments,
  with optional RIVAL_HISTORY_RECOVERY_ENABLED=false to stop bounded repair.
- **Still not complete:** Gate is the first verified candidate with a publicly
  documented ranged API. All ten historical markets do not magically gain
  six months. Other venues have different retention, restrictions, data
  availability and backfill requirements. Hyperliquid API exposes only its
  5,000 latest candles, so obtaining six months requires a different
  source/archive, never fake data. No investment/trading approval, auto
  order creation or evidence of a profitable edge is claimed.
- Automatic point-in-time math/Gemini prediction, outcome settlement and
  shadow-only governance from PR #30 remain active and unchanged.
- Run full PostgreSQL+SQLite regression and CI before merging.

## October 28, 2026: one-PR autonomous dual-track accuracy & governance

**Owner request: all five milestones together in ONE PR.**

- Automatic math predictions: retain the existing point-in-time five-minute
  and fifteen-minute `ForwardAccuracyMonitor`, wired after each five-minute
  market-data collection cycle. Predictions and versioned weights derive only
  from labels already observed; prior historical backfills cannot qualify as
  genuine forward outcomes. No betting orders.
- Immutable feedback: math settles matching exchange future candles once.
  New independent Gemini research uses a four-hour UTC slot per coin, reuses
  RivaL's budgeted `GeminiResearch.ask` across ALL healthy sources, and
  persists an issue-before-outcome `perp_auto_ai_forecasts` ledger. Settlement
  requires a future, observed, same-venue/same-contract price, with ties neutral.
  Uncertain AI calls count as abstentions, not wins. Slot keys, evidence hashes,
  and all settlement columns are PostgreSQL restart-safe.
- Continuous mathematical learning: existing `walk_forward` online logistic
  weights are recomputed every eligible collection cycle using only matured
  labels. A new **shadow-only model governance gate** reviews the last seven
  days of prospective Brier versus base-rate, using unique five-minute UTC
  market windows (NOT summed correlated exchange bets). Requires >=100
  distinct target windows across >=7 UTC days and >=0.005 Brier improvement
  before being marked `qualified_shadow`. Failure or deteriorating evidence
  removes that status on subsequent reviews; nothing silently goes live.
- Gemini auto analysis: if an existing Discloud API key is available and
  `RIVAL_GEMINI_AUTO_ENABLED` (default true), run at most one per asset per
  4-hour UTC slot, max 18 candidate calls per day for BTC/ETH/SOL, leaving
  room under the existing RivaL 24/day cap for manual use. Existing rate-limit
  breaker/caching still rules; no Gemini generation on every five-minute
  candle. No AI-based trades or uncalibrated probabilities.
- Live research Accuracy UI: show 5m/15m distinct forward-market windows,
  Brier, calibration gap, meaningful 7-day comparison only with >=100
  windows in BOTH periods, shadow qualification/withdrawal, and hypothetical
  cost-adjusted bps (20 bps round trip, NOT an executable PnL estimate).
  Show separately how many Gemini forecasts were issued/settled, abstentions,
  and hit rate for directional decisions. Never call Google to load analytics.
- Each subsystem fails separately; missing Gemini keys or HTTP 404 must
  not break collection or mathematical assessment. Existing ten exchange
  integrations and regional restrictions remain unchanged.
- Scope limitations: six months of historical observations are STILL a
  backfill/gap-audit goal; model qualification is for SHADOW research only,
  not a trading permission or proof of profitable edge. Trading execution,
  live post-cost fills, funding/slippage accounting, cross-venue transaction
  weighting, and automatic real-money bets are NOT claimed.

## October 28, 2026: forward-only model accuracy ledger and live research status

- Owner's 2:28 PM deployed Discord screenshot showed 8/10 recently collecting
  BTC streams, latest checks ~2 minutes old, latest shared candle ~3 minutes
  old, and 1,085 / 527,040 stored BTC records (0.206% upper bound).
  These are data-health/coverage metrics, NOT predictive accuracy.
- Add private **Accuracy** screen in /rival-research. Compute it from the
  current PostgreSQL forward shadow ledger on every visit, no static embed
  cache, no Gemini/HTTP request and no model promotion.
- On the five-minute post-collection timer (separate from hourly shadow review), train only with time-released historical
  labeled examples, then **persist a future-candle forecast before that
  candle could close** for 5m/15m horizons. Immutable per-venue/candle keys,
  same-exchange later settlement, confirmed first-observation timestamps and
  idempotent scoring prevent backfills and duplicate refreshes fabricating wins.
- Expose 7-day issued/settled counts, hit rate, model vs baseline Brier loss,
  source count and possible prior-7-day comparison only when both periods
  have >=50 outcomes. Flag overlapping/correlated venue data; NOT a set of
  independent bets or proven post-cost edge.
- Cover all 30 potential exchange/coin streams in bounded, deterministic 10-stream round-robin sets across consecutive five-minute cycles, avoiding stream starvation and retraining cost spikes.
- PostgreSQL integration test confirms issuance/settlement and idempotent restart-safe audit persistence.
- UI always rereads feed health and newest completed candle from PostgreSQL,
  showing last check time separately from last candle close and specifying
  five-minute REST polling, not tick-level real-time data. Refresh button
  invalidates the expensive two-minute history-only cache.
- This is a **prospective audit**, still a research-only shadow model.
  No auto orders, betting signal, confidence guarantee or unverified
  six-month historical data. Long-term history/gap work stays open.

# RivaL Active Task Record

## Active task / outcome

Convert RivaL's existing sports-only Discord parlay application into an **automatically evaluated and retrained, read-only crypto perpetual-futures prediction bot**, using verified no-cost public market-data APIs and the existing authoritative Discloud PostgreSQL runtime.

Source request: GitHub issue #6. Start from `main@fb28a85523891c8251d8c2e5814459817f4c931c` after verified completion of PostgreSQL cutover.

## Scope

- Inspect current `/rival` entry point, sports collector, provider adapters, normalization, persistence, jurisdiction/onboarding, parlay UI, archive/runtime, tests, and deployment dependencies before replacing behavior.
- Confirm supported public derivatives data endpoints, limitations, legal/geographical access restrictions, rate limits, venue symbol and contract identity, funding, open interest, and historical retention. Do not assume source interchangeability or combine venue-specific prices as if identical.
- Keep PostgreSQL authoritative and Drive as optional, separately authenticated cold storage. Preserve existing user data, legacy compatibility until explicitly migrated, and safe rollback.
- Build point-in-time candles/feature/label datasets, horizon-specific probability predictions, walk-forward evaluation, calibration, outcome settlement, autonomous model-training triggers, model version/rollback, quality/drift monitoring, and visible user-facing metrics.
- Preserve Discord private application-command workflows while replacing sports-specific screens with perp prediction screens. Do not enable actual orders, exchange credentials, leverage, or unattended trading.
- Free-first, Android-manageable, Discloud-friendly, low-resource and rate-limit-safe. Do not claim predictive edge without tested out-of-sample evidence.

## Status

**Active: all 10 perpetual adapters implemented; NINE venues now production-evidenced for BTC, ETH and SOL, Binance host blocked by HTTP 451.** PR #13 activated five-minute PostgreSQL ingestion, PR #14 integrated Bitget/Gate, PR #15 delivered the interactive market dashboard, PR #16 added OKX/KuCoin, PR #17 Deribit/Binance, and PR #18 Lighter/Phemex. Merged CI covers the adapters with mock HTTP, SQLite replay and PostgreSQL 16 integration tests. Owner's October 28 8:00 AM local Discord screenshot verifies fresh stored BTC candles from Hyperliquid, dYdX, Bitget and Gate. **Owner's 09:22–09:22:36 EDT Discloud log verifies Hyperliquid, dYdX, Bitget, Gate, OKX, KuCoin, Deribit, Lighter and Phemex all returned 200 and completed collection on BTC/ETH/SOL.** Binance exchangeInfo returned HTTP 451 from Discloud and was paused. Previously saved candle replays produced new=0 (deduplication), not evidence of failed collection. No account region classification or Binance routing workaround is inferred. Deployment may reveal geo blocks or real schema differences; only successful recent PostgreSQL source health and preserved historical data count as usable. ETH/SOL health, sustained uptime, automatic multi-source prediction generation, calibrated walk-forward accuracy, mature-outcome feedback/retraining and Discord signal UI remain unverified or incomplete. Issue #6 stays OPEN.

## October 28, 2026: live research UX, verified candle freshness, UTC scheduling

- Owner's 1:37 PM BTC screenshot: Math & History selected with green
  DISABLED control, Gemini appeared inactive. Replace disabled/gray tab
  controls with both clickable colored navigation and an explicit
  blue "Update live stats" action. Retain private view permissions.
- The research UI now reads actual PostgreSQL feed health on EVERY
  navigation/refresh; only status=ok with a recent last successful sync
  AND a recently completed candle is counted as actively collecting.
  Include last collection check time and Discord embed timestamp.
- Store the last actual closed-candle timestamp from every successful
  collector batch. A successful HTTP response without recent candles is
  NOT a live market sample.
- Show the actual NO SIGNAL reason rather than an unhelpful generic
  statement. Do not convert past movement into a fabricated bet.
- Six-calendar-month aggregate history counts can be cached in the
  user's private view for up to two minutes per asset; pressing
  Update live stats clears it. Current source health and market pulse
  must NEVER use that cache. Gemini's evidence-keyed database cache and
  rate limits remain independent.
- Background ingestion is tied to the NEXT UTC five-minute candle
  boundary + 15 seconds to account for finalization, not five minutes
  after the previous serial network batch finishes. The initial
  collector run remains immediate; blocked venue pauses remain intact.
- Discord ephemeral controls can update when interacted with, but
  must not claim an infinite push-stream or tick-level updates.
- Add UI, source timestamp, cache invalidation, freshness and UTC
  cadence tests, with production deployment verification still required.

## October 28, 2026: automatic evidence-only shadow-learning evaluation

- Existing 10-venue collection and nine producer-verified venue feeds are preserved,
  not replaced. Automatic Discloud update after GitHub merge is a deployment
  expectation, NOT a substitute for runtime log and DB verification.
- A separate hourly, bounded shadow evaluator uses persisted first-observed
  candle timestamps, settled target outcomes and the existing walk-forward
  logistic model. It re-evaluates only ten streams per cycle, rotates across
  active streams, and stores observed counts, Brier and baseline scores in
  PostgreSQL; Gemini and exchange APIs are not called by this worker.
- The Math & History research panel shows measured outcomes after at least
  50 causally valid settled evaluations per stream; before then it truthfully
  reports insufficient evidence. No model approval, live trade signals or
  leveraged orders; the original collector and sports command remain live.
- This is a production-deployable shadow-monitor implementation awaiting
  exact-head CI and subsequent Discloud log/database evidence. Full model
  registry, cross-venue probability synthesis, calibration and promotion
  remain explicitly incomplete.

## October 28, 2026: prioritize owner's five-minute perpetual trades

- User trades five-minute perpetuals. Use next-**5m** forecast horizon
  for Gemini research by default and retain 15m/1h/4h/24h windows as
  contextual market information, not a 15m forecast by accident.
- The existing automatic shadow learner already evaluates both 5m and
  15m outcomes; do not claim it has passed live signal promotion tests.
- Add a Math-screen market pulse showing the **previous completed 5m**
  return, up/down source agreement, 15m/1h context and provenance.
  This is descriptive market data; it is NOT the probability of the
  next five-minute move. NO SIGNAL when fewer than three fresh venues.
- Use source-level live success and closed-candle age guards (seven
  minutes) to avoid stale evidence in a rapid five-minute market.
- The bot must eventually recommend a target/stop for the user's market
  and 5m horizon, with optional custom target, fees/funding/spread/
  slippage, and independently validated target-hit likelihood.
  **No target or take-profit probability is implemented or verified in
  this pulse slice. Never invent or sell a market pulse as a wager.**
- Keep all ten configured providers and nine independently verified
  feeds intact; Binance HTTP 451 remains paused without circumvention.
- Add 5m evidence, pulse and Discord rendering tests, then exact-head
  Ruff/pytest/PostgreSQL CI before merge.

## October 28, 2026: Gemini advertised model list relevance fix

- User's 1:05 PM Discord screenshot showed "Configured: gemini-3.8-flash"
  followed by an alphabetical 2.5/3.1 list. The UI showed only 12
  lexicographically sorted models, NOT a capability ranking.
- Update Check models to enumerate all catalog pages, keep only text
  models advertising generateContent, and order alternatives newest
  version first. Exclude image, speech, TTS, computer-use and embedding
  product variants from this perpetual market research selector.
- Place RivaL's configured model FIRST with clear whether it was
  advertised in the COMPLETE model catalog. Do not confuse catalog
  listing with a verified successful generation or claim API access.
- Keep the one-hour metadata cache and no-key-leak rules. Metadata
  checks do not reserve new Gemini generateContent requests.
- Add pagination, filtering, ordering and explicit absence regression
  tests. Keep primary ten-source collection independent.

## October 28, 2026: exact Gemini pause diagnostics for research menu

- Owner's 9:38 AM Discord screenshot: Gemini UI crash is fixed, but
  Analyze displays the ambiguous "budget/cooldown reached" message.
- The PostgreSQL ai quota table already tracks used requests, last request
  and provider block until. Expose read-only ai_request_status with exact
  reasons and retry timestamp; no guessing and no resetting the breaker.
- Separate a provider-error pause, RivaL's own minimum request spacing and
  RivaL daily request ceiling. Show the active model, count/limit, exact
  UTC retry rendered in the user's Discord timezone, and keep Google's
  independent project quotas distinct.
- Calls to inspect the budget never spend requests or refresh the pause.
  Collection, math shadow track and cached successful Gemini results remain.
- Regression tests verify restart-safe quota read, 404 pause, daily reset
  and Discord UI rendering. Require CI before deploy.

## October 28, 2026: production Gemini HTTP 404 + Discord View crash hotfix

- Owner Discloud live logs verified 9/10 venues completed BTC/ETH/SOL
  collection; Binance HTTP 451 access blocked at the Discloud host.
  Data is continuing on nine others, no bypass or replacement claim.
- /rival-research Analyze button had a real Discord View detach bug:
  _rebuild() calls clear_items(), so self.view is None afterward.
  The callback must retain a stable dashboard reference before rebuild.
- The configured gemini-2.5-flash returned HTTP 404. Read-only model
  documentation now recommends newer Flash models. The default is
  gemini-3.8-flash; legacy 2.5 Flash is translated once to 3.8 for
  this deployment to stop known-bad repeated requests.
- Gemini model listing is an explicitly clicked, one-hour cached,
  read-only metadata diagnostic; advertised generateContent capability
  is NOT a guarantee of project-specific generation permissions.
- Gemini 404 now opens a persisted one-hour circuit breaker, displays
  a clear actionable explanation and leaves Discord interactions and
  PostgreSQL collection running. Never show keys or generic stack traces.
- Additional tests exercise 404, crash regression, new model selection
  and metadata caching; validate exact-head CI before merge.

## October 28, 2026: independent Math and Gemini tracks, with restart-safe caching

- Owner wants six calendar months of genuine, audited history and two separate
  prediction systems: deterministic statistical learning and optional Gemini
  analysis over ALL healthy exchange feeds, not a prose rewrite of three prices.
- Implement /rival-research: Math & History displays *actual* six-month
  candle coverage; mathematical model is still shadow-only and not a
  calibrated, automatically retrained production predictor.
- Gemini reads PostgreSQL records from all fresh independent venues, compares
  15-minute, 1-hour, 4-hour and 24-hour returns/volatility, plus any available
  7/30/90 day and 6-calendar-month baseline returns, then makes an independent
  15m research assessment. It never bypasses missing/blocked sources.
- New PostgreSQL 16 integration test verifies atomic quota, idempotent stored
  AI responses and persisted provider pause across service instances.
- Evidence SHA-256 keyed Gemini response cache in PostgreSQL survives
  bot restarts. Atomic per-model UTC-day quota and cooldown persist across
  processes; identical evidence triggers ZERO new Gemini requests.
- Existing Discloud GEMINI_API_KEY or GOOGLE_API_KEY is read from Secrets;
  absent keys disable Ask Gemini without impacting collection.
- A manual Discord button is the only Gemini trigger. No automatic hourly
  calls, training claims, bets, order access or profit guarantees.
- This is a candidate research layer, not mathematically proven accuracy.
  Do not label Gemini's self-reported confidence as a calibrated probability.
- Six-month historical backfill, gap/availability auditing, real out-of-sample
  skill metrics, settled evaluations and automatic model promotion remain
  active implementation milestones.

## October 28, 2026: Source Coverage and AI readiness transparency

- Add a visible Source Coverage screen to /rival-data, one exchange at a time.
- Distinguish HTTP access denial *from the Discloud host* from legal
  regional availability of a Discord user, which cannot be inferred.
- Show all BTC/ETH/SOL statuses, last attempts, and up to three **other
  verified fresh** feeds per asset, explicitly as independent data
  alternatives NOT silently substituted into predictions.
- Explain rate limits, timeouts, malformed provider responses, stale sources
  and bounded automated cooldowns with no geographic bypass.
- Dashboard Overview clearly labels Math as shadow-only and Gemini as
  not connected, preventing collection from being mistaken for learning.
- This is a read-only Discord interface; no change to collectors, database,
  automated trading or credentials.

## October 28, 2026: mobile pagination for ten-provider Data Center

- With ten configured venues, BTC/ETH/SOL Market screens must not regress
  into a ten-item endless list. Display at most four exchanges per page.
- Add Previous/Next buttons, accurate disabled states, Page 1/3 indicators,
  reset on return to market view, and per-page PostgreSQL candle fetching.
- Preserve private ephemeral controls and the unchanged automatic collector.
- Add regression tests for all three pages, navigation, limits and reset.

## October 28, 2026: completion of ten coded candidate adapters

- Lighter public orderBooks and bounded candles (market_id derived from active
  perpetual symbol metadata). Distinguish spot from perp, request seconds
  versus returned millisecond candle timestamps, and base-currency volume.
  Label venue-specific USD quote, no fake USDC settlement assertion.
- Phemex public products perpProductsV2 and USDⓈ-M hedged-perpetual candle
  rows. Validate listed PerpetualV2 USDT instruments and unscaled base volume;
  reject inverse, spot, delisted and malformed rows.
- Registered both sources with the existing 5-minute collection worker
  and venue-aware /rival-data menu.
- **10 coded candidates only, not 10 proven production sources.** The last
  user screenshot verified four live BTC feeds; the six newer integrations
  must be deployed, observed, and repaired individually if their real APIs
  respond differently or region-block the host.
- The production forecast engine and outcome-based retraining are not done;
  issue #6 remains active and must not close on source count alone.

## October 28, 2026: Deribit and Binance perpetuals adapters under CI

- Add public linear USDC perpetual data from Deribit: validate genuine
  active USDC perpetual instruments, and capture base-coin TradingView
  OHLCV with the official minute-resolution API.
- Add Binance USD-M futures exchangeInfo + 5m klines for actual
  USDT-settled PERPETUAL contracts; base-currency volume and actual
  trade counts remain source-specific.
- No API keys, trade permissions or orders. Region-blocked Binance
  must fail closed with a six-hour cooldown; never circumvent restrictions.
- Eight venue adapters are now coded, but only four have live BTC
  evidence in the owner screenshot. Do not mark new feeds live until
  Discloud reports actual persisted successful collection.
- Four source adapters still needed for ten; the production model
  prediction/retraining loop remains a separate milestone in issue #6.

## October 28, 2026: OKX and KuCoin futures integrations under CI

- Extend automatic read-only collector with OKX V5 linear USDT swaps and
  KuCoin UTA V2 USDTM futures candles backed by public contract metadata.
- Verify market symbols, active/linear/perpetual contract type, complete
  candles and native quote. Convert trading volume from **contract units**
  with published per-contract multipliers, not using unverified fake base volumes.
- Preserve distinct BTC KuCoin symbol XBTUSDTM and all venue identifiers;
  update BTC/ETH/SOL market dashboard lookup to use exact source symbols.
- Failure of either API cannot stop the existing four live venues;
  blocked/rate-limited feeds must fail closed with visible source health.
- **Live deployment evidence remains four BTC sources only.** Newly
  integrated venues must not count as confirmed until Discloud reports
  successful persisted candles and current health.
- Six coded adapters after merge; four still need implementation to reach
  the ten-source owner requirement. Model training/prediction remains unfinished.

## October 28, 2026: real private Discord data dashboard

- Replace /rival-data's unbounded one-embed source list with a responsive
  multi-screen ephemeral dashboard built using Discord Views and Selects.
- Overview shows fresh/configured markets, live venue count, persisted candle
  total, last successful sync and ten-source goal as distinct values.
- Exchanges screen shows one chosen venue at a time with asset-level health
  and last success; user switches venue from a dropdown.
- Markets screen shows a chosen asset's latest stored venue-specific prices,
  quote denomination and candle age; never computes a false cross-venue mean.
- Overview/Exchanges/Markets/Refresh buttons navigate in place and re-read
  authoritative PostgreSQL on demand. Another Discord user cannot control
  the invoking user's menu. DB errors give a safe message, not credentials.
- A stale timestamp cannot be called live just because the last result was
  "ok"; a venue without records remains explicitly waiting.
- No changes to background collection or the sports /rival UI.

## October 28, 2026: additional feed integration and Discord status fix

- Add two newly documented candidates to the collector: Bitget V3 USDT-FUTURES
  and Gate V4 direct USDT futures (BTC/ETH/SOL).
- Bitget API provides base-coin volume. Gate reports contract-size volume;
  fetch its public contract metadata and convert to base units using
  `quanto_multiplier` only for direct, active contracts.
- Unknown source trade counts use an explicitly documented -1 sentinel, NEVER
  zero, and no forecast model can interpret it as a measured count.
- Keep original venue/quote identifiers; do not combine prices naively or
  mislabel historical bars as live observations.
- Actual feed availability from Discloud remains UNVERIFIED; four adapters
  merged/CI does not imply four successful production feeds. Request status
  and failure diagnostics from /rival-data after deployment.
- Fix literal escaped newlines in /rival-data and show dynamically observed
  active sources against the owner's ten-source target.
- This is a code-integration step within active issue #6, not model completion.

## October 28, 2026: automatic collection activation

- Owner identified that no existing code ever scheduled perpetual collection;
  the previous night DID NOT collect observed live market history.
- Fix wires an automatic worker into the existing Discord process on PostgreSQL
  at Gateway readiness and every 300 seconds, ON by default with optional disable.
- Each venue/asset isolates errors and writes to the existing authoritative database.
  Blocked/rate-limited hosts enter a cooldown; no bypass or high-rate retries.
- /rival-data is private, Android-friendly and reports persisted successes,
  failures and total inserted candles from real database records.
- Do not mark this LIVE until the new revision is confirmed running in Discloud
  with OK collection logs and nonzero PostgreSQL data. GitHub CI alone is insufficient.
- Subsequent model training must keep backfilled history separate from
  genuine time-stamped collection observations.

## October 28, 2026: bounded market-data ingestion slice

- Active task remains the entire perpetuals conversion, not a separate feature.
- First official no-key source: Hyperliquid POST Info (metaAndAssetCtxs and candleSnapshot).
- Isolated market adapter, immutable shared-store candle/context tables, owner-invoked
  public probe and explicitly opt-in PostgreSQL ingestion, mock-HTTP regression cases,
  plus PostgreSQL 16 compatibility test. No automatic polling, Discord changes,
  sportsbook code edits, trade execution, or exchange credentials.
- Data provenance: source/contract/USDC quote, interval, UTC candle boundaries,
  observed-at timestamp, base-asset volume/OI, mark/funding snapshot.
- Fail closed on 403/429, schema discrepancies, future/forming bars and changed
  historical candles. Limits: 10 assets, 200 bars/request, no source retries.
- PR #10 merged at `fdc95c75c1a3876d6c660984693f138292935599` after a reviewed, focused 7-file diff and passing CI (81 tests, Ruff, PostgreSQL 16); merged main CI also passed. Live regional access from Discloud and supervised backfill are **not yet verified**; test fixtures cannot establish that. Avoid model-training claims.
- Immediate next step: run the read-only `python -m parlay_bot.perpetuals.probe` on the actual Discloud host and record safe response status. Only then authorize `--persist` after reviewing source health, and develop the second independent exchange adapter. Do not activate automatic ingestion or forecasts until runtime safety and quality checks pass.

## October 28, 2026: second independent venue, dYdX v4 (under validation)

- Public dYdX Chain v4 Indexer provides BTC-USD/ETH-USD/SOL-USD
  perpetual-market and 5m/15m/1h/4h candle endpoints (no Kraken).
- Implemented separate bounded collector; USD quotes, exact ticker identity,
  candle-close timestamps and base-volume, no invented mark/funding values.
- Market storage now accepts legitimate candle-only sources, with the existing
  immutable, atomic PostgreSQL writes and unchanged legacy sports tables.
- Added a provider selector to owner-invoked read-only probe. No automatically
  scheduled collection, unapproved exchange access, or trading permissions.
- This is a code-integration candidate: Discloud's regional HTTP reachability,
  provider responses and data quality remain unverified until the real probe.

## October 28, 2026: point-in-time feature and shadow learning evaluation

- New feature extractor refuses chronology gaps, mismatched contracts, and
  later-backfilled candles as if they had been observed before old predictions.
- Read-only walk-forward logistic shadow evaluator trains **only** on matured
  prior labels, compares probability quality to a rolling baseline, and does
  not deploy a model or make trading recommendations.
- Explicit owner-invoked PostgreSQL evaluation command reports causal sample
  counts and metrics. No cron scheduler, exchange keys, or old Discord changes.
- Tests exercise source identity, simulated feedback delays, absence of
  look-ahead and baseline measurements.
- Active conversion remains unfinished until a sufficient live sample of
  permitted source data, successful evaluation, controlled model versioning,
  and Discord end-to-end operation are proven.

## Findings / authoritative execution path

`main.py` -> `parlay_bot.discord_app.bot.run_discord_bot` ->
`RivalDiscordClient` -> shared `RelationalHotStore` / `PostgresHotStore` ->
`LocationOnboardingService`, `CurrentBoardCollector`,
`ParlayBuilder`, and `ColdArchiveRuntime` -> `/rival` and views.

The first two source integrations are Hyperliquid (USDC perpetuals) and dYdX v4 Indexer (USD perpetuals), both bounded and read-only but **not verified reachable from Discloud**. Kraken is excluded per owner request, and no source geographic restrictions may be bypassed. Provider-specific contract identities and missing data must be preserved.

## Changes / validation / cleanup / conflicts

- Current main includes ten registered read-only perpetual market providers, a scheduled collector, immutable `perp_market_candles` storage, feature extraction, walk-forward research, and private market dashboards. **Do not replace that existing working architecture with a new duplicate adapter or schema.** This task's targeted regression adds optional `as_of` first-observation + closed-candle filtering to the existing `PerpetualMarketStore.recent_candles`, with SQLite/PostgreSQL coverage. Runtime and historical data remain unchanged.

- Added the first isolated conversion code on `feat/rival-perps-forecast-ledger`: source-provenance-aware probability fusion (`perpetuals/fusion.py`) and an idempotent, transactional forecast/outcome ledger (`perpetuals/ledger.py`) using the existing relational database. Added local SQLite unit tests and PostgreSQL integration coverage plus `docs/PERPETUALS_LEARNING.md`. The runtime entry point is intentionally unchanged.
- Existing PostgreSQL/Discord runtime on `main` remains unchanged.
- Backlog unrelated requests; retain one active implementation task, including verification and cleanup.
- The previous PostgreSQL cutover has passed GitHub CI and a user-observed Discloud restart/persistence test, recorded below.
- No live exchange access or model quality can be claimed from docs alone.

## Blockers / risks

- Exchange/public API availability from the actual Discloud region is not yet verified.
- A statistically useful training dataset must first be collected/backfilled, with no future leakage. Data scarcity may require a conservative baseline and a shadow model.
- Google Drive OAuth refresh/token lifecycle and archive uploads remain **separate unverified functionality**. Leave archiving off until authenticated validation; do not confuse Drive readiness with the PostgreSQL cutover.
- Trading access and legal eligibility vary by region; read-only analytics must not circumvent access restrictions.

## Next step

PRs #9-12 contain the tested outcome ledger, two bounded public feed adapters and a shadow-only time-safe learning evaluator. Next, validate read-only Hyperliquid and dYdX probes on the actual Discloud host, check permitted regional access, and conduct supervised immutable ingestion. Add a third independent verified venue before meeting the existing 3-source fusion quorum. After collecting genuinely observed time-series data, measure out-of-sample calibration against baselines; only then build health-gated scheduled ingestion, automated retraining with model versioning and rollback, and a crypto Discord UI. Backfills are not live prediction evidence. No trading credentials or profit guarantees.

---

# Completed prior task: Discloud PostgreSQL production cutover

## Closure evidence (2026-10-27)

- PR #5 merged the shared PostgreSQL implementation, with PostgreSQL integration regression tests passing.
- PR #7 added a secret-safe startup backend log; exact-head push/PR GitHub Actions CI passed, as did the merged `main@fb28a85523891c8251d8c2e5814459817f4c931c` run.
- Owner-supplied Discloud startup logs at **22:58 local** show `RivaL operational storage initialized (backend=postgres)`, one command synchronized to guild `1514374173517152418`, and a Discord Gateway connection.
- A freshly invoked `/rival` at **23:01 local**, after that process restart, directly displayed the previously saved Connecticut (CT) selection without reentry. This verifies persistence across the deployment restart through the currently configured PostgreSQL runtime.
- The `403 Missing Access` from 22:41 was resolved by installing the Discord app in the server, not by altering the guild ID.
- No production DB URL or credentials were exposed. Cold archive uploads, SportsGameOdds live refresh and perpetuals models are *not* implied verified by these checks.

Prior task scope, findings, original test and compatibility results are archived below as historical evidence. References to outstanding production verification there describe the **pre-closure snapshot**, not current blockers.

# Historical PostgreSQL Cutover Record (pre-closure snapshot)

## Project

RivaL

## Repository boundary

RivaL development belongs only in `UglyGameFace/RivaL`.

Do not write RivaL code, workflows, task records, branches, or pull requests to
`UglyGameFace/sports-parlay-bot`.

## Active task / outcome

Replace RivaL's production SQLite hot/state database with a Discloud-managed PostgreSQL database
without changing the user-facing odds, jurisdiction, parlay, or archive behavior.

Branch: `feat/discloud-postgres-storage`

Production baseline: `main@46986f7aada639b649a01e976c082dcaa2e82d9a`

## Scope

PostgreSQL must become the single authoritative production store for:

- fixtures and current sportsbook prices;
- meaningful line-state history;
- saved state/jurisdiction selections;
- shared SportsGameOdds refresh gating;
- OddsPapi market/outcome catalog metadata;
- Google Drive cold-archive batch bookkeeping and source-row archive markers.

SQLite is retained only as an explicit local/test compatibility backend.

## Status

Implementation is complete on the feature branch and the runtime/storage changes are validated in
CI. Production activation still requires the real Discloud PostgreSQL instance and its private
connection URL to be supplied through deployment configuration.

## Findings / root cause

RivaL previously had several direct SQLite owners rather than one production database boundary:
`SQLiteHotStore`, `MarketCatalogStore`, and `SQLiteArchiveQueue` each opened the local database
independently. The Discord runtime also constructed them separately.

A simple connection-string replacement would therefore have left part of the application on local
SQLite.

SportsGameOdds also uses string provider identifiers such as `BASKETBALL` and `NBA`. SQLite
accepted those values in columns declared `INTEGER` because of its dynamic typing. PostgreSQL would
reject that schema, so provider ID columns must be text-compatible.

## Execution path

```text
Discord /rival
    |
    v
RivalDiscordClient
    |
    +--> shared RelationalHotStore
           |
           +--> fixtures/current odds/line history
           +--> jurisdiction state
           +--> refresh runtime state
           +--> market catalog
           +--> cold archive queue/batch state
    |
    v
Discloud PostgreSQL
```

Closed historical line states can still flow from PostgreSQL through the existing verified Parquet
and Google Drive archive path. Drive remains cold storage, not the operational database.

## Changes on branch

- Added a shared relational database adapter with SQLite and PostgreSQL implementations.
- Added PostgreSQL hot-store support using psycopg.
- Made production storage default to `postgres`.
- Added `RIVAL_DATABASE_URL` and an explicit `RIVAL_STORAGE_BACKEND`.
- Made missing PostgreSQL configuration fail clearly instead of falling back to SQLite.
- Moved market catalog access onto the active relational store.
- Moved cold-archive queue bookkeeping onto the active relational store.
- Generalized collector, onboarding, and parlay-builder storage typing.
- Preserved SQLite compatibility for local tests and existing SQLite-focused regression tests.
- Added the PostgreSQL runtime dependency to both Python packaging and Discloud requirements.
- Enabled Discloud VLAN configuration for the bot.
- Added a PostgreSQL 16 service to CI.
- Added PostgreSQL integration coverage for SportsGameOdds IDs, current/history writes, runtime
  state, jurisdiction state, market catalog data, and archive completion ordering.

## Compatibility review

The public Discord surface remains `/rival`. Existing market-consensus rules, sportsbook
eligibility logic, refresh cadence, one-leg-per-fixture policy, Drive archive verification, and ZIP
privacy behavior are intentionally unchanged.

No automatic SQLite-to-PostgreSQL data copy is performed by runtime startup. Production must not
silently import or discard an unknown legacy database. If a real legacy database needs migration,
that is a controlled deployment operation using an inspected source file.

## Validation / results

Validated implementation head:
`897da83566f9684d1d52166e38f1c57e8a395d38`

GitHub Actions CI run #161 passed on the PR merge tree:

- PostgreSQL 16 service initialized and became healthy.
- Runtime dependencies, including psycopg, installed successfully.
- Ruff: passed.
- pytest: **65 passed, 1 dependency deprecation warning**.
- PostgreSQL integration tests cover SportsGameOdds string IDs, current/history writes, runtime
  state, jurisdiction state, market metadata, archive transaction ordering, and an end-to-end
  three-leg parlay build through PostgreSQL.
- Existing SQLite compatibility/regression tests still pass.
- Archive tests pass with the shared relational store.
- Discord client construction tests cover both production PostgreSQL requirements and explicit
  SQLite test mode.

Final diff inspection:

- branch is ahead of the intended production baseline and not behind it;
- no conflict markers;
- no private-key material or Discord-token-like values;
- no real database URL or credential committed;
- no generated database/archive artifacts;
- no unrelated project files;
- SQLite references in production code are restricted to the explicit local/test compatibility
  path.

## Cleanup / conflicts

The affected storage area is consolidated behind the relational store. The stale catalog filesystem
assumption and redundant PostgreSQL archive wrapper found during validation were removed.

The SQLite archive wrapper remains only for existing local/test compatibility. Production runtime
constructs one authoritative store and passes it to hot odds, market catalog, jurisdiction, refresh
state, parlay queries, and archive bookkeeping.

## Blockers / risks

- A real Discloud PostgreSQL instance and its private connection URL are deployment configuration,
  not repository data.
- Production activation cannot be validated against the user's actual Discloud database until that
  database exists and its URL is supplied through deployment environment configuration.
- No legacy SQLite data migration should be attempted without first confirming that the deployed
  SQLite file contains data worth preserving.

## Backlog

The requested self-learning crypto perpetual-futures conversion is recorded in GitHub issue #6.
Do not begin it until this cutover is verified or the owner explicitly force-switches tasks.

## October 27, 2026 post-merge production observations

- PR #5 was merged into `main` on October 6, and the merge-head GitHub CI passed.
- Discloud logs provided by the owner show that RivaL synced one Discord command to guild
  `1514374173517152418` and connected to Discord Gateway after the bot was installed.
- The owner displayed the private `/rival` dashboard with Connecticut selected.
  Code inspection confirms this is backed by `LocationOnboardingService` and the selected
  relational store; it proves a runtime write/read path, not restart persistence.
- Discloud backend selection, connection URL provenance, and restart persistence are not yet
  independently verified. A visible Discord connection alone is insufficient evidence.
- This focused follow-up adds a non-secret startup log recording the actual instantiated
  database backend after successful initialization. It does not alter storage, authentication,
  migrations, command semantics, or archive behavior.

## Next step

Once the startup-log PR is validated and deployed, inspect the Discloud log for
`RivaL operational storage initialized (backend=postgres)`.
Then restart the bot and invoke `/rival` again using the same Discord user to verify the
saved Connecticut location remains present without re-entering it. Record safe evidence only;
never copy database URLs, passwords, Discord tokens, or OAuth refresh tokens into logs.
The PostgreSQL cutover remains active until those production checks pass.
