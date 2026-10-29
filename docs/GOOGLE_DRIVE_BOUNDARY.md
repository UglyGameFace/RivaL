# RivaL Google Drive Boundary

## Purpose

RivaL uses Google Drive only as its cold-data warehouse for historical sports data,
model artifacts, backups, manifests, reports, predictions, and staging files.

The authorized Google account is the user-designated connected Drive account.
The account email is intentionally **not committed** because this repository is public.

## Authorized root

RivaL may read from or write to **one and only one** top-level Drive folder:

`RivaL Data Warehouse`

No RivaL process may enumerate, read, move, rename, overwrite, delete, share, or otherwise
modify files outside that folder tree.

## Folder layout

```text
RivaL Data Warehouse/
├── history/
├── manifests/
├── models/
├── predictions/
├── backups/
├── reports/
└── staging/
```

### history/

Long-term compressed market history, line movement, props, results, and future training data.

V1 uses partition-encoded filenames directly inside `history/` to avoid dynamic folder discovery:

```text
sport=<sport>__date=<YYYY-MM-DD>__book=<book>__provider=<provider>__batch=<id>.parquet
```

A future nested partition layout may be added, but only by creating descendants from the configured
`history/` folder. RivaL must never discover those partitions by starting at My Drive/root.

### manifests/

Checksums, object manifests, archive indexes, schema versions, and integrity metadata.

Every archived file should have enough manifest metadata to answer:

- what it contains
- when it was produced
- which source/provider produced it
- which schema version was used
- row count
- byte size
- SHA-256 checksum
- archive path

### models/

Versioned trained-model artifacts and calibration data.

### predictions/

Time-stamped RivaL predictions required for reproducible backtesting and CLV analysis.

### backups/

Database snapshots and other recoverable operational state.

### reports/

Generated backtest, calibration, data-quality, and integrity reports.

### staging/

Temporary upload staging only. Files here are not authoritative and may be replaced after
successful checksum verification and promotion to a permanent folder.

## Hard safety rules

1. The running bot must receive the warehouse root folder ID through secret/environment
   configuration. It must not discover the root by browsing the user's Drive.
2. Every Drive read/write must target the configured root or one of its descendants.
3. Parent traversal outside the configured root is forbidden.
4. Recursive listing starts at the configured root, never at My Drive/root.
5. RivaL must never delete personal Drive content.
6. Automatic cleanup may operate only inside `staging/` and only on files RivaL itself created.
7. Permanent archives are append-oriented. Existing archive objects must not be silently replaced.
8. A replacement requires checksum verification plus an explicit archive-version decision.
9. Database backups must be encrypted before upload if they ever contain sensitive user records.
10. API keys, OAuth tokens, Discord tokens, refresh tokens, or other credentials must never be
    stored in Drive archives.
11. ZIP codes are not historical data and must never be archived.
12. The user's personal files are outside RivaL's data model and must never be indexed.

## Runtime configuration

The deployed bot will use environment variables similar to:

```text
RIVAL_DRIVE_ROOT_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_HISTORY_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_MANIFESTS_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_MODELS_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_PREDICTIONS_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_BACKUPS_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_REPORTS_FOLDER_ID=<private configured folder id>
RIVAL_DRIVE_STAGING_FOLDER_ID=<private configured folder id>
```

Actual IDs stay in deployment secret/config storage and are not committed to GitHub.

## Implemented archive invariants

- Only closed line states are eligible. The latest state of every selection remains hot.
- Data is written as explicit-schema ZSTD Parquet.
- Every data file has SHA-256 and MD5 digests plus a row count.
- Data uploads first to configured `staging/`, is checksum-verified, then is promoted to configured `history/`.
- A JSON manifest is written to configured `manifests/`.
- Source database rows are marked archived only after both Drive objects are verified.
- There is no archive delete API.

## Archive write sequence

```text
normalized rows
      ↓
local/staging parquet
      ↓
SHA-256 + row count + schema version
      ↓
upload to Drive staging/
      ↓
verify remote object metadata
      ↓
promote to permanent partition
      ↓
write manifest entry
      ↓
mark archive complete
```

If any verification step fails, the permanent archive is not updated.

## Restore sequence

Backtests and model jobs request only the partitions they need. RivaL must never download or
scan the user's entire Drive.

