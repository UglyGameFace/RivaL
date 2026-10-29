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
objects under `staging/`. This CLI **currently writes only local
research files**, not Google Drive or PostgreSQL. Files do not become
durable Drive archives until a separately verified upload/restore path
promotes both data and manifests into the authorized warehouse folders.
Do not assume a ChatGPT Drive connection authenticates the Discloud bot.

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
