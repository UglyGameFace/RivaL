# Active Task Record

## Project

RivaL

## Repository boundary

RivaL development belongs only in `UglyGameFace/RivaL`.

Do not write RivaL code, workflows, task records, branches, or pull requests to
`UglyGameFace/sports-parlay-bot`.

## Completed milestones

- Provider-neutral odds foundation and quota-aware OddsPapi history access.
- SportsGameOdds shared current/today feed with quota reserve and persistent refresh gating.
- Canonical odds normalization and SQLite hot storage.
- ZIP/state onboarding with state-only persistence.
- Verified sportsbook filtering.
- Private Discord `/rival` dashboard.
- Market-consensus parlay engine V1.
- RivaL-only Google Drive warehouse folder tree and hard access boundary.

SportsGameOdds current-feed PR #2 merged as
`47864350e98be72e3b84fffb27e4010785ead908`.
The validated feature tree and squash-merge tree were identical.

## Google Drive boundary

The authorized connected Google account contains one dedicated root:

`RivaL Data Warehouse/`

with:

- `history/`
- `manifests/`
- `models/`
- `predictions/`
- `backups/`
- `reports/`
- `staging/`

Real account details and real folder IDs are private runtime configuration and are not committed.
RivaL may operate only inside this root. See `docs/GOOGLE_DRIVE_BOUNDARY.md`.

## Active task / outcome

Build the first durable Google Drive cold-archive pipeline for closed line-movement states.

Branch: `feat/google-drive-cold-archive`

### Implemented on branch

- Optional archive dependency using DuckDB; it is not part of the normal hot Discord dependency path.
- CI installs the archive extra so the cold path is regression tested.
- SQLite archive queue that selects only **closed** line states.
- The latest state for every market selection is never archive-eligible.
- Closed states must also be older than a caller-supplied cutoff.
- Deterministic batch IDs derived from provider/book/sport/date/source row IDs.
- Archive batch lifecycle table.
- Archive markers on line-history rows.
- Chunked row completion updates to avoid SQLite variable-count limits.
- Explicit-schema Parquet writer.
- ZSTD Parquet compression.
- SHA-256 and MD5 local digests.
- JSON manifest containing schema version, row count, source-row hash, time bounds, data checksum, and Drive history-file ID.
- Google OAuth refresh-token client using the documented token endpoint.
- Resumable Google Drive upload.
- Upload chunk size constrained to Google Drive's 256 KiB multiple requirement.
- No generic Drive-root listing method.
- No delete method.
- Bounded Drive searches only inside configured RivaL `staging`, `history`, or `manifests` folders.
- History files upload to RivaL `staging` first.
- Uploaded size and MD5 must match the local artifact.
- Verified staged history is moved only from RivaL `staging` to RivaL `history`.
- Manifest uploads only to RivaL `manifests`.
- SQLite rows are marked archived only after both verified Drive objects exist.
- Deterministic batch properties allow safe restart/retry without intentionally duplicating an archive.
- Local temporary artifacts are deleted only after the batch reaches complete state.
- Archive OAuth client ID/secret/refresh token are deployment secrets, never Git values.

## Archive commit sequence

```text
closed SQLite line states
        |
        v
explicit-schema Parquet + ZSTD
        |
        v
SHA-256 / MD5 / row count
        |
        v
Drive staging/
        |
        v
remote size + MD5 verification
        |
        v
promote to Drive history/
        |
        v
write + upload manifest
        |
        v
mark SQLite source rows archived
        |
        v
delete local temporary files
```

A failure before the manifest is verified leaves the SQLite source rows unarchived.

## Safety rules

- The current/latest state of a selection is never archived.
- RivaL has no archive API that enumerates My Drive/root.
- RivaL has no cold-archive delete operation.
- Uploads can target only configured RivaL staging/manifests folders.
- History promotion can move only a file already found/uploaded in configured RivaL staging.
- Drive object size and MD5 must match before commit.
- Secrets, ZIP codes, personal Drive content, and user message content are not archive inputs.
- Real Drive folder IDs and the account email are not committed.

## Validation status

Latest implementation head `2b39d14a2d304afd8388f174415678862a6c0746` passed GitHub Actions push CI.

The previous validated head reported:

- Ruff passed.
- pytest: **55 passed, 1 warning**.
- Parquet artifacts were read back through DuckDB in tests.
- Drive resumable upload/promotion was exercised with a mocked Google endpoint.
- Archive completion ordering was regression tested.

Final documentation head and PR merge-ref CI still need validation before merge.

## Deployment blocker

The archive code is ready, but automatic production uploads remain intentionally disabled until
the deployed RivaL runtime receives its own Google OAuth client ID, client secret, and offline
refresh token in secret storage. ChatGPT's connected Drive authorization is not reused by the bot.

## Next step

Open draft PR #3, require green exact-head PR CI, scan for secrets/private Drive identifiers,
then squash-merge if clean. Runtime scheduling can be added after the production OAuth secret
path is configured.
