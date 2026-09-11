# How it works

## What a backup contains

A backup unit is the whole **stack folder**:

- the Compose file;
- **all** env files, regardless of name (`.env`, `config.env`, …);
- bind-mount data located inside the stack folder;
- **named volumes**, tarred **uncompressed** via a temporary `busybox` container
  (restic deduplicates and compresses; legacy `.tar.gz` archives still restore fine);
- **external bind mounts** whose source is outside the stack folder (e.g.
  `/srv/appdata:/data`). They are detected at `create` and included as
  `extra_backup_paths` — interactively after a prompt; system paths like
  `docker.sock` or `/etc/localtime` are skipped;
- **consistent database dumps**, staged in a temporary `.docker-backup/dumps/`
  directory. The raw DB data directories are **excluded** from the file backup.

Everything goes into one restic repository per stack (`<target base>/<name>`),
encrypted with a per-stack key.

## Stack introspection

The stack is read via `docker compose config --format json`, which resolves
`env_file` entries and variable substitution. That model is used to find services,
images, volumes and database credentials.

## Database detection

Databases are detected from the service image:

| Engine | Detected from | Captured as |
|---|---|---|
| MySQL / MariaDB | image `mysql` / `mariadb` | `mysqldump --single-transaction` / `mariadb-dump`, all non-system databases |
| Postgres | image `postgres` | `pg_dump` of the configured database |
| Supabase Postgres | image `supabase/postgres` | superuser dump incl. cluster globals and every database |
| MongoDB | image `mongo` | file copy, frozen via `fsyncLock` only during the file capture |
| Redis / Valkey / KeyDB | image | `BGSAVE` checkpoint, then file copy |
| Others (Elasticsearch, ClickHouse, …) | flagged at `create` | live file copy only (crash-consistent) |

Credentials, scope rules and per-stack overrides: [Databases](databases.md).

## A backup run, step by step

`docker-backup run <name>` (what the timer calls) does the following:

1. **Validate.** Hooks must be approved — unapproved hooks abort the run, they are
   never silently skipped. A legacy ambiguous MySQL scope fails here as well,
   before anything is written.
2. **Stage.** A temporary `.docker-backup/` directory (`0700`, dump files `0600`)
   is created inside the stack folder.
3. **Pre-backup hooks** run (e.g. `gitlab-backup create`), before any restic call.
4. **Database dumps** are written to `.docker-backup/dumps/`. A stopped DB service
   is started temporarily for the dump.
5. **Quiesce and capture.** MongoDB is frozen and Redis checkpointed; named volumes
   are tarred into `.docker-backup/volumes/`. Mongo's lock is held only as long as
   its data is being read: for a named volume only during the tar step, for a bind
   mount until restic has read it.
6. **restic backup.** Stale locks are cleared first (`restic unlock` removes only
   locks of dead processes), then the stack folder, extra paths and the staged data
   are uploaded.
7. **Retention.** `restic forget --prune` with the stack's retention policy.
8. **Manifest.** A small, non-secret manifest (`<repo>/docker-backup.manifest.json`)
   is written next to the repo and bound to the new snapshot ID, so another server
   can [restore straight from the drive](restore.md#restore-on-another-server).
9. **Offsite copy.** If configured, `restic copy` to the offsite repo, followed by
   its own prune (unless `--no-offsite-prune`).
10. **Post-backup hooks** run — **always**, even if the backup failed.
11. **Clean up.** The staging directory is removed — also when the run fails, so
    plaintext DB dumps never stay on disk.
12. **Record.** The result goes to `/etc/docker-backup/status/<name>.json`; on
    failure systemd's `OnFailure` hook sends the [notification](notifications.md).

## Excludes

Exclude patterns (`--exclude`, also set by templates) are relative to the stack
folder. A pattern **with** a `/` (e.g. `app/logs`) is anchored to the stack root, so
it only matches `<stack>/app/logs`. A bare name **without** a `/` (e.g. `*.log`)
matches that basename anywhere in the tree. The raw data directory of every
detected SQL database is excluded automatically.

## Self-healing locks

Stale restic locks (crash or reboot mid-run) are cleared before every backup and
check run with `restic unlock`. Only locks of dead processes are removed; live locks
stay.
