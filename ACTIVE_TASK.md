# RivaL active bot work — November 16, 2026 UTC

## Real OKX original-source month conflict repair

**Branch:** `fix/xmarket-identical-okx-native-snapshots-20261116`, based on merged owner/trial #626 (`fe663906`), separate from Jarvis #608. November 16 verified original source run [43878923264](https://github.com/UglyGameFace/RivaL/actions/runs/43878923264) excluded all OKX monthly restore paths as `conflicting native month snapshots`, leaving XMarket inner-research evaluation at 2 rather than the required 7 distinct UTC days. More weight updates alone cannot meet a future profitability gate.

**Actual root cause:** `source_native.restore_verified_source_months` rejected all repeated `(venue,contract,quote,month)` manifests even if their SHA-verified original manifest bytes could be identical. Repair safely accepts exact manifest-SHA replay only after first original manifest/history (including witnesses) is verified, and rejects any changed manifest or corrupted replay. This **does not assume the real OKX conflicts are identical**, invent source coverage, lower minimum-days gate, delete historical evidence, touch model weights or restart training.

**Validation:** Run exact PR/full SQLite/PostgreSQL CI, merge and main CI if validated, then an authenticated original Drive read-only restore to determine whether real OKX SHA differs. Genuine conflicting source data still needs publisher/provenance investigation, plus at least 7 eligible training-only evaluation days and prospective future after-cost proof. See `docs/XMARKET_OKX_SNAPSHOT_REPLAY.md`.

## Owner API access and live trial-screen repair

**Branch:** `fix/rival-owner-api-and-model-trial-truth-20261116`, based on current `main@ed596182`; the only independent open PR on initial review was Jarvis #608. Screenshots show the account that can use RivaL's private menu is denied by More → API Usage & Limits, while model performance shows 173 aggregate trials / 189 cut records / zero passed.

**Verified causes:** `GuidedHome.act("api-limits")` rechecked `application_info().owner` rather than the existing `OwnerGate` authorized IDs/team, denying valid configured/team owners. `trial_board.read` reported global comparisons without applying `rival_family_early_stop`, treated fully stopped models as active, and folded model generations by maximum days even if the older generation was stopped. The 189 stop count is stop *records*, not necessarily globally unique models. Live Model Lab review already has 30-minute replacement, two-day strong-loss thresholds and 21-day no-edge minimum; there is no evidence that removing all old models produces profit.

**Implementation:** Reuse the existing private OwnerGate and fail closed; scope API confirmation controls to the actual authorized caller. Apply source stop evidence to the global and local display counts and prioritize active/qualified generations under shared labels; do not rewrite existing forecasts or model promotion gates. Regression tests plus `docs/OWNER_API_AND_MODEL_TRIAL_AUDIT.md`.

**Validation required:** Exact-head CI, merged-main CI, production source SHA, Android interaction with the configured owner account, and fresh stop-aware active comparison counts. Honest remaining blockers: no qualified profitable XMarket scope, native source gaps, new locked future evaluations, source-matched paper quote proof. Keep #6/#299/#295/#151 open. No deployment or cleanup of legacy files without authenticated evidence.

## Current correction: XMarket research-only source-selection causality

**Main checkpoint:** #624 merged at `b957d2e0`. Its GitHub one-shot train-only workflow has been installed; no provider-backed completion evidence is established in this task record. The earlier PR #623 had 3,566 passing SQLite/PostgreSQL tests but no claimed qualified edge.

**Identified root cause:** Shared `_select_dispersion_venues` chose optional cross-venue inputs using the source availability of outer validation/test folds even in `research_only=True`. This did not expose their outcome labels or change live models, but it leaked future *availability* into exploratory training feature selection. A previously uploaded #624 run artifact must not be used to pick a challenger without this correction.

**Branch:** `fix/xmarket-research-train-only-venue-selection-20261116`, starting from main. **Repair:** confine research selector market/venue timestamps to the horizon-purged original training period and require enough inner-fit/independent-time evaluation capacity. Keep the serving/production all-splits selector unchanged. Tests demonstrate future availability changes cannot alter the training-only selected feature contract, while production still requires validation/test coverage. Docs identify original #624 results as diagnostics only. The workflow permits exactly one explicit owner-directed, bounded,
research-only rerun on the `b957d2e0` main transition, serialized with
any original research job. Never restart weekly training or issue
unbounded/repeated multi-asset jobs.

