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

## October 30 UI regression and model-readiness investigation (#117)

- The supplied `rival_ui.py` example was **not integrated into `src/parlay_bot/discord_app/home.py`**. Its `fetch_signal` always returned hardcoded `direction="UP"`, `price=68420.0`, `strength=3`, and `accuracy=58.0`. Its persistence and Follow callbacks were placeholder hooks. Never publish these demo numbers as actual RivaL outcomes.
- The production screenshot instead showed BTC 5m Hyperliquid measured directional accuracy **50.1% of 339 settled forecasts** for the preceding seven days, model underperforming a simple baseline, and an after-cost severe estimate of **-82.3 bps**. Those metrics describe the displayed research cohort; they neither validate that mock 58% nor prove a Polymarket trading edge.
- The runtime XRP six-month audit on October 30 reported **9 requested exchange streams, 0 complete**, with tens of thousands of five-minute observations missing on each. A long historical exchange warehouse is not a complete nine-venue coverage guarantee, and the forward event ledger cannot inherit pseudo labels from imported historical candles.
- For Polymarket BTC 5m/15m, the actual market rule says Chainlink BTC/USD 60-second TWAP over the named contract period settles Up versus its starting TWAP. Exchange five-minute candle forward forecasts, even when generated from six months of bars, do not directly score those oracle-specific outcomes.
- Source-of-truth roles are distinct: Gamma confirms listed Polymarket windows; CLOB supplies current ask **share prices**; Chainlink/TWAP and resolved contract history supply the binary **outcome labels**; RivaL's prospective exchange model supplies **research leans**; final Polymarket contract probabilities require their own pre-event predictions, label matching, independent calibration and after-cost forward evidence.
- Fix in PR #117 restores the actual supplied two-column card design via native Discord inline field spacers and real model/quote sources. The forecast and Polymarket live board clearly show a fresh model **UP/DOWN lean** when one exists, while the separate Polymarket contract verdict stays WAIT without correctly calibrated contract evidence. Expired data do not inherit a previous direction. Model strength and settlement accuracy have separate labels.
- **Still unimplemented:** durable source-verified Chainlink TWAP/settled Polymarket outcome ingestion, each market's source/provenance and label audit, chronological leak-free training/evaluation, per-asset/per-window calibration with enough independent forward examples, price-adjusted decision threshold, and production promotion/drift control. Treat these as a distinct data-science outcome, not an alert toggle or UI issue.
- **Deployment gate:** this source branch must pass CI and merge, then Discloud must actually deploy the correct SHA and show live updated private cards; the standalone demo is not a replacement for the real Discord bot.
