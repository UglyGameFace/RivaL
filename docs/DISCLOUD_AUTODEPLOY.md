# RivaL: verified GitHub → Discloud production delivery

This repository has a **real deployment workflow**, not an assertion that a
GitHub merge automatically updates the running Discord bot.

## How a release works

1. A change is merged into `main`.
2. The existing `CI` workflow runs Ruff and the complete pytest suite,
   including the PostgreSQL 16-backed tests.
3. Only a **successful `push` CI run for `main` in this repository** can
   trigger `Deploy verified RivaL to Discloud`. Forks, pull requests and
   failed test runs cannot deploy.
4. The workflow checks that **both** secret values exist:
   - `DISCLOUD_TOKEN`: your private API token from Discloud.
   - `DISCLOUD_APP_ID`: the exact ID of the *existing* RivaL bot in Discloud.
5. If either is missing, CI completes but deployment is explicitly **skipped
   with a warning**. Do not mistake this for a running production release.
6. When both exist, the official `discloud/deploy-action@v1` commits the
   **same exact SHA tested by CI**, with app_id supplied to ensure this is an
   update of the intended bot, not an accidental new app.

**Important:** no `env` or `env_file` is passed to Discloud's action.
Its documented `env` input could overwrite the deployed `.env`, so the
workflow deliberately leaves the existing production secrets and database
connection in Discloud untouched. Only `main.py`, `discloud.config`,
`requirements.txt`, `pyproject.toml` and `src/**` are uploaded.

The action's success means Discloud accepted the deployment. It does
**not** prove that Discord logged in, commands synced, market vendors
returned live data, or forecasts are profitable.

## One-time setup from Android

1. Open your RivaL repository's
   [Actions Secrets and Variables](https://github.com/UglyGameFace/RivaL/settings/secrets/actions)
   page in the browser. In **Repository secrets**, add the two secret
   names above. Never put their values in chat, code, `.env.example`,
   logs or screenshots.
2. Obtain your account API token and the existing bot's exact application ID
   from your [Discloud dashboard](https://discloud.com/dashboard).
3. After secret setup, merge another validated change to `main` to start
   the automatic release. Existing history is not automatically replayed.
4. Open the repository's [Actions](https://github.com/UglyGameFace/RivaL/actions)
   list, find `Deploy verified RivaL to Discloud`, and confirm that the
   official deployment step **actually ran and succeeded**, not just
   `skipped`.
5. Confirm the Discloud bot startup log contains
   `RivaL operational storage initialized (backend=postgres)` and a
   successful global or development-guild command synchronization.
6. From Discord, use `/rival-target coin:BTC timeframe:5 minutes`, then
   confirm the private card changes to WAIT when its actual target candle
   closes or the underlying feed is no longer current. Test `/rival-stop`
   and verify the card visibly switches to **Target stopped**.
7. Restart the RivaL bot in Discloud and confirm the same target continues
   to refresh without another signup. Do not declare live production
   accepted until this restart/continuity check succeeds.

A valid model watch is **not** a real-money trading signal. The user must
separately validate forecast accuracy, costs, legal venue eligibility and
any future authorized execution before risking money.

If your Discloud dashboard is using its own GitHub integration instead
of this repository Action, disable one automatic deploy route to avoid
two competing deployments on the same SHA.
