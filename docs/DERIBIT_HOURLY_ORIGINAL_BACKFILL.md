# Deribit 2021+ source-native hourly trade archival

Deribit's official historical API (https://history.deribit.com) is free and
supports BTC/ETH instrument-specific trades back to 2021. The prior
2021-01-01 one-minute BTC/ETH source proof succeeded and saved unchanged
source bytes with SHA-256 Drive readback.

The hourly collector adds bounded source-original chunks:

- One completed UTC hour per asset, at most 48 total HTTP requests per asset.
- Query Deribit with include_old=true, ascending and count=10,000.
- If has_more=true, bisect the exact time range instead of silently dropping
  trades. Every returned chunk must have matching instrument, unique IDs,
  chronological times and finite positive trade price and size.
- Keep original JSON bytes and immutable per-source SHA manifests in private
  Google Drive and verify actual downloaded bytes, not just Drive metadata.
- Unavailable/empty/overflow/budget-exhausted windows remain explicitly
  **incomplete**. A verified hour never means a full calendar month.
- Separate inverse/perpetual contract identities. SOL/XRP are not invented
  Deribit 2021 native instruments.
- Rate pacing is conservative. Failure isolation, provider 429 stop and
  no live prediction/model promotion remain mandatory.

The first merged run is a small proof for 2021-01-02 hour 00. A future
backfill scheduler can use the same chunk verifier; it must account for
date/contract launch, response volume and remote Drive quota. This does
not promise all historical trades across 2021-present are archived.
