# XMarket frame admission — November 13, 2026

## Objective and current source

Repair the production `no frames to train on` blocker in the authoritative
weekly archive pipeline. Base: main `1aee3eace37c4be3a4123dd458b6a204bbeefe4c`.
Branch: `fix/xmarket-native-frame-admission-20261113`.
No open PR overlapped this repair when inspected. Existing uncommitted work
in another checkout was preserved; its already-merged history-recovery work
was not duplicated here.

## Evidence and root cause

[Training run 43464957081](https://github.com/UglyGameFace/RivaL/actions/runs/43464957081)
contains BTC, SOL and XRP reports with verified Polymarket and Kalshi fidelity,
100% measured comparison overlap, empty `venue_frame_coverage` and
`no frames to train on` at all three horizons. ETH was still executing when
these artifacts were examined. Successful workflow execution is not a fit.

`archive_training.load_frames_manifests` and the Binance loader supply
`venue|contract|quote` keys. `xmarket_training.train_run` accepted only keys
ending in `|ASSET`, dropping every actual archive stream. Native venue
closes at boundary minus 1ms also failed the exact prediction-market join;
CSV fixtures hid both differences. A regression reproduced rejection before
the repair and produces equivalent fitted weights after the repair.

## Changes and validation

The trainer now selects native streams by asset and normalizes only the
documented 1ms close convention, preserving exact stream keys and shared
source frames. The live venue set still pins dispersion. Invalid clocks,
ambiguous source aliases and missing inputs fail closed. Empty eligible
market bars retain a UTC clock in the diagnostic instead of throwing.
Reports measure source dates and per-stream admission/split counts, including
zero-row failures. Provider, calibration, holdout and promotion gates are
unchanged.

Regression coverage includes all four assets and 5/15/60-minute horizons,
fitted-weight equivalence, exact causal joins/cutoffs, invalid clocks, source
identity, disjoint dates, and manifest loader → weekly trainer → report
publication with insufficient splits and no model publication.
Local validation: 98 targeted tests passed, 13 PostgreSQL-dependent tests
skipped; repository Ruff and `git diff --check` passed. Full PostgreSQL CI
and post-merge source execution remain pending at this checkpoint.

The existing date-locked main-only November 13 recovery trigger also watches
the repaired trainer so integration can be checked without asking the owner
to dispatch another run. Its expiry, normal weekly schedule, shared run
serialization and publication gates are retained.

## Remaining production proof and next step

Obtain green CI on the exact repair commit, merge, verify main contains it,
and inspect the subsequent real training report. The base run's verified
Bitget/OKX/Gate archive ends October 31, while native history recovery was
still working through November. Normalization does not invent overlap:
inspect `source_feature_coverage`, `row_admission`, `split_rows` and the
existing independent recovery cursor before deciding the next data repair.

A passing fit, verified Drive publication/import and actual immutable shadow
rows remain separate milestones. No model superiority, fitted import,
forward edge or production recovery is established by synthetic tests.

Related issues #6, #138 and #299 remain open: they request broader prospective
performance, market-engine validation and model improvements that this repair
does not establish. Retire this repair branch only after integration and
verification; do not delete independent or unpreserved work.
