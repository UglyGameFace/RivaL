# Autonomous verified market-spike analytics

RivaL's existing five-minute 10-exchange collector remains authoritative.
A lightweight spike watcher runs once per existing collector cycle and reads
only previously stored, source-exact contiguous closed candles. It does not
call exchanges, Google Drive, LFM3 or another API, and skips optional work when
the bot's memory-pressure circuit breaker is active.

A source qualifies only with a successful fresh feed check, 27 consecutive
five-minute bars, last close observed within 12 minutes of that close and a
common UTC opening shared with >=3 venues. It compares log price returns
within venues, never raw USD/USDT/USDC prices. A price spike requires >=0.8%
median five-minute movement and >=3.5 times the recent typical absolute move,
with at least three agreeing venues. A volume spike requires >=2.5x the
median of each venue's recent 26-volume baseline for >=3 reporting venues.
Unavailable or stale volume remains missing, never zero.

The event is saved once per asset and UTC target in PostgreSQL. Its immutable
source manifest includes venues, quote currencies and event-time closes.
On subsequent collector cycles a future-only shadow evaluator compares its
next candle against the pre-recorded event, requiring at least three exactly
matching source identities and future candles observed **after** the event,
within 12 minutes of their actual close. A missing/late original is marked
`unverified_future_coverage`, not a false win or a false loss. All status
and outcomes survive restarts and retries. No extra worker needs recovery:
the existing collector task and host watchdog already restart failed tasks.
Failures on this optional path cannot interrupt live collection.

The mobile Discord Math → Spikes & volume view shows last verified scan,
24-hour event counts, continuation/reversal/unverified shadow outcomes and
the latest three events; the existing local LFM3 on-demand evidence can include
a bounded, read-only summary. No background AI requests are introduced.
The observed event outcome stats are an experiment dataset only; sample
selection, event confidence, fee/quote coverage, prospective Brier, and
after-cost advantage must be established separately before a model learns
usable predictive weights or promotes any signal.

Historical Google Drive originals and other source families still need
source-specific SHA-256 verification and legitimate source-native training
through RivaL's existing Drive→candidate registry→PostgreSQL mathematics.
This addition is *not* proof all those archives are already ingested by LFM3.


## Mathematical correctness audit

The forward-adaptive model's Brier statistics in PR #263 correctly retained
source-disputed future outcomes, but the legacy *Discord analytics* still
silently dropped those targets from its Brier mean. The analytics now scores
genuine squared errors across every properly settled and causally ordered
venue; contracts on one venue are averaged first, then venues equally per
UTC target. Disputed outcomes remain explicitly counted. Only **binary**
directional hit rate, probability calibration and theoretical trading
costs exclude disputes, because a single synthetic fraction would invent
a trade outcome. Weekly improvement trends require 100 UTC targets and at
least five distinct UTC days in **each** week.

Independent tests use two sources with known squared errors .01 and .64:
the correct balanced Brier is .325, even with an additional duplicate
contract from the first venue. Volume-only events have no implied
up/down forecast and cannot score a directional continuation. Testing
formula correctness is not equivalent to proving profitable accuracy.
