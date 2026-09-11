# Databases

Databases are **dumped consistently** instead of copied raw. Detection happens at
`create` from the service image; credentials come from the resolved Compose
environment. Re-run detection later with
`docker-backup set <name> --refresh-db-detection`.

## MySQL / MariaDB

- **Dump:** `mysqldump --single-transaction` / `mariadb-dump`. The raw data
  directory is excluded from the file backup.
- **Credentials:** from the resolved environment. With
  `MYSQL_RANDOM_ROOT_PASSWORD=yes`, the app user is used instead. A real root
  password is still preferred for reliable access.
- **Scope:** immediately before every dump, all visible non-system databases are
  enumerated; a declared `MYSQL_DATABASE`/`MARIADB_DATABASE` must be among them.
  This includes additional user databases created later, but excludes the
  image-owned `mysql`, `sys`, `ndbinfo`, `performance_schema` and
  `information_schema` databases. The exact list is recorded in the snapshot
  manifest.
- With a non-root dump user, enumeration is privilege-dependent; if a visible user
  database cannot be dumped, the whole backup fails instead of silently omitting it.

This portable scope deliberately does **not** copy accounts/grants from the `mysql`
system database; the fresh container recreates the users declared by Compose. If
the DB container has additional manually managed users or grants, record and
recreate those separately and verify them in the restore drill.

### Refreshing legacy MySQL/MariaDB scope

Configs created by versions up to 1.0.3 may contain `all_databases=true` when a
root password was available, even though Compose declares one application
database. Such a dump includes MariaDB's `mysql` system database and can fail when
imported into a fresh container. Legacy app-user configs can have the opposite
problem: their static list does not include additional databases created later.
After upgrading, every auto-detected legacy MySQL/MariaDB plan must therefore be
reviewed and refreshed explicitly:

```bash
sudo docker-backup --dry-run set snipeit --refresh-db-detection
sudo docker-backup set snipeit --refresh-db-detection
sudo docker-backup run snipeit
```

The refresh changes only the detected DB/volume plan; repo, key, schedule,
retention, offsite settings, hooks, template and user exclude patterns are
preserved. Existing dump-user and Postgres-globals overrides are preserved too and
are shown in the dry-run diff. A legacy ambiguous config fails before hooks or dump
staging and points to this command instead of being changed silently.

The corrected scope applies only to **new** snapshots: existing all-database
snapshots are not rewritten, so create a fresh snapshot and test that exact
snapshot during the restore drill.

!!! info "Rolling back to 1.0.3"
    The stored dynamic MySQL scope is deliberately backward-safe. Current versions
    resolve it to the exact non-system database list before each dump. A refreshed
    JSON config can therefore intentionally contain both
    `database_scope="non-system"` and `all_databases=true`; the marker controls
    current versions and the boolean is only the safe legacy fallback. If the
    program is rolled back to v1.0.3, that older version ignores the marker and
    falls back to `--all-databases`: this may reintroduce the old system-database
    restore conflict, but it does not silently omit additional user databases.
    Refresh and create a new snapshot again after returning to the current version.

## Postgres

- **Dump:** `pg_dump` of the configured database with `--no-owner --no-privileges`;
  no cluster globals. The raw data directory is excluded from the file backup.
- **Overrides** for any Postgres stack: `--dump-user <role>` and
  `--dump-globals`/`--no-dump-globals`, at `create` or later via `set`:

```bash
sudo docker-backup create /opt/foo --target /mnt/backups --dump-user supabase_admin --dump-globals
sudo docker-backup set foo --dump-user supabase_admin --dump-globals
```

On restore, each database is created if missing and imported **atomically**
(`psql --single-transaction -v ON_ERROR_STOP=1`), so a failed import rolls back
instead of leaving a half-restored database.

## Supabase and multi-database Postgres

A `supabase/postgres` image is recognized automatically and changes the Postgres
dump in three ways needed for a faithful Supabase backup:

1. **Role:** dumps as the superuser **`supabase_admin`** (the unprivileged
   `postgres` role can hit *permission denied* on internal schemas like
   `pgsodium`/`vault`).
2. **Globals:** `pg_dumpall --globals-only` captures **all roles and their
   passwords** (a single-database `pg_dump` cannot). Written to
   `<service>.globals.sql`.
3. **All databases + ownership:** dumps **both** `postgres` **and** `_supabase`
   (Logflare analytics / Supavisor pooler) to `<service>.<db>.sql`, **keeping**
   `OWNER`/`GRANT` (so e.g. the `auth` schema stays owned by `supabase_auth_admin`).

```bash
# Supabase is auto-detected — nothing special needed:
sudo docker-backup create /opt/supabase-production --target /mnt/backups
```

On **restore** the globals are imported first (existing-role errors from the
image's init scripts are tolerated, so `ALTER ROLE … PASSWORD` still applies — any
*other* error is surfaced as a warning, not silently swallowed), then each database
is created if missing and imported atomically.

!!! note "Restore expects a fresh target"
    The DB data dir is excluded from the file backup (the logical dump replaces
    it), so restore brings the DB up on a freshly initialized data dir — the
    `supabase/postgres` image's init scripts run, then the ownership-preserving
    `pg_dump --clean --if-exists` is applied on top. On a fresh target the
    `--clean` DROPs collide with Supabase's own bootstrap objects (inherited
    `realtime.messages_*` partition keys, the `extensions` schema and its
    `grant_pg_*_access()` functions); those failed DROPs are **non-destructive and
    are tolerated** — the restore completes instead of falsely reporting an
    incomplete DB. A failed `CREATE`/`COPY`, by contrast, still aborts the restore.
    Works for a same-version restore; always **test-restore into a throwaway dir**
    before relying on it.

!!! warning "Not covered"
    If Storage uses an **external S3 backend** (`STORAGE_BACKEND=s3`), the uploaded
    file **objects live in that bucket, not in the stack** — Postgres holds only
    the `storage.objects` metadata. Back up that bucket separately. Also keep the
    **`db-config` named volume** (pgsodium/Vault key) — it is captured by default,
    and is required to decrypt Vault/encrypted columns after a restore.

## MongoDB and Redis (built-in quiesce)

MongoDB and Redis/Valkey/KeyDB are detected at `create` like the SQL engines, but
captured as a **file copy with a consistency guard** instead of a dump:

- **MongoDB** is frozen via `db.adminCommand({fsync:1,lock:true})` **only around the
  file capture** — for a named volume only during the tar step, before the restic
  upload; reads keep working — and released in a `finally`. A failed lock aborts
  the run (an unlocked WiredTiger copy may be unrestorable); a failed unlock fails
  it loudly (manual `db.fsyncUnlock()` needed).
- **Redis/Valkey/KeyDB** get a fresh RDB checkpoint via `BGSAVE` first.

Auth is picked up from the usual env vars (`MONGO_INITDB_ROOT_*`, bitnami
`MONGODB_ROOT_PASSWORD`, `REDIS_PASSWORD`). Opt out with `create --no-quiesce` or
`set <name> --no-quiesce`.

## Other stateful services

Services without dump support (Elasticsearch, ClickHouse, …) are flagged at
`create`: they are captured as a live file copy only (crash-consistent). Add a
[pre-backup hook](hooks.md) that produces an application dump for guaranteed
consistency. Embedded databases (SQLite in n8n, Uptime Kuma, Headscale, …) are
captured as files as well; "No database detected" at `create` is expected there.

## Skipping detection

`--no-db-detect` (or a template with `db_autodetect: false`) skips DB
auto-detection entirely, for apps that dump their own database — see
[Templates & hooks](hooks.md).