**Acceptance:** exact-head Ruff and full SQLite/PostgreSQL CI, reconciled merge/main check, then explicitly authorized fresh one-shot report under corrected SHA, verify all twelve research scopes and native-source gap counts. Continue source-authentic issued forecasts and after-cost gates separately. Do not close #6/#299/#295/#138 on code CI.

## Following merged #623: source-verified research run

**Merged baseline:** #623 is on main at `c158cd4a` (full PR SQLite/PostgreSQL CI: 3,566 passed). This branch `research/xmarket-train-only-one-shot-20261116` adds an owner-directed, one-time GitHub Actions read-only research run for all four assets and 5/15/60m, without model publication. Existing weekly archive push gates remain in effect; do not revive unattended weekly training.

**Evidence semantics:** Original provider-SHA-verified candle manifests and native PM/Kalshi input bundles are read-only. No provider backfill, Drive source writes, database mutation, model import, live serving or trading. The inner comparison uses only the original training split, purges by the target horizon, and never scores or selects against outer validation/test for research. GitHub artifact summarizes sanitized source status, time splits, profile Brier and blockers. See `docs/XMARKET_READONLY_RESEARCH_RUN.md`.

**Required checks:** Exact-head lint/full PostgreSQL CI, merge, main validation, then verify that the one-time GitHub Actions jobs actually executed and returned four genuine artifacts. If the workflow does not fire because the expected base SHA changed, retain explicit manual-only instructions rather than changing the chronology. No qualified after-cost XMarket scope yet. Keep #6/#299/#295/#151 open and confirm source-exact Discloud forward evidence separately.

## Current objective: XMarket rejected-fit quality research (November 16, 2026)

**Branch:** `research/xmarket-purged-feature-ablation-20261116`. Start from verified `main@fe88d8c` after merged advance-call #622; parallel open Jarvis PR #608 is unrelated. Do not replay the November 14 holdout, promote an unqualified model, or stop the five issuing XMarket shadows.

