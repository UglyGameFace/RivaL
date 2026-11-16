# RivaL Evidence Board automatic deployment

## Existing Cloudflare deployment targets

Only these two production Workers are in scope:

- rival-evidence-board (classic Service Worker): https://evidence.the420lobby.com/
- rival-evidence-collector (ES module): signed read-only evidence, existing Cloudflare KV

Never deploy or alter rival-public-site through this integration.

## GitHub repository connection

On November 15, 2026, the Cloudflare Builds repository connection was registered:

- GitHub repository: UglyGameFace/RivaL, main branch
- Connection UUID: e740b96f-7f52-4e70-8ff0-332d723345e1
- Evidence Board Worker tag: 53cc27fe9f904e379630e1288532ba5b
- Evidence Collector Worker tag: 69169eab36d34fdda4430ba8cda9b94b

Registration alone is not deployment authorization. Cloudflare's GitHub App must have access to this repository, and Workers Builds needs a user-scoped deployment API token. No build tokens or Workers Builds triggers existed when this integration was prepared. The connected OAuth account cannot create or read user API tokens. Never invent a token or put one into the Git repository or ChatGPT conversation.

To authorize once, open Cloudflare Workers & Pages, select a Worker, open Settings → Builds → Connect, choose GitHub, authorize the Cloudflare GitHub App if prompted, and create/select a user API token inside Workers Builds. A browser authorization must be completed by the Cloudflare account owner.

## Secure production build settings

Connect both Workers separately to the same repository and main production branch, with Previews disabled. Use the repo root as the build directory. Set CLOUDFLARE_ACCOUNT_ID as a non-secret build variable; Workers Builds must make the configured deployment token available as CLOUDFLARE_API_TOKEN at runtime.

### Evidence Board

Build command:

    node tools/test_evidence_board_ui.js && node tools/test_rival_operations_api.js && node tools/test_evidence_worker_deploy.js && node tools/deploy_evidence_worker.cjs board --check

Deploy command:

    node tools/deploy_evidence_worker.cjs board

Path filters: web/evidence-board/worker.js; tools/deploy_evidence_worker.cjs; tools/test_evidence_worker_deploy.js; tools/test_evidence_board_ui.js; tools/test_rival_operations_api.js.

### Evidence Collector

Build command:

    node tools/test_evidence_collector.js && node tools/test_evidence_worker_deploy.js && node tools/deploy_evidence_worker.cjs collector --check

Deploy command:

    node tools/deploy_evidence_worker.cjs collector

Path filters: web/evidence-board/collector.js; tools/deploy_evidence_worker.cjs; tools/test_evidence_worker_deploy.js; tools/test_evidence_collector.js.

### Why an explicit deployment script?

This repository contains two different Worker formats and an existing production dependency graph. A generic automatic Wrangler setup may guess the wrong entrypoint and overwrite service or KV bindings. The source-controlled uploader uses Cloudflare's documented upload API, validates the current Worker name, compatibility date, expected bindings, and source, then verifies deployed code and bindings after upload. Any unexpected remote change fails closed. It does not create KV namespaces, change DNS, reset service bindings, train models, or place trades.

## Evidence required before marking automatic deployment complete

- Cloudflare Builds must show a real main-branch build triggered by a commit changing the corresponding source, with tests passing and no unexpected simultaneous deployment of the other Worker.
- Confirm the deployed source matches GitHub and that existing service bindings, KV namespace, compatibility dates and custom domain were preserved.
- Check the permanent site and its read-only API routes: /health, /api/status, /api/market/perps, /api/market/predictions?asset=BTC. Invalid asset DOGE must still produce HTTP 400.
- Independently verify a new signed GitHub OIDC receipt was accepted and retained in KV.
- Preserve the previously deployed Worker version as rollback; a Worker code rollback does not revert KV data.

This document and its deploy script prepare a safe automatic pipeline. The pipeline remains **unverified** until an authorized build token, active trigger, successful GitHub-driven production build, and source parity check are observed.
