# RivaL production delivery: Discloud native GitHub Auto Deploy

## Single deployment owner

**Discloud's built-in GitHub Auto Deploy on the existing `RivaL` app, branch
`main`, is the only production deployer.** The owner confirmed this connection
in the Discloud dashboard. A merge to `main` can cause Discloud to update and
restart the bot directly, independent of GitHub Actions.

`.github/workflows/discloud-deploy.yml` retains the name
**RivaL Production Release**, but **no longer uploads, updates, restarts, or
replaces anything on Discloud**. It is a read-only release *observer*, triggered
by successful first-party main CI. This prevents the previous competing
GitHub deployment action from causing additional restarts.

**CI is not a deployment gate for native Discloud Auto Deploy.** A merged commit
can be picked up by Discloud before CI passes. Review and validate changes
*before merging*, and do not interpret a green CI run or a green observer
workflow without its parity step as confirmation of a production deploy.

## What the release observer actually verifies

1. A successful CI run belongs to first-party `main` (push or manual CI)
   and its tested SHA is still the current `main` SHA.
2. The observer checks out that exact tested SHA.
3. Using GitHub-held `DISCLOUD_TOKEN` and `DISCLOUD_APP_ID` *only for a
   read-only source check*, the observer calls the existing Discloud console
   API with a fixed Python command.
4. The console computes a deterministic SHA-256 fingerprint of `main.py`,
   `requirements.txt`, `pyproject.toml`, `discloud.config` and all deployed
   `src/**/*.py`. The observer compares it with the same files from the
   exact CI checkout, retrying briefly while the native deployment arrives.
5. Only a successful `Verify native Discloud source parity` step counts as
   verified file parity. Missing credentials, inaccessible console, unexpected
   files, malformed output, and unequal hashes **fail closed**.

Source parity proves selected files **on disk**, not the running Python process
has reloaded them, Discord is connected, PostgreSQL can be read, five XMarket
models were imported, shadows are issuing, or any predicted edge is profitable.
The separate LFM3 persisted-research acceptance workflow also gates its host
probe on this verified parity step, not on a GitHub upload.\n\nThe independently authenticated host learning receipt remains responsible for
actual PostgreSQL/worker observations, and downstream `workflow_run` events
check the observer's verified parity step rather than a nonexistent upload.

Scheduled/manual receipt checks are still allowed without waiting for a
release. Those observations cannot by themselves establish the deployed SHA.

## Owner operation (Android-friendly)

- In Discloud, leave the existing app's GitHub Auto Deploy connected to
  `UglyGameFace/RivaL` → `main`. **Do not add another deploy action.**
- In GitHub, review the main CI and the RivaL Production Release workflow.
  The last workflow must show the **source parity step succeeded**, not merely
  a green run with its verification skipped.
- Read the separate RivaL live learning cycle receipt for the actual host
  forecast, learning, XMarket import and shadow counts. A successful
  deployment/CI does not imply those stages are healthy.
- For a production incident, first use the read-only Discloud logs or
  the independent hourly uptime workflow. Do not restart the bot just to
  make an evidence receipt appear.

Sentry's legacy `RELEASE_SHA` file was previously stamped only by the retired
GitHub upload action. Native deployment does not recreate that file. To avoid
attributing current errors to an old commit, runtime Sentry tagging now uses
an explicitly configured hexadecimal `SENTRY_RELEASE` when present, otherwise\na deterministic `src-<fingerprint>` tag derived from shipped source files.
That tag is **not** a GitHub commit SHA.\n\nThe existing Discloud app environment, PostgreSQL credentials, private Drive
configuration, and native GitHub settings are never modified by this observer.
No real-money orders, migrations, training, or model-gate changes occur here.
