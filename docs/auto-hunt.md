# RivaL Auto-Hunt: choose your alert source

The ordinary member flow is **/rival → Auto-Hunt**. Choose exactly one:
**Follow Polymarket** (read-only current listed markets) or **Follow RivaL
Forecasts** (new genuine forecast direction research). The selection is opt-in,
saved per user, changeable, and revocable with **Stop Auto-Hunt** or /rival-stop.
It needs no typed target or coin selection. Legacy Follow in DM and manual
price targets remain distinct.

## Follow Polymarket

- The international **polymarket.com** Gamma and CLOB public read-only
  interfaces are used. This is not the separate Polymarket US trading
  exchange or an assurance of US market eligibility.
- For BTC, ETH and SOL, 5m and 15m, construct the event slug from the UTC
  start of the currently listed market window. Fetch the exact Gamma
  event, verify returned slug, active/closed state, event start and expiry,
  market's *Up* and *Down* outcome token identities. Then obtain fresh
  actual CLOB books for both sides and display quoted ask prices and the
  verified market's own URL.
- At most six event windows per scan, cached for 15 seconds and source checks
  concurrent across asset/horizon. Unavailable listings, blocked sources,
  missing timestamps or liquidity, expired windows and inconsistent metadata
  do NOT trigger market notices. Never substitute spot prices for the actual
  Up/Down outcome token.
- A Polymarket notice is **not** an RivaL model prediction, win probability,
  market edge, trade instruction or order. Polymarket's 15m payout rule is not
  equivalent to RivaL's five-minute target candle on its 15m forecast horizon.
  Consequently these are MARKET watches, not falsely aligned model picks.
- The *Review Fresh Signal* button re-fetches the same listed market and
  refuses stale or expired quotes. User-initiated external market links only;
  no wallet connection, exchange order or automated wagering.

## Follow RivaL Forecasts

- Uses only the existing ledger's genuinely prospective new BTC, ETH and SOL
  5m and 15m forecasts. The private alert is allowed even if no profitable
  entry, model victory over baseline or live executable quote is available.
  Its raw direction strength is labeled **not calibrated**.
- Ignore old imports: candidate must be issued no more than three minutes
  ago and have at least 90 seconds remaining. At most one latest forecast
  per asset/horizon appears, never ten correlated venue DMs.
- The strict /rival-signal **trade qualification** remains separate; it
  still demands valid venue-matched quotes and forward evidence before
  displaying a qualified watch. Forecast notifications cannot bypass that.

## Discord delivery and safeguarding

- Persistent source selection table in existing SQLite or PostgreSQL, with
  the previous forecast source as default for legacy opted-in users.
- A new DM for each uniquely recorded window/forecast at most once per
  person. At least 120 seconds between attempts per person. A claim is saved
  *before* network send so restart/timeout never generates duplicate DMs.
- On startup, a lightweight worker checks only when someone has opted in,
  with a 15s fallback and an immediate event wake from math issuance.
  Bounded per-user asynchronous concurrency keeps DMs off the gateway
  loop. Explicit stops serialize with each user's own delivery.
- Button recovery handles a detached message-specific Discord component
  that otherwise shadows an available persistent safe view. Crypto menu
  preview DB access is bounded by 1.5s, so a busy collector cannot hold
  navigation for 12 seconds. If unavailable, use Get Signal for a full
  on-demand research check.
- Start/source-switch attempts first verify DMs can be sent, then store
  selection. Do not promise delivery until confirmed by message ID.
- Read-only public data is not a legal determination of platform
  eligibility or a guarantee that a person can actually trade there.

## Release acceptance

1. Green Ruff, full SQLite and PostgreSQL16 pytest at exact PR head.
2. Green main CI and confirmation that the existing guarded Discloud release
   actually uploads the exact commit.
3. Live gateway READY and Discord owner/regular-user Android test: choose
   Polymarket, receive real listed-market alert, verify price matches market,
   review with fresh price, switch to RivaL, observe real newly issued
   model alert, stop and verify no more alerts.
4. No claims of delivered notifications, profitability or executable fills
   without actual production records.

**Not implemented:** automatic betting, sports Polymarket bets, guaranteed
winning picks, fake forward signals, accessing region-restricted trading.
