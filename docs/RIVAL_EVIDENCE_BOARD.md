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

## Immediate first run and RivaL Intelligence

The evidence job has a path-scoped push trigger on main for its own workflow and collector files. This permits an immediate signed receipt after a verified merge, alongside the scheduled hourly run and manual dispatch. The receiver authenticates GitHub OIDC signature, expected repository, exact workflow, main ref, event and token expiry. A fork or PR identity cannot publish.

RivaL Intelligence's repository is presently public. The existing ephemeral GitHub token reads a bounded public commit/PR/CI snapshot, not private inference requests, model weights, logs or secrets. The optional Discloud status API GET reads the separate Intelligence container state using existing protected secrets. An online container does not prove model inference quality, deployment SHA, or economic edge.

## Real paired model score evidence

The optional read-only paired-score collector reads the existing settled perp_forward_audit table over the last 24 hours, capped at 500 latest forecast rows per asset. It never initializes tables, predicts, trades, writes, or changes model selection. It scores only the existing prospective-logistic-persistent-v3 incumbent against its baseline on exact paired settled rows with issuance preceding the target, deduplicated per venue/contract/quote/horizon/target. Model and baseline Brier scores (lower is better) and their sample counts are shown with source timestamps.

These are bounded observational scores, not comprehensive family rankings, execution costs, statistical significance or guaranteed profits. All previous model promotion gates remain unchanged. A performance source without eligible settled rows is explicitly unavailable, never zero. The host score module must reach the deployed bot before the publisher can obtain that optional evidence. The first publish may therefore capture GitHub/Drive/DB status but no score. Diagnose that separately.

Signed receipts are saved by run ID and a bounded 72-entry evidence timeline. Charts only connect matched asset/horizon Brier observations across at least two signed snapshots. They never graph fabricated performance or combine unrelated scopes to manufacture a trend.

## Signed-publication transport diagnosis (November 13)

The real signed push jobs [43328686671](https://github.com/UglyGameFace/RivaL/actions/runs/43328686671) and [43330102811](https://github.com/UglyGameFace/RivaL/actions/runs/43330102811) failed at Cloudflare publication, the latter with an **HTTP 403** but no allowlisted receiver JSON error label. This is **not** evidence that the sender's OIDC signature is invalid: the response could have come from Cloudflare upstream before the Worker ran. The existing Worker identity verifier must remain unchanged unless a real claim or signature mismatch is demonstrated.

The Evidence Board workflow now first makes two anonymous, non-mutating checks from the **same GitHub runner** that will later submit the signed receipt:

1. `GET /health` must return JSON identifying `rival-evidence-collector`, not a generic challenge page.
2. `POST /ingest` without Authorization must return the Worker's own `401 authorization_required`. An open anonymous endpoint is a hard failure.

A failed check stops before requesting GitHub OIDC or reading Discloud/Drive evidence. The signed POST uses a fixed application User-Agent and Accept type. A denied POST logs only an HTTP status plus one of the sender's strictly bounded receiver classifications. A 403 without application JSON is marked `non_json_upstream` rather than misreported as an invalid signed token. A JSON verifier denial can report its allowlisted reason. The sender additionally checks *claim equality only* and reports field names on mismatch, not the JWT, its contents, any credential, provider response, or provider URL. **This local check is diagnostic, not cryptographic verification**; Cloudflare still verifies RSA signature, issuer, branch, repo, workflow, nonce/time and expiration.

The first post-merge scoped workflow run is the acceptance check. Success requires a signed `accepted` or `stale_ignored` response and an appropriately dated, run-ID-linked public KV receipt, not merely a passing Python test. If an upstream block remains, fix the actual Cloudflare route/access cause next, not by weakening the GitHub OIDC contract or seeding a fake verified receipt.
