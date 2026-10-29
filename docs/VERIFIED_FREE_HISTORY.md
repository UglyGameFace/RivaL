# RivaL Verified Free Historical Data Intake

## Purpose and scope

This research-only ingestion is separate from `perp_market_candles`,
`perp_forward_audit`, current sportsbook prices, and every live trading surface.
It cannot retroactively improve the bot's live accuracy, create live forecasts,
settle old forecasts, authorize trades, or promote a model. It requires no API
key and never queries account or order APIs.

**Current implementation:** official Binance USD-M futures monthly *kline*
archives; free football-data.co.uk league-season settled match results and
opening/closing prices where supplied; retrospective bookmaker no-vig
baseline. This does not yet import NFL/NBA player performance, tick-level
order books, funding history, or independent authenticated fills.

### Authoritative sources

- Binance public archive: https://github.com/binance/binance-public-data
  and https://data.binance.vision/. USD-M futures path is
  `data/futures/um/monthly/klines/<SYMBOL>/<INTERVAL>/<SYMBOL>-<INTERVAL>-YYYY-MM.zip`.
  Every download requires the sibling SHA-256 `.CHECKSUM` file. CSV candle
  timestamps use UTC **milliseconds**, not live observation times. Do not
  reinterpret the spot archive's post-2025 microsecond timestamps as futures.
- Soccer results/odds: https://www.football-data.co.uk/downloadm.php;
  `mmz4281/<SEASON>/<LEAGUE>.csv`. The publisher's distribution rights
  are *not* assumed. Do not commit raw files to this public repository.
  Opening/closing prices are distinguished, but the exact historical
  publication time is not proven. Historical closing prices cannot be a
  feature in a pre-close prediction.

## Run on the historical-research host (not the Discord event loop)

First list the 100 planned requests (6 completed calendar months x three
assets x five timeframes + 2 completed football seasons x five leagues):

```bash
python -m parlay_bot.research.batch --months 6 --plan
```

Collect a bounded set of up to twelve archives per invocation, saving
validated NDJSON and immutable JSON manifests on a persistent volume:

```bash
python -m parlay_bot.research.batch \
  --out data/research-history \
  --months 6 --max-new 12
```

Repeat the same command (or explicitly schedule it on a storage-backed,
network-enabled worker) until `remaining=0` and `rejected=0`. Each run
verifies the digests of already saved data, skips intact partitions, and
reports provider errors. Bounded attempts continue past unavailable
partitions without silently inventing data. A partial/corrupt output is
never treated as a completed partition. An official archive revision
requires explicit revalidation; skipping an already verified partition is
not a promise that a provider never changed its historical CSV.

For one month's BTC five-minute linear futures archive:

```bash
python -m parlay_bot.research.free_history --out data/research-history \
  binance --asset BTC --interval 5m --month 2026-09
```

For the Premier League's completed 2025/26 season:

```bash
python -m parlay_bot.research.free_history --out data/research-history \
  football --league E0 --season 2526
```

For the resulting bookmaker baseline:

```bash
python -m parlay_bot.research.benchmarks \
  data/research-history/football_data/E0-2526--<digest>.manifest.json \
  --book B365
```

The benchmark computes three-outcome no-vig opening probabilities,
overround, multi-class Brier and log loss; closing Brier is separately
descriptive. Its later 30% slice is *not* a trained or verified live model.
A historical sample with at least 100 later-period fixtures still doesn't
prove forecast tradability. It does not issue picks.

## Validation contract

- No credentials, player identities, user ZIP codes or personal Drive data.
- Symbol, league, season and interval values are allowlisted; redirect and
  arbitrary URL requests are disallowed.
- SHA-256 of every Binance ZIP must match the official named checksum.
  ZIP must contain exactly one expected CSV, limited to 64 MiB uncompressed.
  Download size, row count, and numeric/time validity are bounded.
- Misaligned, duplicate or descending candles reject the *whole* partition;
  missing intervals are counted, not fabricated. Require exact closed,
  completed historical months.
- Settled soccer match result must match the supplied scoreline. Duplicate
  fixtures, non-finite odds, impossible probabilities or unpaired bookmaker
  triplets cannot influence benchmarks.
