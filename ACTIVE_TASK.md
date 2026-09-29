# Active Task Record

## Project

RivaL

## Repository boundary

RivaL development belongs only in `UglyGameFace/RivaL`.

Do not write RivaL code, workflows, task records, branches, or pull requests to
`UglyGameFace/sports-parlay-bot`.

## Completed foundation

- Quota-aware OddsPapi v4 provider layer.
- Account entitlement parsing.
- Current fixture and odds access.
- Historical odds with ETag support.
- Canonical current-odds normalization.
- SQLite hot storage.
- Meaningful-state line history and duplicate-state compaction.
- Stale provider-state protection.
- ZIP/state onboarding with ZIP discarded after state resolution.
- State-only jurisdiction persistence.
- Verified sportsbook filtering.
- Private Discord `/rival` onboarding/dashboard.
- Python 3.11 Ruff + pytest CI.
- RivaL-only Google Drive cold-warehouse folder tree.

## Google Drive boundary

The user-designated connected Google account is now known and the following dedicated root exists:

`RivaL Data Warehouse/`

Children:

- `history/`
- `manifests/`
- `models/`
- `predictions/`
- `backups/`
- `reports/`
- `staging/`

The account email and real folder IDs are intentionally not committed because this repository is public.
Runtime Drive operations must be confined to that configured root and its descendants.
See `docs/GOOGLE_DRIVE_BOUNDARY.md`.

## Active task / outcome

Build the first real RivaL parlay-construction layer on top of cached current odds.

Branch: `feat/parlay-engine-v1`

### Implemented on branch

- Decimal implied-probability math.
- Within-book no-vig probability normalization.
- Multi-book median market-consensus probability.
- Stable market/outcome label catalog model.
- OddsPapi `/v4/markets` adapter with quota protection and 1-second endpoint cooldown.
- Persistent market/outcome label catalog.
- Same-book parlay construction so one returned slip is executable at one sportsbook.
- Jurisdiction-aware book filtering.
- Risk modes: Lower, Balanced, Aggressive, Longshot.
- Candidate scoring from market-consensus probability, offered price, and price edge.
- One-leg-per-fixture correlation guard.
- Active/main-line filtering.
- 30-minute current-price freshness guard.
- Started-fixture rejection.
- Discord `Build Parlay` action.
- Discord `Make Safer` and `Boost Payout` actions.
- Explicit UI disclosure that V1 probabilities are market-consensus estimates, not a trained prediction model or guaranteed outcome.
- Drive environment configuration placeholders with no real IDs committed.

## V1 modeling boundary

RivaL V1 does **not** claim to have a trained predictive edge yet.

Current fair probabilities are derived by:

1. Remove each sportsbook market's vig by normalizing implied probabilities across all outcomes.
2. Compare the same logical outcome across eligible books.
3. De-duplicate identical probability vectors so cloned feeds do not receive extra voting weight.
4. Use the median no-vig probability as the market-consensus estimate.
5. Compare that fair estimate with the price offered by each actionable sportsbook.

A trained player/team model, calibrated probabilities, injury/lineup features, CLV learning, and
correlation model remain later milestones.

## Safety / data-integrity constraints

- A returned parlay uses one sportsbook only.
- Current prices older than the configured freshness window are ignored.
- Already-started fixtures are ignored.
- Inactive and non-main-line selections are ignored.
- Same-game/multi-leg fixture combinations are blocked until correlation is modeled.
- The UI does not use words such as lock, guaranteed winner, or guaranteed profit.
- ZIP codes are never persisted or archived.
- No API keys, Discord tokens, OAuth tokens, real Drive folder IDs, databases, or raw odds payloads are committed.

## Validation status

Implementation written. Exact-head GitHub Actions CI and final diff/security review are required before merge.

## Next step

Open a draft PR, run exact-head CI, repair any lint/test failures, inspect the complete diff for
secrets and unrelated changes, then squash-merge only if green.
