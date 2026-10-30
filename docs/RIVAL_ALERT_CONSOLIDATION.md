# RivaL alert consolidation and Polymarket evidence contract

## Authoritative user-facing paths

- **Live Mode → Start Auto-Track**: one explicitly opted-in private message, edited automatically approximately every 15 seconds when its verified observation changes. Displays the present and next **verified** Gamma events, current CLOB ask prices in USD **per share**, optional Gamma-published price to beat, and a separately labeled exchange-candle model context. A source-unverified market or target remains unknown. The Polymarket-specific decision is `WAIT` until settlement-matched, forward-validated edge evidence exists. The card is durable across restarts and can be stopped independently.
- **My Alerts → Other Alert Types**: *optional, existing* notification streams (`polymarket` price-only, model `research`, separately screened `qualified`, model reversals and WebSocket shifts). These are not additional Polymarket prediction models. Their original opt-ins, SQL tables, delivered-message keys and permission controls remain intact.
- **Crypto → Follow Exchange Forecast**: existing, separately opted-in personal forward-candle forecast card; never represent it as a Polymarket contract prediction. Preserve existing follow records.
- **Sports & Odds**: separate sportsbook workflow with protected provider quota, not merged into crypto event data or alerts.

## Sources and matching

1. Global Polymarket Gamma is authoritative for *listed event identity and its start/end*, but a predictable slug alone does not prove a listing. Polymarket CLOB is authoritative for verified Up/Down **purchase asks**, not win probabilities or RivaL convictions.
2. `target_snapshot` reads RivaL's existing, prospective exchange-candle model. Importantly, even a forecast configured for a `15m` horizon predicts a **future five-minute candle**. That forecast is NOT a prediction of the whole 15-minute Polymarket contract.
3. A 5m model lean is displayed as *separate research context* only when source timestamps/closing boundaries line up with the verified current 5m contract. That still does not guarantee matching oracle, calibration, execution fees or profitability. The decision remains WAIT.
4. No raw model confidence and no book ask are to be reported as a Polymarket win probability, combined into a purported betting edge or treated as a recommended real-money bet without proper same-contract labels, forward validation, quote costs and calibration.
5. Missing prices, stale book timestamps, network errors, missing or mismatched model labels and unsupported horizons result in `WAIT` or `unverified`. Never manufacture a strike or borrow another exchange's quote.

## What was consolidated vs deliberately preserved

- Removed redundant on-demand market polling and the manual-refresh control from the guided Live Mode. The background worker is the single owner of its automatic private tracker.
- Removed direct legacy research/price/shift toggles from the primary Live Mode. They remain in advanced notification options, so previously enabled subscriptions are not silently changed or deleted.
- Reused source-matched Gamma/CLOB, existing forward model and existing hot-store systems. Did not duplicate model training code, add a second exchange signal engine, or create fake heuristic betting probabilities.
- **Do not remove** Auto-Hunt delivery records, historical quote evidence or DirectionTargetService without proving that no active subscriber or caller needs them. Future retirement should reuse the existing subscription and data contracts rather than breaking users or resetting their choices.
- The nightly history collector and automatic shadow-learning continue independently. Shadow evaluation by itself is **not** evidence that a new model beats the incumbent or has been promoted into live use.

## Proof still required

- GitHub CI covers configuration, Polymarket source identity, time-aligned models, 15m non-equivalence, stale-price failsafe, in-place DM edits, restart and no duplicate messages. The actual Discloud commit and live Polymarket data must be observed after deployment.
- A meaningful Polymarket-specific UP/DOWN verdict requires a verified market settlement data source, exact oracle-window outcome labels, historical and prospective coverage, calibrated model probabilities, after-cost evaluation, drift monitoring and paper/live forward tests. **None of that is established by this PR.**
- Do not interpret better log hygiene or more raw venue candles as proof of higher win rate, and never promise winning outcomes on every event.