- Each immutable NDJSON object has a content digest and JSON manifest.
  Repeat imports with identical data are idempotent, source revisions retain
  distinct digests. Tests exercise fake HTTP responses (no real API calls).
- Missing/corrupted manifests force re-download; the batch process may be
  safely restarted after failure.

## Warehouse and deployment boundary

The user-designated private `RivaL Data Warehouse/` Drive root is the
only permitted destination. Keep permanent imported research under
`history/`, source digests/manifests under `manifests/`, and temporary
objects under `staging/`. The intake CLI writes only local research files, never PostgreSQL. An
**explicit, separate** archive command now uses the existing RivaL Drive
client to verify source file digests and promote history files through
`staging/` to `history/`, then upload the manifest to `manifests/`:

```bash
# Offline preview, no Drive write:
python -m parlay_bot.research.publish \
  data/research-history/football_data/E0-2526--<digest>.manifest.json

# Requires the independent bot OAuth refresh token and all four verified
# RIVAL_DRIVE_*_FOLDER_ID environment variables on the authorized host:
python -m parlay_bot.research.publish \
  data/research-history/football_data/E0-2526--<digest>.manifest.json \
  --publish
```

The archive client checks that configured history, manifests and staging
folders are *direct descendants of the configured RivaL warehouse root*;
it verifies Google Drive object size and MD5 before declaring upload success.
Do not assume a ChatGPT Drive connection authenticates the Discloud bot.
A real downloaded restore drill has not yet been performed. Production
credentials and permissions must be validated independently; never store
them in this public repository.

Do not enable on-demand bulk downloads in Discord handlers. For a scheduled
worker, cap invocations and monitor network egress, provider blocks,
validation failures and local disk consumption. Keep the existing live
5-minute collector and read-only autonomous shadow forecasting independent.

**Remaining work before treating this as a production learning input:**
source-to-warehouse verified promotion/restore, training feature
standardization by symbol+venue+contract+timeframe, genuine as-of
information availability, chronological holds and forward evaluation,
and measured after-cost performance. Historical data alone has zero
prospective prediction accuracy.

## End-to-end six-month warehouse and learning pipeline (PR #49)

One command covers provider acquisition, calendar-exact continuity, research
training and private Google Drive round-trip byte validation:

```bash
# No network, writes or credentials:
python -m parlay_bot.research.production --months 6 --plan

# Download all requested BTC/ETH/SOL archives, preserve actual source hashes,
# check all six full UTC months per stream, train research candidates and
# generate deterministic evidence reports. No live prediction changes:
python -m parlay_bot.research.production --months 6 --max-new 100 \
  --seasons \
  --root data/research-history --report-dir data/research-reports

# Same full pipeline plus real private Google Drive publication and SHA256
# download verification. Requires separate bot-owned OAuth credentials:
python -m parlay_bot.research.production --months 6 --max-new 100 \
  --publish --root data/research-history --report-dir data/research-reports
```

Production publication now additionally needs
`RIVAL_DRIVE_MODELS_FOLDER_ID` and `RIVAL_DRIVE_REPORTS_FOLDER_ID` beside
the four original root/history/manifests/staging IDs and three private OAuth
values. No secrets belong in GitHub commits, published reports, Discord
screens, or ChatGPT messages. With incomplete requested source coverage the
production command exits nonzero and withholds all-done status.

RivaL's real public-provider GitHub workflow is
`.github/workflows/verified-history.yml`. PR runs validate one real Binance
hourly month and attempt Football-Data, plus a six-month public crypto history
job covering BTC, ETH and SOL in `1m/5m/15m/1h/4h/1d`. This workflow stores
raw provider-derived records in short-retention Actions artifacts only for
the purpose of verified owner archival; it does not publish them into git.
On main it can run the full pipeline and securely upload to the private Drive
only when the bot-owned OAuth values have actually been configured.

Historical candidates use immutable versions tied to source content hashes.
For each asset and compatible horizon (5 minutes through seven days), all
features precede the associated future label. The 60%/20%/20% chronological
train/validation/test splits purge label overlap across boundaries, use a
training-only base-rate comparator and report accuracy alongside Brier,
log-loss, calibration error and daily score stability. Nonoverlapping target
windows avoid multiplying overlapping multi-hour decisions.

