# RivaL Evidence Board: independent hosting and recovery

**Canonical URL:** https://evidence.the420lobby.com/

The RivaL Evidence Board (the read-only operations dashboard) is hosted by the user's own Cloudflare Worker, **not** a ChatGPT Site or ChatGPT subscription. The hostname was attached on November 15, 2026. The existing public trading-information site at https://rival.the420lobby.com/ is a different Worker and must not be overwritten.

## Ownership and source of truth

- **Cloudflare zone:** `the420lobby.com` (active).
- **Worker:** `rival-evidence-board`.
- **Other Workers that the dashboard depends on:** `rival-evidence-collector` (signed evidence) and `rival-public-site` (read-only market feeds).
- **Permanent source:** this repository, `web/evidence-board/worker.js` and `web/evidence-board/collector.js`. Behavior and caveats are described in `web/evidence-board/README.md`.
- **Public fallback URL:** https://rival-evidence-board.staytrippynlw.workers.dev/
- **Deployment acceptance on November 15, 2026:** The live board source now matches tested GitHub `web/evidence-board/worker.js` exactly (241,513 bytes / 241,121 characters; Git blob `df1c54bd66c3e3f4e0b5e03d5e46c38d16e705c5`). Figures corrected 2026-11-17 to main `5d5c3bc`; the November 15 numbers described an older revision. The collector now accepts the exact main-branch `workflow_run` identity that the outdated deployment rejected, while retaining its OIDC signature, issuer, audience, repository and workflow restrictions and `EVIDENCE_KV` binding. [Previously failing run #43792927880](https://github.com/UglyGameFace/RivaL/actions/runs/43792927880) passed on rerun, and a new signed Cloudflare KV receipt arrived at `2026-11-16T02:46:06Z`. The site rendered at 390px mobile and 1280px desktop, and public API checks confirmed the expected successful routes and invalid-asset rejection. That November 15 check predates the Workers Builds setup: as of November 16 each Worker has a separate production main-branch push trigger (see [RIVAL_EVIDENCE_AUTODEPLOY.md](../../docs/RIVAL_EVIDENCE_AUTODEPLOY.md); Board build `cbd10209-5ba6-4518-81b5-619b8893d4b8` succeeded 2026-11-16T04:04:57Z, collector trigger configured but its first independent build unobserved). Future source changes still need source/binding parity verification after any auto-deploy. These are dated production checks, not guaranteed future uptime or model edge.
- **DNS/TLS:** Cloudflare Worker Custom Domain for `evidence.the420lobby.com`. Cloudflare owns the DNS record and certificate provisioning. Do not add a separate DNS record, or change the site's existing apex/WWW/other subdomain records.

## Required runtime contract

The `rival-evidence-board` Worker is presently a **classic Service Worker** (`addEventListener('fetch', ...)`), not an ES module. Keep the existing compatibility date (`2026-11-01`) and two service bindings:

| Binding | Destination | Purpose |
| --- | --- | --- |
| `RIVAL_EVIDENCE_RECEIPTS` | `rival-evidence-collector`, production | Read signed, sanitized host/DB/Drive/Intelligence evidence |
| `RIVAL_MARKET_FEED` | `rival-public-site`, production | Read allowlisted public exchange and prediction-market quotes |

The collector has its own `EVIDENCE_KV` KV namespace, GitHub OIDC trust policy, and trusted workflow `.github/workflows/evidence-board-receipt.yml`. Preserve those settings when restoring. **Never** copy secrets into the public Worker, public Git repository, or browser. Never replace the collector with a mocked success response.

One optional binding is allowed on `rival-evidence-board` only: the secret `ALERT_WEBHOOK_URL` (type `secret_text`), set once by the account owner in the Cloudflare dashboard (Worker → Settings → Variables and Secrets → Add → **Secret**). The deploy contract in `tools/deploy_evidence_worker.cjs` preserves it once set and never requires it; a plain-text variable of the same name is refused, because the webhook URL is a credential. The board POSTs warning/critical alerts to it from `/api/status`, with the send registered via `waitUntil` so delivery completes before the worker may terminate. Never commit the URL to this repository.

## Recovery without ChatGPT

1. Sign in to Cloudflare using the account that owns `the420lobby.com`; go to Workers & Pages and inspect `rival-evidence-board` and the custom domain. Cloudflare must show the Worker and its two service bindings.
2. Retrieve the current tested source from this GitHub repository. Inspect the current branch's CI and compare the deployed Worker script to the intended commit. If they differ, choose the tested version, preserving the deployed version as rollback.
3. Deploy the **classic** Worker using Cloudflare's own tools/API, preserving the Worker name, compatibility date, and both existing bindings. Avoid converting to a module, changing the collector, or resetting KV just to republish the UI.
4. Verify HTTPS and GET `/`, `/health`, `/api/status`, `/api/market/perps`, and `/api/market/predictions?asset=BTC`; `/api/market/predictions?asset=DOGE` must be rejected. The `/health` endpoint is **Worker liveness only**. `/api/status` may correctly return **503** for stale or missing signed evidence; do not hide that failure.
5. Check that the dashboard's Command, Markets, Signals, Models, XMarket, Intelligence, and Diagnostics views render on Android. Signed production snapshots have their own capture times and must never be labeled as streaming or a verified profitable edge.

## Important independence boundary

Losing a ChatGPT membership does **not** delete Cloudflare Workers, a custom domain, or GitHub repository code. Those services have their own accounts, billing, limits, and credentials. However, the dashboard is only as fresh as its external sources: its signed GitHub OIDC publisher, private Discloud access, PostgreSQL and Drive evidence, and Cloudflare KV must continue running independently. A stale receipt means stale evidence, **not** necessarily a failed website.

The separate ChatGPT-hosted *RivaL Intelligence Health* workspace is **not** this Evidence Board. It requires its own migration if its source exists only inside ChatGPT Sites.

## Unattended publication reliability

The trusted main-branch publisher attempts a ten-minute GitHub Actions schedule, but scheduled runs are best-effort and have been observed to skip. Successful main pushes, production-release verification and the independently scheduled hourly Discloud uptime workflow provide additional publication opportunities. Every fallback uses the evidence workflow's own OIDC identity and re-reads the host, database, and Drive, without inheriting any other workflow's artifacts or access. A recent signed publication was independently confirmed after the deployed collector authorization mismatch was repaired. Remaining GitHub schedule gaps must still be displayed as stale or unavailable. The site can be owned and recovered in Cloudflare and GitHub without a ChatGPT membership.
