# Verified macro and world-event context for RivaL

The existing `AutonomousResearch.cycle` now polls a **small, fixed official-source
allowlist** every 30 minutes (persisted in the same PostgreSQL store), even if no
Discord user interacts with the bot. RivaL does not create another bot, another
decision engine, a new subscription or a paid API dependency.

### Actual source adapters
- Federal Reserve monetary policy announcements:
  `https://www.federalreserve.gov/feeds/press_monetary.xml`
- SEC press releases, limited to titles referring to crypto/digital assets:
  `https://www.sec.gov/news/pressreleases.rss`
- BLS official downloadable news-release calendar:
  `https://www.bls.gov/schedule/news_release/bls.ics`
- Global headline **discovery, not verification**: the free, keyless
  GDELT DOC API (`https://api.gdeltproject.org/api/v2/doc/doc`), restricted
  to a static crypto/macro/geopolitical query, an overlapping one-hour
  rolling window, at most 40 articles per poll, and an explicit list of
  established news-site hostnames. Domain allowlisting does **not**
  authenticate an article or establish the truth of its headline. Original
  news-site pages are never fetched.

GDELT articles are separately marked `indexed_secondary_unverified`,
retain their original publisher link and upstream index-first-seen time,
and have `published_at=null` because an index timestamp is **not** a
publisher's original publication clock. Unknown headlines are skipped;
crypto incident, macro and geopolitical topics are only text-matched
research leads. GDELT never earns a government-source credibility label,
and cannot cause an automatic trading direction or NO CALL by itself.
`rival_macro_index_provenance` stores original upstream index time and
display-only publisher domain alongside the immutable first-observed records.

A source-specific failed request is recorded as unavailable and cannot
silently become an "all clear". Each official source has a bounded response
size and per-request timeout. HTML/news content is **not** downloaded as code
or instructions. RSS links must remain on the exact source host; responses
do not follow redirects. The CFTC RSS feed is deliberately **not used**
because its own official RSS page says feeds are temporarily disabled as
of October 2026. On-chain/ETF/options/macro-consensus, real-time cross-asset
feeds and global newswire access are **not** claimed by this implementation.

### Causal event ledger
`rival_macro_events` stores source, stable event identity, title, official URL,
category, source publication time, scheduled event time, first observed
(ingested) time, last observed time and exact event-content hash. An additional
`rival_macro_revisions` records every distinct content hash and its
first observation. Re-polls never rewrite the first-seen timestamp.
`rival_macro_sources` durably tracks polling and last successful source
health, including across restarts. No historical event is masqueraded as an
original live observation, and an announced calendar revision is retained.

### Decision and LLM boundaries
The **existing** background research evidence now includes the official
event-context status, source timestamps, short citation-linked headline
summaries, and observed-risk flags. This can inform the current LLM's
research explanations, **not** model weights or fabricated impact scores.

The existing `signal_embed` read-only decision gate withholds new qualified
WATCH calls in verified high-impact BLS CPI/PPI/employment/JOLTS windows
(15 minutes plus selected forecast horizon before the release, through 30
minutes after). Newly observed official Federal Reserve **FOMC statements**
also withhold new WATCH calls for a bounded post-publication window.
This is explicitly a volatility precaution, **not** a claim to know whether
a political or economic decision will push an asset UP or DOWN.

Ordinary SEC headlines are presented as verified contextual research, not
universal vetoes. Missing sources are shown as unavailable, never as verified
absence of world events. No LLM can bypass the event veto. Unaffected
collectors and genuinely issued paper forecasts keep running for honest
ongoing learning; this does not backdate results or erase previous calls.

### Still unproven / next work tracked in Issue #151
Global index results are not complete world news; website access, coverage,
language and query filtering limit the sample. Multi-source corroboration,
source reputation calibration and origin-authenticated reporting remain
required before classifying any individual story as verified. The existing
OpenRouter and Gemini research prompts are updated to interpret official
source evidence and treat third-party indexed headlines as unverified data,
never higher authority than source-matched mathematical risk controls.

Full newswire coverage, economic expectation/actual *surprise*, economic
data vintages, event-to-market causal validation, institutional/ETF activity,
credible on-chain/derivatives context, political-event contract settlement,
source cross-confirmation, and forward-tested improvement **are still open**.
Historical macro and sentiment features cannot be retrospectively treated
as available at their occurrence without verified publication timestamps.
A semantic LLM classification must remain research-only until evaluated on
genuinely future data; no automatic promotion or trading authorization.

Run the ordinary `ruff check .`, `pytest`, hosted CI, exact-commit
release and inspect `RivaL official event research` logs plus source
health in PostgreSQL before claiming operational coverage.
