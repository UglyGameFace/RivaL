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

## Automated multi-asset recovery

The readback-verified 2023-11 production batch contained 720 legitimate
funding events per BTC and ETH (16 original pages total), settling slightly
after the hourly clock boundary. The first report incorrectly required
literally xx:00:00.000 timestamps; the corrected qualifier now requires
one genuine UTC event in **every** hourly bucket, with no missing/duplicated
bucket and evidence the pages crossed the start boundary. It does not
change the original bytes or any live model.

Six serialized scheduled jobs per day rotate through all completed native
v4 UTC months from 2023-11 to the latest completed month. The preferred
four tickers are BTC, ETH, SOL and XRP; missing/unlisted markets are
reported separately rather than substituted. With 36 completed months
the *ideal* first rotation is six days, but rate limits, missing contracts,
upstream retention and Drive write failures may extend it. Repeated runs
keep deterministic batch identities and previously stored SHA evidence.

A completed v4 funding month does not imply complete L2/trades/candles or
historical v3 coverage. Source rights, unavailable months, and 2021-v3
history remain explicit separate gaps.