**Verified root causes:** Actual original-source run [43647037069](https://github.com/UglyGameFace/RivaL/actions/runs/43647037069) had seven genuine offline quality rejections (four 5m, ETH 15/60m, SOL 60m), not missing training code. Each exact gate failure is recorded in `docs/XMARKET_REJECTED_FIT_RESEARCH.md`. The existing five fitted scopes remain independently subject to future source-matched and after-cost gates.

**Implementation:** For rejected trained scopes, append four predeclared logistic feature-ablation comparisons to the existing research report using only the training partition with a second horizon purge. Fold statistics count distinct target timestamps rather than multiplying correlated venue rows. No new model is automatically selected or imported, no new source/worker, no threshold relaxation and no production inference change. Added focused unit tests and the existing train-run rejection integration assertion.

**Verification required:** Exact-PR full PostgreSQL/SQLite CI, merged-main CI if authorized to merge, one actual new Drive training report with `train_only_feature_ablation` (not a historical re-export), and future locked-period validation. **A measured inner Brier gain is not independently validated predictive improvement.** Next candidate development must pre-register the winner against a new untouched future period; keep #6/#151/#299/#295 open. The existing five XMarket scopes and fresh advance-target Discord delivery need separate production acceptance.

## Advance next-target calls — active implementation

**Objective/branch:** `feat/advance-next-target-calls-20261116`, based on
`bf568fac` (including #619/#621), [PR #622](https://github.com/UglyGameFace/RivaL/pull/622).
Implement real pre-close next-target issuance and observable
channel delivery. Website PRs #620/#621 and private LFM3/Jarvis #608 are
independent; no conflicting forecasting PR was open at the initial live check.

**Root cause:** #619 reads existing predictions; it cannot create an advance
5m call from a model trained for the current horizon. Optional spike/research
stages also preceded any lead head. A single zero-issuance cycle was incorrectly
generalized earlier; the same receipt contained real incumbent calls and 4,084
XMarket shadows in 24h.

**Changes:** Reuse causal features and persistent learning with a distinct
next-candle-own-open target contract for 5/15/60m selections, plus five minutes
of lead. Priority issuance after the shared collector and before optional
spikes; bounded guarded thread; immutable native forecast ledger, separate
commit deadline proof/readiness/outcomes and accepted channel delivery.
The card shows actual next raw probability/model/source/timestamps, retains
the prior advance, and reports pending reasons. Polymarket missing next strike
is explicit; Kalshi remains native. See `docs/ADVANCE_FORECASTS.md`.

**Validation at publication:** 21 advance/guard SQLite cases passed, including
20 matching PostgreSQL fixtures for CI; focused existing regressions passed.
Initial PostgreSQL CI caught new `REAL` columns rounding Brier/probabilities
to float32. They now follow existing `DOUBLE PRECISION` conventions without
weakening the assertion. Repeated live schema setup was also removed and
covered by a regression. Full exact-PR/main CI remains required; see #622 for
the latest acceptance. Existing unrelated SOCKS
proxy support was installed in the local test environment after an HTTP client
test could not construct its transport; no production dependency was changed.

**Acceptance/blockers/next:** Publish one cohesive PR after full regressions,
validate exact CI, integrate and inspect native Discloud source plus fresh
read-only commit/delivery receipts. Actual consecutive pre-close channel
delivery and future model skill are not yet proved. Preserve old ledgers,
models, qualification and cost gates; issues #6/#151/#299 remain open. Retire
only this branch after its full tree is preserved on main.

## Active user-selected Live Calls and XMarket model-quality continuation (2026-11-16)

**Branch/PR:** `feat/channel-live-research-calls-20261116`, [PR #619](https://github.com/UglyGameFace/RivaL/pull/619), based on main `a35d7b1`. This extends the merged #613–616 exact-source quote infrastructure. Existing parallel Evidence Workers #618 and private Jarvis/LFM3 #608 remain independent and untouched.

**User requirement:** Show model UP/DOWN (or exact-contract YES/NO) and numeric confidence per requested market even when not qualified. Fast updates to current and *next genuinely issued* target/contract on the **same channel card**, not unsolicited DMs. Show provenance, expiry, raw-vs-calibrated nature, and “NO TRADE CALL” for unqualified models. Do not synthesize entry/SL/TP1/TP2 or profitable P&L.

**Implemented:** `discord_app/call_board.py` is one persistent, owner-controlled, opt-in message per market/asset(+Polymarket horizon). Exchange uses existing `target_snapshot` and `recent_watches` for all 5m/15m/60m horizons, actual model/venue/confidence and next previously-issued target; market-specific adapters reuse original Polymarket Gamma/CLOB frozen current/next contract `_observation` and independent Kalshi listed-contract `contract_preview`. Unsupported DraftKings declines rather than borrowing another market. `GuidedHome` selects market/asset/horizon; `/rival-stop` revokes cards. The dedicated `_call_board_loop` wakes after real `autonomous.cycle()` issuance and also checks the immutable exchange issuance ledger every 15 seconds; fallback full redraw 60s and 5-minute UTC expiry transitions; Polymarket 30s and Kalshi 45s native budget/caches. All market reads produce no real orders.

**Tests:** `tests/test_live_call_board.py` covers displayed raw/calibrated scores, current and future-issued accuracy, Polymarket/Kalshi source identity, channel-only posting, owner-controlled Stop All, market/horizon separation, fast issuance refresh and expiry boundary. The first CI failed at lint BLE001 and was repaired in the same PR; later exact-PR CI must pass full SQLite/PostgreSQL tests before merge. After merge verify main CI, native Discloud release parity and a real channel card on host. Card delivery speed is a scheduling target contingent on actual data/provider latency, **not yet production verified**.

**XMarket quality still unresolved:** Only five model fits imported, seven declined chronological/calibration/holdout gates, zero independently qualified profitable edges after costs. Binance book intake is healthy but its XMarket issuance gap is separate. Keep issues #6/#151/#299 open until independent >=150 targets, >=21 days, >=80% quote coverage and calibrated after-cost superiority prove an edge. Evidence Board #618 work is separate.


## Current continuation (2026-11-16 UTC) — bounded source-exact quote sampling

Branch `fix/fair-bounded-quote-observer-20261116` extends merged #613–615 without touching separate Jarvis PR #608. `main@46f4f9b` passed [full CI](https://github.com/UglyGameFace/RivaL/actions/runs/43792111472) and [native release parity](https://github.com/UglyGameFace/RivaL/actions/runs/43792807328). [02:34 UTC production receipt](https://github.com/UglyGameFace/RivaL/actions/runs/43792927860) contains genuine source-exact evidence: seven live native book sources fresh on BTC/ETH/SOL/XRP; Bitget 34/34, OKX 30/30, Hyperliquid 36/39, Gate 7/23, Deribit 6/19, Lighter 10/29 matched XMarket directional-entry rows in a rolling two-hour window. Binance books are fresh but its XMarket forecast count remains zero. No source has proven profitable XMarket edge.

The last receipt overall failed because the latest fresh learning heartbeat recorded `math_ok=false` and 0 issued/settled immediately after boot. Do not call learning healthy until later source evidence confirms recovery. The observed misses on newer feeds may include rows issued before feed activation. Root code risk: `_stream_shadow_loop` always sampled 7 venues x 4 assets serially with the three new venues last; occasional slow PostgreSQL operations can exhaust the unchanged 90-second quote-entry window.

This branch keeps native clocks, costs, exact-source ledgers, no-order policy and qualification gates intact, but interleaves/rotates venue sampling, limits simultaneous database writers to three (eight-slot shared pool), and adds read-only 30-minute per-source decisive/entry/expired cohort counts to distinguish pre-deploy misses. Regression tests cover concurrency, tail-source fairness, rotation, and original PostgreSQL/SQLite quote identity/read-only proof.

Acceptance: exact PR and merged-main full CI; native Discloud release parity; a post-deployment authenticated quote receipt with healthy source intake, real new-cohort entries/exits and `math_ok=true`; no increased pool/lock pressure. Still missing seven XMarket fits and empirical independent 150-target/21-day/80%-coverage, calibrated baseline and after-cost superiority.


## Objective
Continue all eleven areas in [RIVAL_REMAINING_BACKLOG.md](docs/RIVAL_REMAINING_BACKLOG.md); website repair remains paused and independent Jarvis/LFM3 PR #608 stays separate. Current production outcome: extend the shared native quote pipeline to Gate, Deribit linear-USDC and Lighter, including exact-source entry/exit, fee, review scheduling, signal access and host evidence. No orders, inferred quotes, retrospective forecasts or relaxed qualification gates.

## Verified prior outcomes
- #610 recovered current close-target scoring while older source backlog drains. The actual PostgreSQL receipt at 00:37 UTC records 2,240 recent head side settlements. These correlated rows are not independent targets, a cleared backlog or profitable path calibration.
- #611 is merged at a8a4e897ada2430b85e6d481a332f7be7eb2e249. Exact PR/main CI each passed 3,446 tests; executed native source parity, live panels and authenticated PostgreSQL checks succeeded. At 00:37 UTC, both OKX and Bitget had their first completed SOL 15m XMarket quote pair with zero missed exits; additional genuine entries were observed.
- #612 is merged at 8f2a65eb012d7aae9a79327caf03f6d2cc12353d. PR/main CI each passed 3,447 tests. The reproduced watchdog/Discord clock error is corrected at observation completion, while genuinely stale/future books remain rejected. Native source release 43782844709 and authenticated receipt 43782924288 succeeded. At 00:52:39 UTC the new boot reports all four assets fresh on OKX, Bitget and Hyperliquid, with zero reconnects on the new feeds and 2,880 recent close-target head side settlements. Binance still has no books. This verifies the corrected observation behavior on the host; it does not establish prediction quality.
- The previous task record is preserved byte-for-byte in docs/archive/ACTIVE_TASK_OKX_BITGET_CLOCK_20261116.md. Broader issues #6/#151/#299/#138/#347 remain open; none of these milestones establishes qualified XMarket profits.

## Current implementation and evidence
- Branch feat/gate-deribit-lighter-quote-proof-20261116 is based on #612, with main/PR overlap rechecked before publication. Independent evidence-board hosting documentation is preserved.
- Actual public metadata verifies all four assets on Gate direct-USDT contracts, Deribit linear-USDC perpetuals and Lighter active perpetual IDs. Gate contract counts require coin multipliers BTC .0001, ETH .01, SOL 1, XRP 10. Deribit linear amounts and Lighter BBO sizes are already coins; inverse/spot instruments are rejected. Lighter's documented public read-only endpoint supplies genuine microsecond committer timestamps and complete BBO snapshots; no stateful delta book or authentication is needed.
- Three bounded sockets reuse the shared identity, incumbent/family/fusion ledgers, signal reads and durable watchdog snapshots. Raw ticks remain in RAM. Missing native stream quotes fail closed in the UI; no invented REST fallback or another venue's book is used. The watchdog's existing cached coverage query now covers registered source/currency pairs.
- Gate public contract metadata lists .00075 taker fee per leg: add 7 round-trip bps to existing scenarios (21/87 bps). Deribit .00035 and Lighter's public fees do not reduce the existing 8-bps fee floor or 14/80-bps total stresses. CASH fees remain zero.
- Review one deterministic source/asset/horizon per 20-minute boundary on the existing 5m driver. All 84 scopes rotate in 28 hours, inside the unchanged 48-hour freshness gate. No bulk startup review or new driver is added.
- The bounded actual running-hub probe observed fresh BTC/ETH/SOL/XRP on all three new sources at 8 and 33 seconds, with zero reconnects across the heartbeat. Focused unit/integration tests cover real wire units/clocks, inverse/spot/malformed/stale/future rejection, exact entries/exits, restart, adverse costs, CASH, native signal fail-closed behavior, source coverage caching, public subscription lifecycle and complete proof scheduling. Ruff and the broader focused suite passed locally; full unchanged repository CI is required.

## Integration, cleanup and remaining work
- Require exact PR and main full CI, native source parity and authenticated durable source health. Then verify genuine eligible entries and same-source exits as targets mature; absent signals stay absent. Record dated production evidence in #151 and update the implementing PR. Retire only this work's merged branch after full-tree preservation checks.
- Current executor checks could not establish Phemex WebSocket upgrades or KuCoin's public subscription bootstrap; dYdX books lack the exchange timestamp required by this proof contract. Do not label these feeds supported or substitute local receipt time. Binance also remains connected-without-books in the observed host snapshot.
- Seven XMarket fit slots, sufficient independent future targets/days, baseline-relative calibrated improvement, stressed after-cost gains, history/input completeness, path calibration, native contract markets, unattended recovery/restore, UX and sports remain separate acceptance work. Preserve the full backlog and continue the next actionable gap after this outcome.

## Follow-up implementation: Binance public bookTicker source restoration

- #613 merged into main at `e5c8a74f49a63c90b0300ca6a5c83a454bb86dc1` after the exact PR passed 3,495 tests, critical regressions and Ruff. Main CI, native release and authenticated host proof remain separate acceptance; a Discloud commit status alone does not prove running book health.
- The new feature branch `fix/binance-public-bookticker-route-20261116` repairs a directly verified exchange-contract mismatch: since Binance's 2026 WebSocket category split, `@bookTicker` belongs under `/public/stream`; the quote hub was connecting to `/market/stream`. Two local URL constructions have been consolidated so default and selected-asset sockets cannot diverge. The existing separate `@aggTrade` stream keeps its valid `/market` path.
- The integration regression simulates a live URL-subscribed socket with both COIN-M `st=2` (ignored) and USD-M `st=1` (accepted) books, checks source/asset identity and bounded cancellation. No REST polling, new worker, historical backfill, inferred quotes, orders or relaxed gates.
- After exact-PR/full-main CI, require a fresh Discloud release, authenticated source-health counters and actual Binance BTC/ETH/SOL/XRP books with original exchange clocks; then verify eligible source-exact issuance/entry/exit observations. If a region blocks Binance, report that honestly and retain no-quote/no-call, rather than treating a successful handshake as evidence. Keep XMarket quality/backlog issues open until genuine independent after-cost proof is established.

## Next evidence milestone — actionable quote funnel (2026-11-16)

- #613 merged and the first hosted post-release receipt proved fresh Gate/Deribit/Lighter quote books across BTC, ETH, SOL and XRP, but new venue matched XMarket entries were absent at that sample; source support and forecast rows alone do not explain why.
- #614 merged at `e2f2770f6931693e149117ca6c89b9af193a06a7`, correcting Binance's 2026 `bookTicker` route. Exact PR CI passed 3,496 tests; exact main CI also succeeded. Release parity, actual native quote updates and matched Binance pairs still require independent host proof. The separate Jarvis/LFM3 PR #608 remains untouched.
- Branch `feat/xmarket-entry-opportunity-receipt-20261116` is based on #614's main. It adds non-mutating counts for directional, CASH, expired-unmatched and still-live unmatched forecast opportunities to the existing source-exact receipt. A SQLite/PostgreSQL regression checks genuinely observed entries and that a status read cannot backfill late quotes. Original source clock, 90-second entry window, 150 targets, 21 days, 80% coverage and cost gates are unchanged.
- Before merge, require exact PR full CI; after merge, main CI and authenticated host receipt. Determine from new values whether remaining zero source entries reflect neutral forecasts or elapsed unmatched opportunities; do not infer profitable performance. Keep #151/#299/#6 and the eleven-area backlog active. The evidence website remains separate.