**Important:** This is an *offline historical* test. The bar-open clock is
not a documented record of when RivaL knew the price. No retrospective score
can be relabeled as an actual contemporaneous 46%-accuracy improvement, real
fill profitability, a licensed sportsbook quote or a live deployable model.
A future independent prospective shadow ledger and account-specific
execution-cost receipts are separate required gates. The existing automatic
prospective RivaL loop continues on its current controls, with no auto orders.

### Verified external evidence

- On October 29, 2026, a GitHub Actions runner independently fetched the
  official Binance `BTCUSDT-1h-2026-09.zip`, checked its provided SHA-256,
  validated **720 hourly candles with zero gaps**, and recorded the source and
  normalized data digests. This is **one month of one stream**, not six months.
- An authorized connected Drive transfer placed the verified normalized data
  and JSON source manifest in the existing private RivaL `history/` and
  `manifests/` folders. An independent download from Google Drive was
  compared byte-for-byte; recovered research data SHA-256:
  `183e0c42e1acab44d233b765f1166f52e6e8bea87ca12ed86f96e0dd612f6819`.
  This proves the **connected owner Drive** archive round-trip, not the
  deployed Discloud bot's separate OAuth access.
- Football-Data's official CSV endpoint was unavailable from the GitHub
  runner in the first source smoke. Other histories, prospective prediction
  gains and actual betting profitability must not be marked verified.


### Real six-calendar-month production evidence (October 29, 2026)

The PR #49 GitHub Actions workflow #40806781762 **successfully fetched
and checksum-validated every requested Binance USD-M month** from April to
September 2026. Coverage: BTCUSDT, ETHUSDT and SOLUSDT, 1m, 5m, 15m,
1h, 4h and 1d, for 108 independent monthly partitions with 1,018,395
genuine observations and **zero UTC time-grid gaps**. Download attempts
rejected: zero. A 26,673,248-byte ZIP holds the 108 NDJSON partitions plus
108 per-partition source checksum manifests. ZIP SHA-256:
`dafb560b27ab0816d3220e6833dc6c49a2b7976a62aa57d6c7725c1bc5826a2b`.

Through the authorized user-connected Drive interface, the full normalized
archive ZIP was stored under RivaL `history/`, its index under
`manifests/`, the complete holdout report under `reports/`, and a
versioned offline candidate registry under `models/`. All four objects
were downloaded back from Drive and verified byte-for-byte, including
the archive ZIP. The archive is recoverable by unzipping it and verifying
the per-file SHA-256 fields. It is a bundled cold copy; it must not be
mistaken for a verified production bot read-through deployment or a live
PostgreSQL import.

A first 42-model offline research screen covered compatible short-horizon
combinations; 36 longer-horizon trials were withheld for insufficient
non-overlapping, leak-free samples. **Zero candidates passed the strict
Brier improvement gates.** Any raw hit-rate gains are not proven
improvements over an independent probability baseline and did not cause
model promotion. The preexisting live 46% accuracy is not redefined.

The official football odds source was inaccessible from the Actions
runner. A live Discloud OAuth grant, direct provider archive read-through,
verified future prospective model performance and actual net betting
profitability remain **NOT VERIFIED**. No automatic trading.

### Controlled refresh cadence and resource budgets

A successful initial full-archive PR proof already exists. To avoid
redundant 1,018,395-row downloads on every pull-request edit,
`full-six-month-public-crypto` runs again only when the explicit GitHub
repository variable `RIVAL_FULL_ARCHIVE_PR_PROBE=true` is set.

Monthly refresh is scheduled for the **8th at 07:15 UTC**, after the
official previous-month release window. It performs no expensive work unless
`RIVAL_HISTORY_PRODUCTION_ENABLED=true` is configured in GitHub repo
variables. The separate repository Actions secrets must contain the
bot-owned `RIVAL_DRIVE_OAUTH_*` values and root/history/manifests/staging/
models/reports folder IDs. An Actions upload using the connected ChatGPT
Drive account is evidence of a different authorization path, not proof
that this unattended OAuth secret path works. Missing credentials fail
closed into an explicit non-published report. Verify a full unattended
run plus restored objects before treating automation as activated.

No scheduled job can promote a research candidate to the live forecasting
model or authorize wagering. The existing prospective shadow metrics remain
independent of imported offline research.
