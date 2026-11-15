# RivaL Operations Dashboard

The Cloudflare `rival-evidence-board` Worker is now the **read-only RivaL operations dashboard**, not a mini GitHub. GitHub activity and research provenance remain available under **Diagnostics**, but do not form the primary product.

## Main screens

| Screen | What users see | Real source | Important gaps |
| --- | --- | --- | --- |
| **Command** | RivaL production cycles, forecast/settlement counters, XMarket data coverage, recent BTC/ETH/SOL/XRP quotes, true model qualification and top operational blockers | Signed GitHub-OIDC Discloud/DB/Drive receipt plus separate live public market API | Source health is dated; true per-call signal feed not yet published |
| **Markets** | BTC/ETH/SOL/XRP median prices, 24-hour change/volume/open interest, supported venue status, venue quotes/spreads/funding, Polymarket 5/15/60-minute public CLOB asks and Kalshi crypto market summaries | Existing read-only `rival-public-site` Worker, through a narrowly allowlisted service binding | Indicative quotes cannot prove RivaL predictions or executable orders |
| **Signals** | Issued/settled forecast counts, the 12 asset/horizon paired scoring scopes, four independent market-engine evidence classes, current promotion gates | Signed host and PostgreSQL performance receipt | **No individual prospective forecast direction, entry, SL/TP, model probability or contract order is claimed connected** |
| **Models** | Per-scope model vs baseline Brier comparisons, correlated pair counts, distinct target windows, source timestamp and method, observed online weight updates | Signed PostgreSQL paired settled scoring | Neither win rate nor after-cost edge is inferred. Exact deployed model hash/lineage unavailable |
| **XMarket** | 12 training reports and gates, exports by asset, latest import count, XMarket shadow row count, per-scope evidence drilldown | Signed host/Drive receipts; separately dated archived historical snapshots | Five reported host imports were **not** linked by artifact hash to the latest passing Drive reports. Shadow rows still zero at last receipt |
| **Intelligence** | LFM3 container reachability versus verified private inference, unavailable CPU/RAM/TPS/fallback/restart health in explicit slots | Bounded signed Intelligence host observations | No authenticated private inference measurement yet. Container online is not model-serving proof |
| **Diagnostics** | Source provenance, the earlier Evidence Board research, CI queue, code changes, and audit trails | Signed receipts and bounded official GitHub read-only endpoints | Never treated as primary model-readiness metrics |

Android has five persistent bottom controls and a horizontally scrollable full section menu; the **More** control exposes XMarket, Intelligence and Diagnostics. The default route now opens **Command**, never GitHub tickets.

## Two independently timestamped read-only data pipelines

1. **Signed production evidence**: GitHub Actions `.github/workflows/evidence-board-receipt.yml` collects bounded authenticated Discloud, PostgreSQL, private Drive and Intelligence observations, obtains short-lived GitHub OIDC identity, and publishes to the separate Cloudflare `rival-evidence-collector` Worker with signature/issuer/repository/main/ref/event validation. The collector retains only allowlisted sanitized data in KV. Website refresh checks the saved receipt at most once a minute. GitHub cron is best-effort; a successful main push or same-repository successful Production Release can trigger additional publication. Every health claim must include the capture time and source age.
2. **Public market quotes**: the dashboard Worker binds `RIVAL_MARKET_FEED` as a Cloudflare service binding to the already deployed `rival-public-site` Worker. Only `GET /api/perps`, `GET /api/polymarket?asset=BTC|ETH|SOL|XRP`, and `GET /api/kalshi?asset=BTC|ETH|SOL|XRP` are invoked. They are exposed by the dashboard under `/api/market/perps` and `/api/market/predictions?asset=...`, respectively. The server uses an explicit per-field allowlist; strips token IDs, cookies, all private data, unexpected fields and unrecognized assets; and never forwards caller headers or arbitrary URLs. Each upstream has an independent result, unknown values remain `null`, bad sources fail closed or return partial read-only results. Public quotes update about every 30 seconds while the page is visible and are labeled separately from Discloud evidence. Source worker-side market caches avoid repeated exchange fan-out on every page paint.

The dashboard never sends an order, stores credentials in its public script, issues a model prediction, initializes production tables, trains a model, publishes to private Drive, writes Discloud data or changes model promotion rules. The public site itself has trading disabled.

### Cloudflare deployment requirements

This Worker intentionally remains a **legacy Service Worker** (`addEventListener('fetch',...)`), not a module upload. Deploy its exactly merged and tested `web/evidence-board/worker.js` via `PUT /accounts/{account_id}/workers/scripts/rival-evidence-board`. Preserve the existing `RIVAL_EVIDENCE_RECEIPTS -> rival-evidence-collector` service binding and compatibility date `2026-11-01`. Add **only one** additional service binding: `RIVAL_MARKET_FEED -> rival-public-site` with environment `production`. The new code returns `503` for public markets without that binding; it never substitutes fixture prices. Leave secrets on `rival-public-site`, do not duplicate them on the public dashboard.

Post-release checks: fetch the deployed source and compare it byte-for-byte to the merged GitHub commit; confirm both service bindings, compatibility date and fresh `/api/status`; verify `/api/market/perps` has BTC/ETH/SOL/XRP entries with source timestamps and unavailable venue statuses; verify `/api/market/predictions?asset=BTC` has separate Polymarket and Kalshi statuses with their actual quote-source metadata; reject `asset=DOGE`. Render **Command**, **Markets**, **Models**, **XMarket**, **Intelligence**, and **Diagnostics** at 390px mobile and desktop widths. Confirm stale or missing sources show unavailable, not zero/green.

## Validation and limitations

- `node tools/test_evidence_board_ui.js` checks the operational views plus legacy evidence semantics with no network.
- `node tools/test_rival_operations_api.js` mocks the service binding and tests GET-only strict route allowlists, missing bindings, quote sanitization, unknown/invalid asset behavior and independent partial source failures.
- `tests/test_evidence_board_ui_source.py` runs both Node checks and collector regressions under the normal Python full CI suite.
- Existing `tools/test_evidence_collector.js` and `tests/test_evidence_board_receipt.py` continue verifying receipt provenance and read-only constraints.
- No live RivaL personalized holdings, position sizing, wallet, per-contract model targets, trade P&L, live private inference metrics, or economic edge can be filled from the current available sources. The dashboard exposes clearly labeled gaps rather than performing private proxy access or generating sample statistics.

## Next integration, separate from this UI release

Publish an allowlisted **immutable prospective per-call ledger summary** (model family/version, market/provider, asset/horizon, prediction direction, probability, created/target times, quote quality, issued/settled/status) using the same private signed read-only collector. That must be built with source-level privacy/identity checks and must never publish raw credentials, prompts, private trades or unverified model weights. Separately add authenticated non-sensitive LFM3 latency/TPS/memory/fallback summaries. Those are evidence-source enhancements, not a reason to leave GitHub ticket lists on the home screen.

The current confirmed research status at the November 14, 2026 7:05 PM EST receipt was 5 imported XMarket scopes, 0 issued XMarket shadow rows in the host window, 7 offline-gate-rejected and 5 insufficient-label-blocked reports among the 12 latest/mixed-age reports, and no qualifying after-cost paper scope. That snapshot is illustrative; the deployed UI reads later signed records and never hardcodes these counts.
