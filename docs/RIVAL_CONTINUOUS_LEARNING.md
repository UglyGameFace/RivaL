# Continuous selected-contract calls and persistent learning

## Behavior

- Selecting Polymarket requests exact-contract research, including the verified condition, tokens, closing window, settlement source, published rules and source quote times. Bitget/exchange forecasts are excluded from this path.
- Start Auto-Track edits one DM about every 15 seconds until Stop. Current/next listings roll forward only after validation. Open menu previews also finish background source/AI work and update in place; the persistent DM is the continuous tracker.
- The source reader has an eight-second total budget, connection reuse and independent shared tasks. Canceling a menu waiter does not cancel a shared read. Quotes older than 25 seconds are not renewed by the cache.
- Free-only OpenRouter remains catalog-verified with one shared persisted 18-request daily cap. Contract revisions require at least 60 seconds and a five-cent move. A call retains its real issue time and expires with its exact contract. Missing data, quota or errors produce an explicit status, never an invented call.
- Hourly contracts require a year-qualified, Gamma-verified listing and the published Binance spot hourly settlement source. Shorter-window oracle contracts remain distinct. Unsupported/unlisted hourly markets do not create a tracker.

## Learning and serving

- Live exchange research uses `prospective-logistic-persistent-v3`. Learned parameters, training cursor, source fingerprints and hashes are stored in PostgreSQL. Only newly known labels update weights; rolling candle eviction and restart retain accumulated learning. Corrections rebuild research state; earlier clocks cannot read or overwrite future state. Old v2 forecasts and outcomes remain intact, but their results do not qualify v3.
- Historical training uses the exact 40-bar/10-feature live input and strict future-five-minute close target. It compares three fixed regularizations and a logit ensemble on a chronological validation set, purging labels that cross split boundaries. The chosen method is tested once on the final holdout. This evaluates a small reproducible family; it does not establish a universally superior forecasting method.
- Compatible, screened artifacts have their own persistent challenger weights and immutable live predictions. Research selection needs at least 100 nonoverlapping paired targets across seven UTC days, sufficient Brier improvement, consistent daily benefit, a positive day-block stress screen and calibration error at most 0.10. Failed/stale/missing evidence retains the incumbent. The existing matched quote and after-cost gates still govern qualified trade calls.
- The original 42-model bundle is accepted as an immutable inventory with its real weights and original feature contract. Its six-weight models are not loaded into the ten-feature engine. Drive reads run separately every six hours; live inference uses PostgreSQL, not a per-call Drive download.
- Monthly historical research and research-code main releases are enabled by default; `RIVAL_HISTORY_MONTHLY_ENABLED=false` is an explicit stop switch. Missing remote credentials/upload failures remain visible and cannot be reported as verified archive synchronization.
- The independent Polymarket learner records the first verified book observation per contract, including token/rule evidence. It learns only from exact-condition terminal, binary payout receipts with transaction/block evidence. Observations are collected even without an AI call. Split/nonterminal/incorrect-condition payouts cannot supply labels. Receipt checks rotate so unresolved conditions cannot block all later work.
- A local Polymarket book model needs 100 resolved contracts per asset/timeframe/settlement source before issuing experimental calls. Its weights survive restart and its quotes can be rescored without using AI quota. These calls are labeled model research. Repeated ticks contribute only one frozen forecast per contract to forward accuracy. AI confidence is not a target, feature, calibrated probability or promotion vote.

## Actual October 30 historical rerun

Official Binance USD-M monthly archives for April–September 2026 were downloaded and SHA-verified: 18 five-minute partitions, BTC/ETH/SOL. Nine asset/timeframe selections were trained and tested. **Zero passed the historical admission gates.** No production accuracy improvement or profit is established.

| Contract | Horizon | Validation Brier gain | Final test Brier gain | Final test accuracy |
| --- | ---: | ---: | ---: | ---: |
| BTCUSDT | 5m | -0.002357 | -0.000871 | 51.92% |
| BTCUSDT | 15m | 0.000416 | -0.000031 | 50.95% |
| BTCUSDT | 60m | -0.000081 | 0.002919 | 53.64% |
| ETHUSDT | 5m | -0.002350 | -0.002043 | 52.65% |
| ETHUSDT | 15m | -0.000171 | -0.000426 | 53.32% |
| ETHUSDT | 60m | 0.000493 | 0.002290 | 52.28% |
| SOLUSDT | 5m | -0.003912 | -0.002200 | 52.18% |
| SOLUSDT | 15m | -0.001461 | 0.000550 | 51.84% |
| SOLUSDT | 60m | -0.003333 | -0.005028 | 52.39% |

Positive Brier gain means lower squared probability error than the training-only base-rate baseline. Small positive test gains alone do not pass both chronological screens, demonstrate reliable live probabilities or establish after-cost returns.

## Verification and limits

Automated checks exercise SQLite and PostgreSQL, cancel-safe source sharing, hourly/year/source validation, wrong/expired AI responses, shared quota across restart, immutable call/outcome records, rolling online memory, historical feature/source isolation, forward challenger selection and rollback, holdout isolation and receipt-only Polymarket learning.

Public read-only Gamma/CLOB, hourly listing and terminal-resolution probes succeeded during development. This does not exercise the production OpenRouter key, Discord permissions, provider availability or continuous hosted latency. Release acceptance requires current startup/learning logs, actual contract calls, DM edits across rollover, quota/error statuses, and Stop from the running Discloud deployment. New Polymarket training and forward scores need genuine future observations; fixtures do not create that evidence.
