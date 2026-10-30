# RivaL Auto-Hunt: opt-in research alerts

Auto-Hunt is an opt-in, no-strike, **no-betting** crypto research watcher in the
existing /rival home. It is not a wagering service, financial recommendation,
trading engine, or guarantee of profitability.

## Ordinary member flow

1. /rival → **Auto-Hunt** → **Start Auto-Hunt**.
2. Permit the bot's private DMs. A setup message confirms DM reachability.
3. RivaL independently watches newly issued BTC, ETH and SOL forecasts at 5m
   and 15m without requiring a manually entered target.
4. A **new** private message is sent only when RivaL verifies the forecast
   exists in its prospective ledger, is less than 90 seconds old, has at least
   90 seconds left, scores at least the existing 56% raw direction threshold,
   has a source-specific 7-day forward model beating its benchmark, and has
   a current matched-venue bid/ask with valid exchange timestamps, depth and
   spread no more than 25 basis points.
5. **Review Fresh Signal** rechecks the current market and explains missing
   evidence or expiry. **Dismiss** removes that message's controls.
6. **Stop Auto-Hunt** revokes Auto-Hunt only. /rival-stop stops both
   Auto-Hunt and existing Follow-in-DM / manually configured target messages.

Auto-Hunt does not claim a quote is an order fill, invent a betting strike,
open a wager or make an exchange order. A positive research signal does not
prove positive after-cost expected value. Models with unverified forward
evidence, incomplete source coverage or invalid quotes are **not alerted**.

The former manually entered Set Price Target is still available under
**Advanced → Manual Price Target**, but not required for Auto-Hunt.
Existing Follow in DM remains one edited forecast card for the selected coin.

## Runtime and persistence

- Existing PerpetualMarketCollector and AutonomousResearch remain the
  only forecast issuers. There is no new collector, exchange subscription,
  WebSocket, sportsbook API, or model.
- A bounded worker scans only when subscribers exist, every 15 seconds or
  sooner when the existing automatic research cycle reports new issuance.
  Individual assets/providers are isolated. Historical cold recovery never
  triggers old-model betting alerts.
- Existing recent_watches / recent_multi_watches, promotion policy,
  source-specific venue_summary / combined model proof, and _venue_book
  are the same authoritative decision inputs used by /rival-signal.
- rival_hunt_subscriptions is an opt-in, per-user Postgres/SQLite table.
  rival_hunt_delivery is a write-before-send **at-most-once attempt** ledger
  keyed by user and model/venue/issued/expiry SHA. It stores status,
  actual message ID and review timestamp. It never retries an ambiguous
  Discord send, avoiding duplicate alerts after restarts.
- At most 100 simultaneously opted-in users and at most one alert attempt
  per 120 seconds per user. Unlike the previous follow service, each
  eligible new opportunity sends a **new** DM, not an edited old DM.
  Deliveries are not broadcast into public guild channels.
- The Discord button View is persistent across bot restarts; click-time
  identity and the current signal are revalidated. Old alerts are never
  an automatically executable order.
- Missing PostgreSQL credentials/permissions, blocked DMs, missing forward
  training, exchange geoblocks, stale books and quote timeouts fail safely.
  A saved alert attempt is not proof the user received a DM.
- The delivery ledger is strictly separate from actual sportsbook bets
  and from simulated profit ledgers.

## Release acceptance

- Exact PR head Ruff, entire pytest suite and PostgreSQL16 CI green.
- Verify that existing model/delivery/price-goal endpoints and old guided
  views have not regressed. Confirm four existing top-level Discord commands.
- Explicit owner merge and guarded Discloud release of exact main SHA.
- Live Discord Android test with a fresh member opting in, receiving a new
  research-only DM on a genuinely new qualifying forecast, tapping Review
  after expiry, stopping then observing no more messages.
- Confirm row-level delivery attempts/successes/latencies in production
  without exposing user IDs or confidential prices.
- No profitability, live market fill or guaranteed latency claim before
  independent forward tests and actual production evidence.

## Not included in this first production-safe implementation

Sports Scout alerts need a separate eligible-market/venue/odds/deeplink and
per-user jurisdiction screen; sports odds alone do not prove a model edge.
Early Warning pre-boundary predictions are not issued by the current honest
5-minute candle model; advertising advance outcomes would be fabricated.
They are future work, not controls that pretend to function.
