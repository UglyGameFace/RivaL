# dYdX v4 original funding and oracle price recovery

This is a source-specific, no-auth public Indexer funding collector with strict
dYdX chain version v4 identity. dYdX v3 history is separate, and its 2021
funding cannot be relabeled as v4.

- Source: https://indexer.dydx.trade/v4/historicalFunding/{ticker}
- UTC: completed months on/after Nov 2023, exact BTC-USD/ETH-USD etc.
- Responses: maximum 100 rows and ten historical pages per asset/month.
- Every downloaded original response is stored unchanged in RivaL history,
  together with its deterministic SHA manifest, with subsequent actual
  downloaded-byte verification from the existing private Google Drive client.
- Funding rates, effective timestamps and oracle reference prices are
  checked for timestamp monotonicity, strict source identity, numeric sanity,
  and repeated rows. Strict full month requires every UTC hourly settlement;
  if pages fail to reach the earliest boundary, coverage stays incomplete.
- A missing month or contract is a visible upstream blocker. This collector
  does not fill missing hours or borrow another exchange's funding.
- Original archived bytes are offline research; no automatic signal changes,
  model approval, or retrospective predictions treated as forward evidence.

The new workflow performs a real, bounded source + Drive acquisition after a
validated merge and supports explicit historical-month selection for further
recovery. Full unattended 2023-present monthly backfill, source-native trade
history and historical order-book snapshots remain separate completion gates.

Official docs: https://docs.dydx.exchange/api_integration-clients/indexer_client
