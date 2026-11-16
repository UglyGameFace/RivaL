# RivaL active bot work — November 16, 2026 UTC

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
