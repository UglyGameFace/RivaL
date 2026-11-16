# RivaL API owner and stale-model screen audit — 2026-11-16

## Incident: authorized owner denied API budgets

**Observed:** Discord `/rival → More → API Usage & Limits` said
`Only the application owner can edit API limits.` even from an account
permitted to open and interact with RivaL's private menu.

**Actual cause:** `discord_app.home.GuidedHome.act("api-limits")` called
`application_info()` again and compared only `application.owner.id`
against the tapper. However the authoritative global command/view/modal
access boundary is `discord_app.owner_gate.OwnerGate`: it allows explicit
`RIVAL_OWNER_USER_IDS` or the Discord application team members, not
necessarily `application.owner`. The duplicate check excluded legitimate
private users, and introduced an unnecessary Discord API request.

**Repair:** acknowledge before checking; consult the already-installed
`interaction.client.owner_gate.allows(interaction)` and fail closed if
unavailable; scope the actual `OwnerLimitsView` plus confirmation modal to
the authorized tapper's Discord ID. Do not grant a Discord guild owner or
administrator API budget control merely for holding that server role.
`OwnerLimitStore` audit, provider-specific quota enforcement, and
required confirmation remain unchanged.

**Production verification needed:** ensure the logged-in Discord account
is one of the authorized owner IDs (for a configured deployment, check
`RIVAL_OWNER_USER_IDS` without logging or posting private environment
values), deploy and source-verify the merged commit, open More → API Usage
& Limits, test read-only review and an explicit confirm/cancel of a safe
setting, and ensure an unauthorized account is refused. CI alone cannot
prove Discord's deployment loaded the fix.

## Incident: a growing table of “bad models on trial”

**Observed:** the screenshot showed 173 `on trial`, zero passed, 189
`cut early`, multiple losing models with 17/21 days, and a 6-year
candidate ahead on Brier with 13/21 days. These are not proof of profit.

**Verified code paths:**

1. `discord_app.trial_board.read` counts global models from the latest
   promotion evidence using `fold(entries, [])` but **omits** the
   `rival_family_early_stop` records from that aggregation. That can
   count fully stopped families as `on trial` even though they no longer
   issue shadow forecasts. Correct the count to apply the actual stops;
   show their record count separately, not as model successes.
2. On individual market screens, `read` included fully stopped rows in
   `Models on trial`. Now hide stopped models from that active view while
   retaining their immutable ledger history and a separate cut count.
3. `trial_board.fold` merges model generations under shared display names
   such as `6-year`, preferring the one with the most days even when it is
   fully stopped. Prefer a qualified or still-active generation over a
   stopped older one before comparing days/results. This is a **display
   selection**, not a change to live model weights or promotion.
4. `model_lab.ModelLab.review` already closes designs on decisive losses
   and refills on a 30-minute schedule; it uses scored targets, UTC days,
   uncertainty, source-specific early stops, and 21-day no-edge checks.
   Most losing models at 13–17 evaluated days are not automatically closed
   simply because their current average gain is negative. This is
   intentional to avoid selecting models from short noisy samples. Its
   original stop and governance gates stay intact.
5. Legacy `regime_v2` training is disabled; earlier reverted `*_venue`
   variants are already excluded in `trial_board.is_retired`. Other
   legacy model filenames do not, by themselves, prove those models are
   still issuing or harming live predictions. Do not delete historical
   ledgers or remove code just because it looks old. Source-verified
   imports, issue switches, per-stream issuers, and current host receipts
   must establish which route is really active before retirement.

**Why no profits yet:** On the screenshots, `Edge` denotes lower Brier
error *versus the main model*, not dollars or net profit. `Right` is
directional correctness over recent scored calls, not a source-matched
tradable fill. The positive 6-year numbers still require at least 21 UTC
days plus genuine forward target coverage, 80% matched entry/exit quotes,
calibration and positive after-cost stressed evidence on that exact
exchange/asset/horizon. Model Lab and XMarket are distinct families and
proof streams; simply adding offline model fits, feature ablations and
data collectors cannot guarantee a profitable successor.

## Verified XMarket training-research failure behind the apparent progress

The one-shot XMarket runs
[#43871582320](https://github.com/UglyGameFace/RivaL/actions/runs/43871582320)
and corrected [#43878923264](https://github.com/UglyGameFace/RivaL/actions/runs/43878923264)
**did execute** and failed all four asset jobs. This is more precise than
the earlier claim that no verified run evidence was available.

I inspected the GitHub-produced per-asset, read-only JSON evidence artifacts
for the corrected run. In all 12 combinations, source verification flags
for Polymarket and Kalshi were `verified`, and all outer overlap
admission checks were true. However the mandatory *inner training-only*
feature-evaluation fold contained only **two distinct UTC days** per scope,
versus the predeclared seven-day floor. Independent evaluation target counts
were 302–358, exceeding the 100-target floor; the problem was **days**.
The four assets' oldest verified native market coverage was too sparse in
the Sep-2026 training partition to fit these causal feature experiments.

Original-source restore also reported six Bitget monthly archive months
per asset; OKX was excluded for
`HistoryImportError: conflicting native month snapshots`, including BTC
references for non-BTC coins. The original source-verifier appropriately
failed closed instead of picking one conflicting document. The missing
matched-market training calendar, not the number of restored Bitget months,
determines the usable feature-fold duration.

**Necessary next work:** source-exact chronological recovery across at
least seven distinct eligible training-evaluation days, plus enough
earlier purged fit data for each asset and horizon; identify each conflicting
OKX monthly snapshot's original provider hashes and decide only with
authenticated source evidence, not by guess or silent overwrite. Then run
a separately authorized fresh research pass. Only after it produces scores
can an XMarket challenger be pre-registered for a *new untouched future*
evaluation period. Do not lower the seven-day floor, fabricate history,
mine the old validation/test periods, or claim that any XMarket scope is
profitable. The existing five imported XMarket 15/60m shadows remain separate
from the non-XMarket `6-year` trial in the screenshot.

## Acceptance and remaining blockers

- Unit/integration tests reproduce permission divergence, non-owner
  denial, stop-aware trial counts, and pooled-version preference.
- Run exact-PR Ruff, SQLite/PostgreSQL and applicable critical regression
  CI, then verify merged-main CI and actual Discloud source SHA.
- Android Discord acceptance: authorized menu opens API review, other
  accounts fail closed; latest Bot Performance uses stopped-aware active
  trial counts, retains honest negative active candidates, and separates
  aggregate Brier from tradable after-fee proof.
- The separate XMarket one-shot original-source research, seven rejected
  scopes, unverified after-cost edge, native source gaps and future
  evaluation requirements remain open. Do not mark RivaL profitable or
  autonomous on a green test suite.