```text
requested sport/date/book
      ↓
manifest lookup
      ↓
specific archive object
      ↓
checksum validation
      ↓
local processing
```

## Repository boundary

This document describes storage policy only. RivaL application development belongs solely in
`UglyGameFace/RivaL`.


## Perpetual-futures candle warehouse (opt-in; October 28, 2026)

The sports-odds archiver above does **not** export the five-minute perpetual
`perp_market_candles` history. The dedicated
`PerpetualColdArchiveService` adds this separate dataset to the *same*
RivaL Drive history/ and manifests/ folders, not to a second Google account
or private user folder. Records are explicit-schema ZSTD Parquet with the
original `first_observed_at`, preventing backfilled candles from acquiring
fictional live history.

Data flow:

1. At most a configured number of **immutable, closed** candles older than
   the configured hot-history age are copied from active PostgreSQL.
2. The archive selects oldest unreceipted rows by *composite candle identity*
   and groups one venue/contract/interval/day partition at a time. This ensures
   a newly discovered older backfill is still eligible; a global high-water
   timestamp cursor would miss it.
3. Write a local Parquet object under RivaL's private staging directory. Record
   row count, schema, source identity and SHA-256/MD5 checksums in a manifest.
4. Upload Parquet through the existing bounded, resumable Google Drive staging
   flow, verify metadata, promote it to `history/`, upload and verify the
   manifest under `manifests/`.
5. **Only after both objects verify**, transactionally record each candle's
   archive receipt in the same authoritative PostgreSQL database. Retried
   batches are idempotent; incomplete operations retain local evidence.
6. Archive receipts do not modify price/provenance columns or reclassify old
   data as prospective forecasts. A failed Drive operation **never** marks
   a source candle archived.

### Important retention guard

This is deliberately **copy-only**. It **does not shrink the PostgreSQL
database yet**, because RivaL's prediction, backtesting, historical coverage,
and point-in-time research currently query the live candle table. Deleting rows
today could silently corrupt results. A future hot-data eviction must first
prove by downloaded Parquet SHA-256, manifest integrity, fully bounded
restore/query support, research lookback compatibility, tested rollback and
recovery from a lost host.

### Private Discloud setup (NOT committed to this public repository)

- Keep RivaL's verified primary database in Discloud PostgreSQL.
- Supply your RivaL-specific Google OAuth offline refresh token and existing
  `RIVAL_DRIVE_ROOT_FOLDER_ID`, `RIVAL_DRIVE_HISTORY_FOLDER_ID`,
  `RIVAL_DRIVE_MANIFESTS_FOLDER_ID`, `RIVAL_DRIVE_STAGING_FOLDER_ID`
  in the bot's private runtime environment, not GitHub.
- The ChatGPT Google Drive connector **does not** authenticate the Discloud
  bot; configuring these variables requires the owner's separate OAuth grant.
- `RIVAL_ARCHIVE_ENABLED=true` activates the preexisting closed sports-odds
  archive too. `RIVAL_PERP_ARCHIVE_ENABLED=true` opts into perpetual candles.
  Perpetual mode fails startup if the shared archive mode is not enabled.
- `RIVAL_PERP_ARCHIVE_KEEP_HOT_DAYS=14` (allowed 7..365),
  `RIVAL_PERP_ARCHIVE_BATCH_ROWS=1000` (1..5000), and
  `RIVAL_PERP_ARCHIVE_MAX_BATCHES=2` (1..4) bound network and database load
  per hourly archive cycle. Pause with `RIVAL_PERP_ARCHIVE_ENABLED=false`.
- Before production enablement, check that OAuth can access **only the
  authorized warehouse**, and perform a disposable sample staging upload,
  verify the manifest, and download/decode a copy to prove restore integrity.
  Never copy user ZIP codes, Discord IDs, access tokens or secret trading data
  into the public candle dataset.
- A Google Drive 5 TB storage plan does not grant unlimited API operations or
  guarantee future retention; monitor bytes, upload failures, free space and
  owner storage policy separately.

**Connected read-only audit on October 28:** the RivaL Data Warehouse root and
all seven intended subfolders exist in the designated Drive account, but
`history/`, `manifests/`, and `backups/` held zero direct files.
That proves there is not yet an independently recoverable production archive.
No production OAuth configuration, archive activation, or live RivaL database
connection was performed as part of this repository update.
