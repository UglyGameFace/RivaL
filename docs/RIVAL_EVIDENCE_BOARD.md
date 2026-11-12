# RivaL Evidence Board: source receipts

This connector publishes narrowly sanitized production observations to the separate Cloudflare evidence board. The public dashboard never receives a Discloud API token, PostgreSQL URL/password, Google OAuth credential, private model file IDs, or raw model weights.

## Three read-only sources
- Discloud: the existing authenticated console action executes the already-shipped read-only host receipt. It proves freshness of forecast and math/review cycles at the capture time, not future uptime or deployed SHA.
- PostgreSQL: the host receipt uses RivaL's actual private Discloud PostgreSQL backend. Weight SHA changes, imports, issued shadow rows and paper scopes are read, not trained or changed. Weight updates do NOT prove prediction edge.
- Google Drive: the existing GitHub workflow OAuth secrets read ONLY the configured RivaL reports folder, selecting the latest valid XMarket report per BTC/ETH/SOL/XRP x 5/15/60 scope and parsing its bounded JSON. No Drive writes, no user Drive root traversal. Report existence is not a fitted model, valid publication, host import or shadow issuance.

## Authentication
The GitHub schedule runs hourly on main, or via a manual dispatch. It requests a short-lived GitHub OIDC ID token for the fixed Cloudflare audience. The receiver checks GitHub's RSA signature, token expiry, repository, workflow file, branch, and event. Unauthenticated POST is rejected. A Cloudflare KV namespace stores only the allowlisted latest receipt and 90 days of per-run receipts. Public GET returns timestamped aggregates without credentials or private file contents.

## Correctness
- Missing data stays unknown; true zero stays zero. Never turn a skipped job or API error into evidence of a passed check.
- A fresh live feed is NOT matched historical overlap. An archive file is NOT a verified fit.
- An offline model artifact is NOT an imported host model. Shadow issuance is NOT promotion.
- No deployment, trade, repo mutation or database write is exposed by the public dashboard.
- Source data is timestamped and stale snapshots do not establish current health.
- The repository's deployment workflow currently runs after qualifying main CI. Even evidence-only commits can initiate a separate Discloud release. Review that impact before merging; the receipt collector itself has no production deployment step.

## Tests
Run: python -m unittest discover -s tests -p test_evidence_board_receipt.py

Before calling the connection continuous, merge the source PR with explicit deployment awareness, manually dispatch the evidence job, and verify an OIDC-accepted receipt with the same GitHub run ID on the dashboard. An unmerged PR and seeded snapshot do not establish an automatic future feed.

## GitHub reliability and refresh

The public dashboard does not call GitHub's anonymous REST API to build its primary PR/CI view. Anonymous GitHub requests can return HTTP 403 after rate limits, so retrying from a browser was not a meaningful repair. The evidence workflow uses the ephemeral built-in `GITHUB_TOKEN` with **read-only** `actions:read` and `pull-requests:read` permissions. It gathers a bounded, allowlisted snapshot of public RivaL PR merge records and recent Actions runs. No PR bodies, private Intelligence repository metadata, user emails, logs, or secrets are published.

The same signed GitHub Actions OIDC receipt is authenticated and scrubbed again by the Cloudflare collector, then atomically replaces the public GitHub snapshot cache. A failed source read does not erase the previous verified snapshot. The site shows the capture time and explicitly calls it a snapshot rather than live status. GitHub run links remain available for job logs and artifacts that require additional permissions; missing detailed logs are not treated as nonexistent artifacts.

Private RivaL Intelligence repository activity is **not** automatically exposed by the public dashboard. It requires a separate private-safe connector/privacy decision before claiming complete ecosystem coverage.
