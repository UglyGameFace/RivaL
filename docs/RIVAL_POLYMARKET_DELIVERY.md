# Polymarket delivery and source freshness

## October 30, 2026 continuation after #120

The logged `issue_to_attempt_ms=44383` for a Polymarket notice measured time
since **contract opening**, because `PolymarketNotice.issued` is the window
start. It did not measure time spent waiting in the notification pipeline.
Exchange forecast issuance timestamps still have their original meaning.

Real delays were possible: qualified, research, Polymarket and reversal scans
ran sequentially; all tracker markets waited for the slowest observation;
tracker message edits were sequential; and the extra 15-second sleep followed
the work instead of accounting for it. The listing and book readers also had
independent Gamma request paths.

## Current implementation

- The runtime schedules separate forecast and Polymarket alert workers. Their
  scan locks are independent. Public market scanning continues during stalled
  exchange-model research. Qualified research retains precedence over early
  research for the same forecast window. User locks, daily limits, opt-in,
  cooldowns and the existing durable outbox still govern message delivery.
- Gamma event reads share one in-flight request per exact slug across listing,
  menu, tracker and alert consumers. One bounded HTTP connection pool reuses
  keep-alive connections and closes on shutdown. Five-second caches are bounded; callers
  recompute pre-open/current status from the cached metadata. Display and
  alert readers share both positive and negative validated book snapshots.
- Each market read has a four-second total budget. Invalid, stale, mismatched
  or unavailable books do not yield a notice. The current 90-second alert
  threshold remains separate from late-window display eligibility.
- The older of the two CLOB source timestamps is `quote_at`; local receipt is
  `received_at`. Both the token ID and condition ID must match Gamma. Missing
  order-acceptance flags cannot authorize an alert. These checks follow the
  official [book schema](https://docs.polymarket.com/api-reference/market-data/get-order-book)
  and [event-by-slug schema](https://docs.polymarket.com/api-reference/events/get-event-by-slug).
- Cached reads and the final send recheck source age and expiry. The final
  check occurs after PostgreSQL outbox checkout, immediately before Discord
  send. No ambiguous send is retried after a restart.
- Tracker groups deliver as their observation completes, with eight concurrent
  edits at most. Stop and retarget use a per-user lock; an observation begun
  before either operation cannot restore the previous card.
- Market failures log fixed reason codes such as `book_stale`,
  `book_identity`, `window_mismatch`, `orders_unavailable`, `request_timeout`
  or the HTTP status code. The menu explains common failures in plain language.

## Production evidence to collect

The Polymarket worker targets 15 seconds between cycle starts; slow work may
still exceed this interval. HTTP 200, CI success and a successful upload to
Discloud do not establish delivery latency on the running host.

Inspect `RivaL Auto-Hunt scan source=polymarket scan_ms=...` and each send's:

| Metric | Meaning |
| --- | --- |
| `window_age_ms` | Time since contract opening; not delivery delay |
| `observed_to_attempt_ms` | Local validated receipt to actual Discord send attempt |
| `source_quote_age_ms` | Older CLOB book timestamp to actual send attempt |
| `delivery_ms` | Discord send request duration |
| `status` | Confirmed sent or unconfirmed network result |

A read-only public probe from the development environment returned matching
Gamma/CLOB contract, condition and token identities. A cold read hit the
four-second deadline; the next read on reused connections verified current and
next listings plus both books in 1,248 ms. This is a development-host sample,
not a Discloud latency measurement or a performance guarantee.

The 25-second source-age and 90-second remaining-window checks apply at the
actual attempt. A slow Discord response can still arrive later; the bot cannot
guarantee Discord client delivery time. Runtime logs must confirm real provider
payload compatibility, contract rollover, responsiveness and opted-in DMs.

## Contract-outcome training remains open

This release repairs market reads and delivery. It does not create a
Polymarket-trained directional model or promote exchange-candle forecasts.
`polymarket_contract_audit` remains an offline audit of supplied comparisons,
not a verified source ingestion pipeline. Still required: authenticated exact
contract resolutions and oracle receipts, immutable candidate predictions
issued before the evaluated window, contemporaneous quotes, compatible scoring
features, chronological training/evaluation, and independent forward results
against the paired baseline after costs. Issue #6 remains open.

Rollback is a code revert; this change adds no database migration and removes
no forecasts, settlements, subscriptions, deliveries or historical archives.
