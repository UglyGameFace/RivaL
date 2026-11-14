# XMarket native-source recovery

The source-only recovery job repairs independent Polymarket/Kalshi history
twins for observed live contracts and advances the existing older-history
cursor. It publishes SHA-verified originals and checkpoints through the existing
private Drive pipeline. It never fits a model or writes a forecast.

## Allocation and bounds

A completed daily export can add more than 32 native contracts per provider.
The previous fixed 32-contract overlap allocation could leave the source
comparison below its 80% coverage gate after a whole recovery pass. The rest
of the capacity was spent on older history even while missing live/history
comparisons prevented all XMarket row admission.

Recovery now reallocates that same capacity. For a requested older-window
budget `W`, each provider retains its existing `W + 32` contract-attempt ceiling.
Up to 96 missing comparison contracts can be selected per provider, with
capacity reserved for at least `min(W, 24)` older windows. The actual shared
older-window allocation uses the larger of the two providers' selected overlap
batches, preserving the paired cursor and the ceiling for both providers.

When both providers have a large comparison backlog:

| Requested older windows | Maximum overlap contracts per provider | Reserved older windows | Total attempt ceiling, including 12 gap rechecks |
| ---: | ---: | ---: | ---: |
| 24 | 32 | 24 | 124 |
| 48 | 56 | 24 | 172 |
| 96 | 96 | 32 | 268 |

With little or no overlap backlog, the original requested older-window budget
is retained. The CLI still supports its existing 0–8640 window range; zero
retains the bounded overlap-only mode. Budget counts are **contract attempts**,
not individual HTTP requests: one contract may need a market lookup or the
provider's historical tier. Provider pacing, timeout and internal retry limits
remain unchanged.

## Failure and evidence rules

- A rate limit or transport failure pauses that provider for the remainder of
  the pass. Other phases cannot immediately request the same unavailable
  provider again; the paired cursor does not advance over its failed window.
- A successful, exactly bound contract obtained earlier in the same pass can
  satisfy its own gap/cursor window. Its original rows and publication are
  retained once. Unavailable or unverified contracts are never reused as successes.
- Only committed, verified Drive checkpoints advance the next pass. Staged or
  failed publications cannot acknowledge progress.
- The workflow summary includes the planned allocation, actual attempts,
  paused providers, comparison coverage and outstanding comparison bars. It
  excludes raw prices, credentials, contract identifiers and Drive IDs.

The 80% native comparison coverage requirement, minimum comparison count,
price tolerance, mismatch limit, original-source provenance and chronological
300/100/100 row floors are unchanged. Successful source recovery alone does
not establish training eligibility, a fitted/imported artifact, prospective
shadow forecasts or a profitable edge. Missing older native timestamps remain
an explicit blocker even after comparison coverage passes.
