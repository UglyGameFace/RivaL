# Official funding history collectors

Hyperliquid and KuCoin BTC/ETH/SOL/XRP historical perpetual funding now have
separate **public source-native original-page** acquisition lanes.

- Hyperliquid: official `POST /info`, `type=fundingHistory`, per coin.
- KuCoin Futures: official `GET /api/v1/contract/funding-rates` with exact
  per-contract symbol (BTC maps to XBTUSDTM).
- Both use disjoint four-day UTC time windows, limited page size, byte ceilings,
  exact contract/timestamp/rate validation, and original JSON plus manifest
  SHA-256 readback from the private Drive warehouse.
- Every UTC month is merely an **attempt window**, not proof a contract existed
  or the API retains data that far back. HTTP 200, a source page, and a Drive
  manifest never certify a full month or feed the live forecast model.
- The existing central 640-scope coordinator counts these as eight newly
  *routed* scopes, not eight completed scopes. Census files index them only
  as partial source pages. Failed/throttled requests remain visible.
- Scheduled job runs once daily on both sources with independent concurrency
  groups; existing bulk/download jobs remain unchanged.
- Manual: GitHub Actions **Official Hyperliquid and KuCoin funding-history
  originals**, specify venue and a completed `YYYY-MM` or `auto`.

Sources:
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-public-funding-history

No provider keys, external AI, automated orders, model promotion or invented
missing records. Other data families still need their own genuinely available
source-native acquisition contracts.
