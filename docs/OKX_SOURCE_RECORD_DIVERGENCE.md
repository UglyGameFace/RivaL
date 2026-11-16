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

## Actual post-#629 source comparison (November 16, 2026)

Authenticated [main run #43916036555](https://github.com/UglyGameFace/RivaL/actions/runs/43916036555)
used merged SHA `ad1a0b13` and produced `okx-bulk-source-progress`.
All four original conflicts have **both** normalized history objects SHA-readback
verified, equal row counts, and `identical_except_first_observed_at=false`:

- BTC July 2026: 8,928 rows per version.
- ETH, SOL, and XRP September 2026: 8,640 rows per version.

The source families are PostgreSQL first-observed ledger exports versus the
independent OKX v5 public-history backfill. Code review independently shows
an important schema distinction: the PostgreSQL exporter writes `volume_base`
and `trades`, while the OKX public-history/ZIP exporters also write
`volume_contracts` and `volume_quote` and use `trades=-1` when unavailable.
These **known extra fields** can change the full-record signature even if
OHLC prices happen to be identical. This is not yet an observed claim that
those actual four-month price series are equal. Both manifest byte hashes and
normalized file byte hashes are verified, **not** independent exchange-published
raw evidence. The observations refute an observation-clock-only discrepancy
but do not prove prices specifically changed: volume, trade counts, candle
time, identity fields or numeric serialization could also explain it.

## Bounded field-level conflict receipt

The next diagnostic retains the strict import block and adds
`comparison.differing_fields` with only known, safe column names and boolean
flags for price, volume, trade count, and candle-grid/identity differences.
Other unknown record keys appear only as `other_record_field`. Per-field
streaming SHA signatures distinguish original records by indexed rows and
normalize numeric *spelling* (e.g. 101 and 101.0) for diagnostics only.
Full original SHA-256, full-record identity and observation-excluding identity
are **not** relaxed. No candles, times, Drive IDs, source URLs or per-row values
are included in the report.

Acceptance requires another authentic four-asset OKX run from the diagnostic
commit, followed by source-family and provider witness reconciliation based
on which fields actually differ. Even a semantically identical result does
not automatically select, merge, promote or backdate either original.

## Authenticated per-field proof from main #630 (November 16, 2026)

Post-merge [official OKX restoration run #43922686148](https://github.com/UglyGameFace/RivaL/actions/runs/43922686148)
at `main@533299ad` verified both conflicting normalized archives' SHA
readbacks for four assets; still admitted **zero** monthly histories and
published **zero** models. The exact new public field categories:

| Asset | Month | Different non-observation fields | Price/grid/trade differences |
| --- | --- | --- | --- |
| BTC | 2026-07 | `volume_base`, `volume_contracts`, `volume_quote` | none |
| ETH | 2026-10 | `volume_base`, `volume_contracts`, `volume_quote` | none |
| SOL | 2026-09 | `volume_contracts`, `volume_quote` | none |
| XRP | 2026-09 | `volume_base`, `volume_contracts`, `volume_quote` | none |

Each also differs in `first_observed_at`. The earlier ETH September
example was a stale collision frontier; the **fresh exact-head** run reports
October. These field equality statements concern the verified normalized
records, **not a provider-signed original-byte comparison**. OHLC and all
candle identity/time grid values match; important: *volume still affects live
model features*. The original two provenance families must never be blended
into one model simply because prices match.

## Correcting base-volume ingestion and separating original families

OKX V5 public candlestick responses have
`[ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]`;
for linear swaps `vol` is **contracts** and `volCcy` is directly **base
currency**. Official API spec: https://www.okx.com/docs-v5/en/

The prior live `OkxMarketClient` and PostgreSQL historical
`OkxHistoryRecovery` inferred base volume as
`vol * ctVal * ctMult`, introducing a second representation. Going forward,
both instead parse and validate the exchange's own `volCcy` (index 6).
They continue validating exact linear instrument metadata, identity,
closed candles, and contract/quote-volume types. **No pre-existing database
candle, Drive original, first-observed clock, or settled target is rewritten**.

The Drive restore's default behavior is unchanged: any two divergent
month manifests for the same scope are BLOCKED. For official archive
**inventory only**, `source_family="okx_official"` excludes RivaL's
PostgreSQL-derived first-observed snapshots, but still fails closed on
different API-versus-ZIP originals for the same month. For retrospective
model research, `source_integrity` pins **one exact, whitelisted OKX
provenance** into a separate local research directory; the unattended
source-native workflow explicitly pins
`okx-v5-public-history-5m-v1`. It never silently chooses a family or mixes
Postgres and OKX volumes. Missing full months still yield no candidate.
The importer screen, chronological holdout boundaries, prospective shadow
qualification, matched quotes, and after-cost promotion gates remain intact.

An OKX public-history SHA of a response-derived stream is **not** an
independent provider-signed raw witness. Use original, authenticated ZIP
readback or equivalent exchange-origin evidence when enforcing stronger
source authenticity. Retrospective training alone is not live model approval.
