# Discloud PostgreSQL startup: DNS resolution failure

## Observed incident (November 1, 2026)
Production startup stopped in `self.store.initialize()` with
`psycopg.OperationalError: failed to resolve host 'uglygameface138'`.
This is a database endpoint/private network resolution failure, not a
Lighter candle exception, memory threshold, Discord voice dependency,
authentication error, or evidence of missing historical data.

## Recovery without losing learned data
1. In the Discloud Dashboard, open the **existing PostgreSQL** app/template
   associated with RivaL. Confirm it is **Online** and record its **actual**
   internal service hostname, database name and port from the authorized
   connection details. Do not create a new blank database.
2. Confirm the PostgreSQL app and RivaL bot are in compatible Discloud
   private VLAN networking. The bot's checked-in `discloud.config` already
   includes `VLAN=true`, which does not prove the database service is
   reachable. Confirm the database app's private networking and identity.
3. Open **RivaL** in Discloud and inspect the protected
   `RIVAL_DATABASE_URL` variable. Correct its URL's hostname **only if**
   it differs from the live database's documented hostname. Preserve the
   existing user, password, database name and SSL settings. If Discloud
   provided a replacement full connection URL, use that authoritative URL.
   Never commit or paste full connection URLs or credentials into GitHub,
   logs or chat.
4. Restart RivaL on Discloud after correcting its variable. Confirm logs
   contain `RivaL operational storage initialized (backend=postgres)`,
   Discord `READY`, and subsequent fresh market collection/learning and
   ordinary persisted records. Confirm the same production data survived.
5. If the hostname matches yet still cannot resolve, investigate the
   PostgreSQL container's actual online state and private DNS/VLAN scope
   with Discloud support; do not guess a public IP or bypass isolation.

## Code safeguards
RivaL now retries **only** DNS lookup failures at PostgreSQL startup over
four short pauses (2, 4, 8, 12 seconds). A recovered resolver connects to
the **same** protected PostgreSQL DSN; no provider switch, credentials
rewrite, SQLite downgrade, schema deletion, or historical reset occurs.
A persistent failure exits with a credential-safe explanation so an
infrastructure problem is never reported as healthy. Auth failures,
connection refusal and SQL faults are not retried by this DNS-specific
path. The API 200 Discloud deployment receipt proves a package upload,
not live Discord/DB health.

Do not add PyNaCl or davey just to silence voice warnings; neither causes
this database startup failure.
