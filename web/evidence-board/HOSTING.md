# RivaL Evidence Board: independent hosting and recovery

**Canonical URL:** https://evidence.the420lobby.com/

The RivaL Evidence Board (the read-only operations dashboard) is hosted by the user's own Cloudflare Worker, **not** a ChatGPT Site or ChatGPT subscription. The hostname was attached on November 15, 2026. The existing public trading-information site at https://rival.the420lobby.com/ is a different Worker and must not be overwritten.

## Ownership and source of truth

- **Cloudflare zone:** `the420lobby.com` (active).
- **Worker:** `rival-evidence-board`.
- **Other Workers that the dashboard depends on:** `rival-evidence-collector` (signed evidence) and `rival-public-site` (read-only market feeds).
- **Permanent source:** this repository, `web/evidence-board/worker.js` and `web/evidence-board/collector.js`. Behavior and caveats are described in `web/evidence-board/README.md`.
- **Public fallback URL:** https://rival-evidence-board.staytrippynlw.workers.dev/
- **Deployment on November 15:** Cloudflare showed the Evidence Board Worker deployed through the API and serving 100% of Worker traffic. The deployed script (170,580 JavaScript characters, FNV-1a `46a5929d`) matches the GitHub source at commit [`a547854`](https://github.com/UglyGameFace/RivaL/commit/a547854083449f209f1cfd0e1e9582afc987b69d). Current `main` is **newer**, so do not assume its version is live. This **does not** mean GitHub automatically deploys to Cloudflare: the Worker currently has no Workers Builds configuration, build token, or configured trigger. Keep the known-good deployed version for rollback before synchronizing with current tested `main`.
- **DNS/TLS:** Cloudflare Worker Custom Domain for `evidence.the420lobby.com`. Cloudflare owns the DNS record and certificate provisioning. Do not add a separate DNS record, or change the site's existing apex/WWW/other subdomain records.

## Required runtime contract

The `rival-evidence-board` Worker is presently a **classic Service Worker** (`addEventListener('fetch', ...)`), not an ES module. Keep the existing compatibility date (`2026-11-01`) and two service bindings:

| Binding | Destination | Purpose |
| --- | --- | --- |
| `RIVAL_EVIDENCE_RECEIPTS` | `rival-evidence-collector`, production | Read signed, sanitized host/DB/Drive/Intelligence evidence |
| `RIVAL_MARKET_FEED` | `rival-public-site`, production | Read allowlisted public exchange and prediction-market quotes |

The collector has its own `EVIDENCE_KV` KV namespace, GitHub OIDC trust policy, and trusted workflow `.github/workflows/evidence-board-receipt.yml`. Preserve those settings when restoring. **Never** copy secrets into the public Worker, public Git repository, or browser. Never replace the collector with a mocked success response.

## Recovery without ChatGPT

1. Sign in to Cloudflare using the account that owns `the420lobby.com`; go to Workers & Pages and inspect `rival-evidence-board` and the custom domain. Cloudflare must show the Worker and its two service bindings.
2. Retrieve the current tested source from this GitHub repository. Inspect the current branch's CI and compare the deployed Worker script to the intended commit. If they differ, choose the tested version, preserving the deployed version as rollback.
3. Deploy the **classic** Worker using Cloudflare's own tools/API, preserving the Worker name, compatibility date, and both existing bindings. Avoid converting to a module, changing the collector, or resetting KV just to republish the UI.
4. Verify HTTPS and GET `/`, `/health`, `/api/status`, `/api/market/perps`, and `/api/market/predictions?asset=BTC`; `/api/market/predictions?asset=DOGE` must be rejected. The `/health` endpoint is **Worker liveness only**. `/api/status` may correctly return **503** for stale or missing signed evidence; do not hide that failure.
5. Check that the dashboard's Command, Markets, Signals, Models, XMarket, Intelligence, and Diagnostics views render on Android. Signed production snapshots have their own capture times and must never be labeled as streaming or a verified profitable edge.

## Important independence boundary

Losing a ChatGPT membership does **not** delete Cloudflare Workers, a custom domain, or GitHub repository code. Those services have their own accounts, billing, limits, and credentials. However, the dashboard is only as fresh as its external sources: its signed GitHub OIDC publisher, private Discloud access, PostgreSQL and Drive evidence, and Cloudflare KV must continue running independently. A stale receipt means stale evidence, **not** necessarily a failed website.

The separate ChatGPT-hosted *RivaL Intelligence Health* workspace is **not** this Evidence Board. It requires its own migration if its source exists only inside ChatGPT Sites.
