# RivaL Intelligence

This is the **standalone private CPU LFM3 service**, not the RivaL Discord bot.

## Source of truth

Service source is mirrored one-way from [UglyGameFace/RivaL](https://github.com/UglyGameFace/RivaL), branch `main`, by its `sync-intelligence-repo.yml` workflow. `UPSTREAM_COMMIT` records the exact 40-character upstream commit. Only the allowlisted service and grounding policy files are mirrored; no model weights, local executables, credentials, database dumps, or Discord bot code are copied.

Do not edit mirrored files directly. Make service changes in `RivaL/services/rival-llm` (or the authoritative `RivaL/src/parlay_bot/perpetuals/evidence_policy.py`) and merge to `RivaL/main`. The upstream sync will push the new source to this repository's `main`.

## Release

GitHub Actions on **this repository's main branch** compiles the pinned llama.cpp CPU runtime, validates the exact Discloud upload files, and deploys to the existing **RivaL-Intelligence** Discloud application. It then queries the authenticated on-host `/v1/status` and requires the source SHA, LFM3 identity, readiness, and repair telemetry. A green code build without this live acceptance is **not** a verified production update.

Configure two repository Actions secrets:

- `DISCLOUD_TOKEN`: existing Discloud account token (never commit it).
- `DISCLOUD_LLM_APP_ID`: existing RivaL-Intelligence app ID, **not** the RivaL Discord bot's app ID.

Keep `RIVAL_LLM_SHARED_TOKEN` in the standalone Discloud application's private environment, not in GitHub or these deployment files. Discloud-hosted variables, models and storage must remain separate from the main bot.

**Do not enable a second Discloud GitHub auto-deploy for this source-only repository.** The repository intentionally does not commit CPU binaries or GGUF weights. Its own `release.yml` workflow compiles the CPU runtime, deploys the approved file set, and verifies the live host. Connecting Discloud's native raw-source deploy in parallel would race it and deploy a directory without the required prebuilt binary.

## Initial provisioning

The authoritative RivaL repository's sync workflow can create and populate this private repository with a credential having permission to create/write repositories. Set `RIVAL_INTELLIGENCE_REPO_TOKEN` in **RivaL** Actions secrets. This is a cross-repository publish credential only, not an LLM token. Use the narrowest available permissions. The publisher needs private-repository creation (if not already created), **Contents: read/write** and **Workflows: read/write** on the destination because it installs an Actions workflow file. A classic PAT requires `repo` and `workflow` scopes; a fine-grained token can be restricted to the existing target repository once it has been created.

The sync runs on relevant RivaL/main changes, hourly reconciliation and manually. It does not overwrite unrelated owner-managed downstream files. Only the upstream-owned manifest paths are replaced or removed.

## Acceptance boundaries

Passing Python/build validation proves the deployable package shape. A successful Discloud deploy step plus fresh authenticated SHA-matching status proves the matching service code is live and ready. Neither proves that intentional backend crash recovery has been exercised in production, that model weights have improved, or that historical market data is complete. Those require separate on-host fault tests and forward-outcome evidence.

The service is designed for private VLAN research only, with bounded CPU and a strictly authenticated request path.
