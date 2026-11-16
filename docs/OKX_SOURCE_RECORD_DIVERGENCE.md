# Original OKX monthly candle divergence: verified comparison

**Evidence**: The authenticated original-source GitHub Actions run
[43895848526](https://github.com/UglyGameFace/RivaL/actions/runs/43895848526),
released after [#628](https://github.com/UglyGameFace/RivaL/pull/628),
produced `okx-bulk-source-progress` with four *real* conflicting month receipts.
The manifests and the first original history artifact passed SHA readback.

| Asset | Conflicting month | First integrity | Second integrity | Source original normalized hash comparison |
|---|---|---|---|---|
| BTC | 2026-07 | postgres-first-observed-at-v1 | okx-v5-public-history-5m-v1 | different **claimed** hashes |
| ETH | 2026-09 | postgres-first-observed-at-v1 | okx-v5-public-history-5m-v1 | different **claimed** hashes |
| SOL | 2026-09 | postgres-first-observed-at-v1 | okx-v5-public-history-5m-v1 | different **claimed** hashes |
| XRP | 2026-09 | postgres-first-observed-at-v1 | okx-v5-public-history-5m-v1 | different **claimed** hashes |

These are two distinct collection/provenance methods sharing one archive
`(venue,contract,quote,month)` key. Their manifest hashes, claimed normalized
history hashes and claimed provider-source hashes all differ. The original
receipt did **not** independently verify the second history bytes, so it
could not establish whether OHLCV differed or only observation chronology
and/or serialization. See issue #295 for the original facts.

## This PR

At the strict `source_native.restore_verified_source_months` conflict boundary:

1. Verify the conflicting **second history file** exists in the same
   authenticated Drive folder and expected immutable batch, has the exact
   expected name, bounded size and **actual bytes matching the second
   manifest's claimed SHA-256**. A missing or tampered second original fails
   closed. Do not copy or import it.
2. Generate bounded row-count and SHA-only signatures of normalized JSON
   records, retaining all fields in one comparison, and omitting
   `first_observed_at` only in a separate comparison. All timestamp, venue,
   contract, price, volume, trade and other columns remain in that
   observation-independent signature.
3. Add `comparison` to the existing conflict report and record **both**
   original normalized history byte verifications. Explicitly keep
   `second_provider_original_witness_verified=false`, since an official
   provider page or raw ZIP requires a separate source-witness proof.
4. Preserve the typed `HistoryImportError` failure, both originals,
   immutable hashes, no source selection and zero live model changes. The
   existing `okx-bulk-progress.json` and generic restoration exclusions
   already preserve typed conflict receipts.

**Interpretation**: `identical_except_first_observed_at=true` proves only
that normalized JSON candle records match when that one attribute is ignored;
it does **not** prove provider historical truth or authorize retrospective
availability. `false` means at least one other stored record field differs,
not necessarily just price. Neither result automatically chooses a winner.
Do not merge PG-recovered history and official 5m/API/ZIP history into one
unlabeled model stream. A model training pipeline must make an explicit
source-authenticated and chronologically causal policy decision later.

No actual OHLCV prices or user/Drive IDs are included in the diagnostic
artifact. Independent validations: exact-head CI plus original-source run
after merge, then inspect four `comparison` fields and authenticated source
families before implementing any source-family archive separation.

**Remaining**: the previous XMarket feature experiments had only **two**
distinct inner evaluation UTC days compared with required **seven**;
there are still 7 unimported XMarket fits and zero proven after-cost edges.
Do not retrain against a previously examined holdout or relax the gates.
