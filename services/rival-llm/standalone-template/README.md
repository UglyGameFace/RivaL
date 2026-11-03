# RivaL Intelligence

This is the **standalone private CPU LFM3 service**, not the RivaL Discord bot.

## Source of truth

Service source is mirrored one-way from [UglyGameFace/RivaL](https://github.com/UglyGameFace/RivaL), branch `main`, by its `sync-intelligence-repo.yml` workflow. `UPSTREAM_COMMIT` records the exact 40-character upstream commit. Only the allowlisted service and grounding policy files are mirrored; no model weights, local executables, credentials, database dumps, or Discord bot code are copied.

Do not edit mirrored files directly. Make service changes in `RivaL/services/rival-llm` (or the authoritative `RivaL/src/parlay_bot/perpetuals/evidence_policy.py`) and merge to `RivaL/main`. The upstream sync will push the new source to this repository's `main`.

## Release

GitHub Actions on **this repository's main branch** compiles the pinned llama.cpp CPU runtime, validates the exact Discloud upload files, and deploys to the existing **RivaL-Intelligence** Discloud application. It then checks the authenticated on-host `/v1/status` for the exact source SHA, LFM3 identity, readiness, and repair telemetry. A release makes two synthetic, read-only requests: `/v1/research` for safe grounded explanations and `/v1/hypothesis` for a fixed-feature experimental research proposal. Neither sees real source history, places trades, writes a database, or generates a forecast probability. The response must be safely grounded, informational-only and explicitly uncertain; the private status route must reject anonymous access. These checks are mandatory for a green release.

Configure two repository Actions secrets:

- `DISCLOUD_TOKEN`: existing Discloud account token (never commit it).
- `DISCLOUD_LLM_APP_ID`: existing RivaL-Intelligence app ID, **not** the RivaL Discord bot's app ID.

Keep `RIVAL_LLM_SHARED_TOKEN` in the standalone Discloud application's private environment, not in GitHub or these deployment files. Discloud-hosted variables, models and storage must remain separate from the main bot.

**Do not enable a second Discloud GitHub auto-deploy for this source-only repository.** The repository intentionally does not commit CPU binaries or GGUF weights. Its own `release.yml` workflow compiles the CPU runtime, deploys the approved file set, and verifies the live host. Connecting Discloud's native raw-source deploy in parallel would race it and deploy a directory without the required prebuilt binary.

## Initial provisioning

The authoritative RivaL repository's sync workflow can create and populate this private repository with a credential having permission to create/write repositories. Set `RIVAL_INTELLIGENCE_REPO_TOKEN` in **RivaL** Actions secrets. This is a cross-repository publish credential only, not an LLM token. Use the narrowest available permissions. The publisher needs private-repository creation (if not already created), **Contents: read/write** and **Workflows: read/write** on the destination because it installs an Actions workflow file. A classic PAT requires `repo` and `workflow` scopes; a fine-grained token can be restricted to the existing target repository once it has been created.

The sync runs on relevant RivaL/main changes, hourly reconciliation and manually. It does not overwrite unrelated owner-managed downstream files. Only the upstream-owned manifest paths are replaced or removed.

## Acceptance boundaries

Passing Python/build validation proves the deployable package shape. A successful Discloud deploy step, fresh authenticated SHA-matching status, and one safely grounded synthetic inference prove that the matching service code is live, ready, and can complete a bounded private request. Neither proves that intentional backend crash recovery has been exercised in production, that model weights have improved, or that historical market data is complete. Those require separate on-host fault tests and forward-outcome evidence.

The service is designed for private VLAN research only, with bounded CPU and a strictly authenticated request path.

## Bounded feature hypothesis research

The private endpoint `POST /v1/hypothesis` uses the existing LFM3 CPU service,
authenticated shared bearer token and single busy semaphore. The only accepted
hypotheses are `trend_only`, `momentum_only`, `volatility_only`, and
`volume_only`. Output is a short falsifiable research proposal with no price,
probability, trading order, trained weight, promotion or claimed accuracy.

On the main RivaL bot, an optional once-per-six-hour UTC feature laboratory
rotates BTC, ETH, SOL and XRP. It requires `RIVAL_AI_PROVIDER=local`,
`RIVAL_LOCAL_AI_TOKEN` matching the LFM3 shared token, and the existing
`RIVAL_LLM_HYPOTHESES_ENABLED` setting (enabled by default). Its calls share the
existing persisted daily AI budget and hourly cooldown. Proposals are stored
in PostgreSQL with source-evidence and record SHA-256, not in forecast tables.

Source-native GitHub research, when authenticated to the bot's existing Discloud
application, reads only verified proposals and tests predeclared feature masks
on the original exchange-specific **training and validation** partitions.
The existing models are evaluated independently. The proposal-derived trials
never read the sealed holdout, enter the model import folder, or promote.
Fresh prospective comparisons and actual after-cost tests remain necessary.
