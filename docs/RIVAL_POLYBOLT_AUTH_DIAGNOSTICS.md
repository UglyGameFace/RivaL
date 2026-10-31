# Verify Polymarket CLOB credentials without exposing them

RivaL uses three host-only settings: `POLYMARKET_CLOB_API_KEY`, `POLYMARKET_CLOB_API_SECRET`, `POLYMARKET_CLOB_API_PASSPHRASE`. `RIVAL_POLYBOLT_ENABLED=true` enables the read-only oracle worker (already the default). Discloud changes may require a restart before the process loads new values. **Never paste credential values into Discord, GitHub, logs, or this chat.**

After the updated bot starts, run `/rival-tools health`. The **Polymarket oracle research** section distinguishes:

- **credentials needed**: one or more required environment variables are absent or empty
- **not authenticated**: settings are present but no authenticated server acknowledgement has arrived; review the safe provider-state field
- **authenticated, awaiting subscription**: server accepted credentials but has not acknowledged `price.crypto.twap`
- **authenticated, awaiting fresh TWAP**: subscription acknowledged, but no currently fresh, source-verified Chainlink TWAP data has been received
- **LIVE verified oracle samples**: real source-aligned prices have been accepted within the eight-second freshness window. The status gives asset names, count, last accepted timestamp and connection attempts, **never raw credentials or sensitive values**

The bot logs one read-only worker-start event, source connection attempts, authenticated and subscribed acknowledgements, and the first real accepted batch. It does not log tick prices, auth frames or account details. Transient provider `auth_unavailable` now retries after bounded backoff; invalid credentials stop instead of looping forever.

Important: publicly accessible `gamma-api.polymarket.com` and `clob.polymarket.com/book` HTTP 200 responses are **not** evidence of authenticated PolyBolt access. `RivaL silent Polymarket research studied=N` is existing book-based research; oracle v2 needs a valid Chainlink source reference for each contract, as well as actual verification of exact settlement rules and future terminal receipts. Successful login cannot demonstrate a profitable model, calibrate it instantly, or approve trades.

If the source confirms valid samples but the experimental oracle ledger stays empty, inspect whether each market actually carries a verified price-to-beat, same-source settlement rule, nonstale book quote, sufficient volatility history and a valid remaining window. Do **not** weaken these checks to generate artificial observations.
