# Active Task Record

## Project
RivaL

## Repository
`UglyGameFace/RivaL`

## Recovery note
The initial RivaL implementation was mistakenly written to `UglyGameFace/sports-parlay-bot`.
The validated project is being migrated into this repository, and the wrong repository will be restored to its original pre-RivaL contents.

## Completed foundation
- Quota-aware OddsPapi v4 provider layer.
- Account entitlement parsing.
- Current fixture and odds access.
- Historical odds with ETag support.
- Canonical odds normalization.
- SQLite hot storage.
- Meaningful-state line history and duplicate-state compaction.
- Stale provider-state protection.
- ZIP/state onboarding with ZIP discarded after state resolution.
- State-only jurisdiction persistence.
- Verified sportsbook filtering.
- Private Discord `/rival` onboarding/dashboard.
- Python 3.11 Ruff + pytest CI.

## Hard repository boundary
Continue RivaL development only in `UglyGameFace/RivaL`.
Do not write RivaL code, workflows, task records, branches, or pull requests to `UglyGameFace/sports-parlay-bot`.

## Privacy/security
- Never commit API keys, bot tokens, OAuth credentials, databases, or raw odds dumps.
- ZIP codes are transient and are not persisted.
- Store sportsbook odds once per real market selection, not once per state.
- Saved jurisdiction filters displayed books; it is not proof of physical wagering eligibility.

## Next milestone
Build the first real parlay-construction layer: no-vig probability, eligible-book price comparison, candidate-leg scoring, correlation safeguards, and a Discord-facing Build Parlay flow.

Google Drive archival remains blocked until the connected account that owns the 5 TB storage is positively identified.
