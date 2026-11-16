# RivaL Evidence Board automatic deployment

## Verified connection status (November 16, 2026)

The user-owned Cloudflare Builds deployment token `RivaL Evidence Board Auto Deploy` is now configured; both Workers are linked to GitHub `UglyGameFace/RivaL` `main` with automatic builds enabled, source-specific build watch paths, no previews, and caching off. **Query Cloudflare Builds by immutable Worker tags, not Worker names.** The Board trigger is `67b22119-f38c-4bed-8ccc-f52ed44fc552`, collector trigger `6f07936c-4634-45eb-bac2-89cf209d0a2c`.

The Board's GitHub push build `cbd10209-5ba6-4518-81b5-619b8893d4b8` completed successfully at 2026-11-16T04:04:57Z. It passed the source-backed Node UI and API tests, confirmed the fail-closed deploy contract, published source, and verified production bindings. Cloudflare's `status: stopped` represents completion; inspect `build_outcome: success` and build logs, not status alone. The collector's trigger was confirmed configured but no completed collector build had yet been observed, so its automatic deployment still needs an independent real push acceptance check. The separate signed OIDC receipt publisher has been operational and remains independent of Builds.


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

Registration alone is not deployment authorization. Cloudflare's GitHub App must have access to this repository, and Workers Builds needs a user-scoped deployment API token. At original preparation time, tokens and triggers were absent. The configuration and production build status above supersede that initial state. The connected session could not mint user API tokens, but the account owner has since created the authorized Builds token. Never invent a token or put one into the Git repository or ChatGPT conversation.

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

The Board pipeline has now passed a genuine source-change GitHub-driven production build with its token, trigger, tests and binding/source verification. The Collector pipeline has a trigger but no separately observed completed source-change build yet. Future pushes must still be validated against source and binding parity; a green build does not prove bot model deployment or profitability.
