# Discloud PostgreSQL for RivaL

RivaL uses PostgreSQL as its production operational database. Google Drive remains the verified cold
warehouse for archived historical line states.

## Production boundary

The PostgreSQL database stores current odds, line-state history, saved user jurisdiction, refresh
state, market catalog metadata, and cold-archive bookkeeping. All of those features share the same
runtime store.

Do not configure a second production SQLite database alongside PostgreSQL.

## Discloud setup

1. Create a PostgreSQL database for RivaL from Discloud's database/template area.
2. Keep the database and the RivaL bot on the same private Discloud VLAN.
3. Give the database a private hostname that is unique within that VLAN.
4. Copy the PostgreSQL connection URL supplied by the database deployment into RivaL's deployment
   environment as `RIVAL_DATABASE_URL`.
5. Keep `RIVAL_STORAGE_BACKEND=postgres`.
6. Redeploy RivaL. The bot creates its required tables and indexes idempotently at startup.

The repository's `discloud.config` enables VLAN participation for the bot. Database credentials and
the connection URL belong only in deployment environment/secret storage.

## Required environment

```dotenv
RIVAL_STORAGE_BACKEND=postgres
RIVAL_DATABASE_URL=postgresql://USER:PASSWORD@PRIVATE_HOST:5432/DATABASE
```

The value above is only a shape example. Use the actual credentials, host, port, and database name
issued for the RivaL PostgreSQL deployment.

## Failure behavior

If production storage is set to `postgres` and `RIVAL_DATABASE_URL` is missing, RivaL fails
startup with an explicit configuration error. It does not fall back to a local SQLite file.

## Local tests

SQLite remains available for local or isolated tests:

```dotenv
RIVAL_STORAGE_BACKEND=sqlite
RIVAL_DB_PATH=data/rival.sqlite
```

Do not use that local compatibility mode as the Discloud production database.

## Existing SQLite data

RivaL does not automatically copy an old SQLite database into PostgreSQL. Automatic migration would
be unsafe when the source file's age, completeness, or deployment provenance is unknown.

If a deployed SQLite file contains production data that must be preserved, inspect and back it up
first, then perform a one-time controlled import before enabling PostgreSQL as the production source
of truth.
