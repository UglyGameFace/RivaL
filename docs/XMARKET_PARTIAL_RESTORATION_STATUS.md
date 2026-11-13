# XMarket verified partial restoration — November 13, 2026

## Objective and repository state

Restore usable partial candle partitions from the existing private daily
ledger originals, prove exact market/candle timestamp overlap, and retain
the established chronological training, validation, calibration, test,
publication, import and forward-promotion gates.

Base: main `97ef3c6f303809f242351acd61ddc1bdb39da964` (merged PR #567).
Branch: `fix/xmarket-verified-partial-restoration-20261113`.
No open PR covered this outcome when checked. Independent uncommitted work
in other checkouts was preserved.

## Observed production evidence and root cause

[Run 43477093655](https://github.com/UglyGameFace/RivaL/actions/runs/43477093655)
completed successfully. Its downloaded BTC reports still have **zero**
eligible XMarket rows at 5/15/60m. Bitget/OKX/Gate feature clocks stop at
November 1 00:00 UTC; verified prediction-market bars begin November 8
06:50 UTC. Binance reaches November 12, but it cannot substitute for the
missing native sources in the pinned dispersion features. A green workflow
does not establish a fitted model.

`NativeMonthlyPublisher` deliberately exports only complete months. The
training restore invoked those monthly bridges, omitting the existing
`RecordsBackup` daily candle originals. Those daily files are partitioned
by **first observation day**, not candle-open day; recovery must respect
both clocks rather than infer observation times from candle dates.

The November 13 workflow uses train-before September 18 and test-from
October 16 (28-day validation and test windows, one-horizon purge).
Restoring November alone cannot supply earlier market observations.

## Implemented behavior

- Extend `TrainingWarehouse.records`, retaining its existing callers, with
  bounded date-scoped SHA-256/MD5/size-verified original readback, retained
  file identities, duplicate detection and complete bounded pagination.
- Adapt those originals into partial native partitions for the existing
  Bitget/OKX/Gate streams, including matching BTC references for altcoins.
  Retain original compressed bytes; re-parse and verify their exact row
  derivation again on load. Reject conflicting records, malformed prices,
  off-grid clocks, impossible observation times and changed hashes.
- Preserve missing days, leading/internal/trailing gaps and actual
  observation timestamps. Mark every such partition as partial and as
  ledger-derived, with no provider-original hash or forward-forecast claim.
  Verified complete monthly sources take precedence. Growing partial
  snapshots may supersede unchanged subsets without deleting older files
  or duplicating training rows; conflicting revisions still fail.
- Reuse `learning.assemble` and `load_frames_manifests`; retain separate
  provider, manifest, normalized and daily-original hashes. Never relabel
  a ledger digest as a provider hash.
- Record exact source/candle intersections and complete-row split dates,
  distinct timestamps, days, counts and shortfalls in XMarket reports.
  Require the existing 300 train / 100 validation / 100 test row floors.
  Artifacts using partial sources also need this eligibility proof at import.
- Integrate recovery into the existing archive workflow, preserve its
  serialization/schedules and 28/28-day windows, and upload the restore
  report with the ordinary run reports. Never upload private originals to
  public Actions artifacts. No new collector, live DB writes or Drive
  source writes are introduced.

## Validation and integration checkpoint

Local targeted validation: 118 passed, 2 PostgreSQL-dependent tests skipped
at the implementation checkpoint; repository Ruff and diff checks passed.
The final source/warehouse/workflow subset passed 49 tests (1 PostgreSQL
case skipped), and the XMarket admission/fit subset passed 66 tests (1 skip).
A local full-suite run reached 2,705 passing tests and 156 skips, then hit
the SQLite query-plan assertion in `test_stream_shadow_observe_index`.
That same assertion fails on an isolated, unchanged main checkout in this
environment: SQLite selects `idx_perp_forward_score_recent` rather than
the test's required index name. No database/index code is changed here.
The remaining tests passed separately: 238 passed, 18 skipped. Full
PostgreSQL CI remains required; use the linked PR's exact head checks for
final results. Hosted source execution remains a separate evidence requirement.

Regression coverage includes actual daily-original -> manifest -> loader
-> weekly trainer -> report publication, all horizons, exact joins,
tampering even after normalized hashes are recomputed, conflicting source
rows, absent days, BTC-reference lineage, full-month precedence, idempotence,
growing snapshots, old artifact compatibility and partial-source import refusal.
The key insufficient-history case has real November overlap but zero train
and validation rows: only blocked reports are published, never a model.

## Remaining production evidence and next step

Connected Drive searches did not locate the daily originals. That is not
proof of their absence from the workflow's authenticated warehouse.
Run the integrated workflow with its existing private credentials; inspect
`restore-report.json`, source receipts, measured gaps, `overlap_evidence`,
`row_admission` and `split_rows`. Missing or oversized sources remain explicit
exclusions, without weakening source or model gates.

Only a subsequent eligible fit can establish model publication and import;
actual prospective shadow issuance and independently verified after-cost
superiority remain later evidence. Issues #6, #138 and #299 remain open.
Retire this branch only after validated integration; preserve unrelated work.
