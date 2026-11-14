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
is retained until both source-fidelity checks pass. The CLI still supports its
existing 0–8640 window range; zero
retains the bounded overlap-only mode. Budget counts are **contract attempts**,
not individual HTTP requests: one contract may need a market lookup or the
provider's historical tier. Provider pacing, timeout and internal retry limits
remain unchanged.

## Required chronological history

The production trainer reserves 28 days for validation and another 28 for
test. Restoring only the newest native contracts cannot populate its training
period. Read-only native API probes on November 14 recovered exact September
18 contracts for BTC, ETH, SOL and XRP from both providers, and an October 16
BTC contract. Those probes prepared local originals only; they did not publish
inputs, fit weights or establish complete historical coverage.

Once **both existing source-fidelity gates pass**, recovery uses the same
contract-attempt ceiling to traverse the missing chronological ranges directly.
It keeps at least 24 continuous older windows and assigns the remaining
capacity to the training and validation ranges in a 2:1 ratio. At the default
96-window setting this means 24 continuous, 70 training and 34 validation
windows per provider, plus the existing total 12 gap rechecks: at most 268
contract attempts. Optional extra comparison repairs are deferred while the
actual comparison gates remain verified; a later unverified pass restores
comparison priority. No schedule, provider pacing or retry ceiling changes.

The first committed checkpoint fixes an acquisition calendar, using the
trainer's 28-day boundaries and the original 90-day native retention window.
For a November 14 start, training traverses September 19 backwards toward
August 16, and validation traverses October 17 backwards toward September 19.
Each range has its own persistent cursor. Advancing one never jumps the
continuous archive cursor across unrequested contracts. Later days resume the
original positions rather than fetching the same newest contracts again;
the moving retention floor still applies. Finishing a range records traversal,
not completeness: unavailable native contracts remain explicit pending gaps.

Only complete, exactly bound native feature grids already in verified archived
inputs can satisfy a visited window without another network request. Partial
or conflicting grids cannot supply invented bars. Priority cursors are saved
only in the final checkpoint, after all original publications and input chunks
are acknowledged. Uncommitted local files and uploaded data chunks cannot
advance them. Small manual budgets below 24 preserve their previous behavior.

The trainer also checks chronological input availability when pinning a
dispersion venue subset. A recent-only third venue must not erase older
training timestamps genuinely shared by two other venues. It prefers the
largest material subset with observed pooled capacity in every required split,
using timestamps and feature availability, never labels or model scores. If
every subset lacks sufficient history, it retains the largest material subset
and reports the original split shortfall. The actual 300/100/100 admission
floors and model quality gates remain authoritative.

After a substantive collector merge, the existing source workflow runs the
read-only original-archive admission diagnostic after that asset's new
checkpoint publishes. It independently restores verified monthly archives and
daily candle originals, reads the published native bundles and measures all
three horizons with the actual trainer's admission-only path. Only the concise
verdict is uploaded to Actions; no original source rows or fitted model are
uploaded. Regular six-hour recovery passes retain their existing workload.

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

## November 14, 2026: independently proven first historical folds

The post-merge source run [43641543527](https://github.com/UglyGameFace/RivaL/actions/runs/43641543527)
verified original archived source windows, both native fidelity checks and
the existing 300/100/100 row floors on BTC and ETH at 5m, 15m and 60m.
Both assets selected Bitget and OKX; neither fitted a model in this
admission-only run. The reports are scoped to the fixed original clock
`trained_through=2026-11-13T23:55Z`, train cutoff September 19 and
test start October 17. All four assets must prove the same conditions
before the one-time training push is authorized.

XRP's first pass failed during Google Drive resumable **session initiation**
(HTTP 502), before the final checkpoint, source summary or readback.
The Drive client previously recovered interrupted PUT chunks but did not
retry a transient initiation error. Its initiation retry is now separately
bounded, never sends source bytes before a session exists, and does not
retry 400/401/403. Existing immutable batch reuse and checkpoint ordering
remain authoritative.

The one-off fit guard is pinned to the exact completed successful source
run, four matching asset receipts and original split clock. Only a single
`main` push transitioning from #585's commit may attempt it; failed or
incomplete evidence must block the fit. Ordinary pushes do not train.
Neither source admission, a passing CI workflow nor a passing offline model
gate proves prospective after-cost performance. Weekly/manual schedules and
promotion policies remain unchanged.
