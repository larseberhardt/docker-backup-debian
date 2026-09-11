# Templates & hooks

Some apps don't fit the "detect the DB → `pg_dump`/`mysqldump`" model — they bring
their own consistent backup command and store large, regenerable live data you
don't want in the archive. **GitLab** is the canonical case: `gitlab-backup create`
produces the consistent dump, the Gitaly/Postgres/Redis/registry live dirs should
be excluded, and restore is its own sequence. Three building blocks cover this:

1. **Hooks** — shell commands that run **before** the backup (`pre_backup`),
   **after** it (`post_backup`, always — even on failure, for cleanup), and as a
   custom **restore** command instead of the built-in DB import.
2. **User excludes** (`--exclude`) — extra paths/globs, relative to the stack folder.
3. **`--no-db-detect`** — skip DB auto-detection entirely (the app dumps its own DB).

An **app template** bundles these settings for a known application, so
`create --from-template <name>` gives you a reviewed starting point.

## Shipped templates

```bash
sudo docker-backup templates list          # or: create --list-templates
sudo docker-backup templates show gitlab   # full description + the root commands
```

| Template | App | Approach |
|---|---|---|
| `gitlab` | GitLab Omnibus | own dump via `gitlab-backup create` (pre/post/restore hooks), live dirs excluded — see [GitLab](gitlab.md) |
| `headscale` | Headscale | file copy of config + SQLite DB; keys in the data dir are essential |
| `immich` | Immich | file copy of media and live Postgres dir (pgvecto-rs image is not auto-detected); thumbs/encoded video excluded |
| `infisical` | Infisical | Postgres auto-dump; Redis cache excluded; `.env` (encryption keys) is essential |
| `invoiceninja` | Invoice Ninja | MySQL/MariaDB auto-dump; keep an offline copy of `APP_KEY` |
| `keycloak` | Keycloak | Postgres auto-dump (pgbackups sidecar correctly ignored) |
| `mailcow` | mailcow-dockerized | MariaDB auto-dump, Redis checkpoint, all named volumes tarred |
| `minio` | MinIO (SNSD) | live file copy of `minio-data`, transient dirs excluded |
| `minio-cold` | MinIO (SNSD) | stops the container via pre-hook for a crash-consistent snapshot, restarts via post-hook |
| `n8n` | n8n | SQLite file copy or Postgres auto-dump; keep `N8N_ENCRYPTION_KEY` offline |
| `nextcloud` | Nextcloud | MariaDB auto-dump, previews/cache/chunk uploads excluded |
| `nextcloud-aio` | Nextcloud AIO | captures only the master container config incl. borg password; data needs AIO's own backup |
| `nocodb` | NocoDB | Postgres auto-dump; `nc_data` volume holds the JWT secret |
| `paperless` | Paperless-ngx | SQL auto-dump, Redis checkpoint, pgbackups sidecar dumps excluded |
| `passbolt` | Passbolt CE | MariaDB auto-dump as app user; gpg/jwt volumes are essential |
| `plausible` | Plausible CE | Postgres auto-dump; ClickHouse as live file copy |
| `registry` | Docker Registry | live file copy of content-addressed blobs |
| `rocketchat` | Rocket.Chat | MongoDB via built-in fsync lock, no hooks needed |
| `supabase` | Supabase | built-in Supabase-flavored Postgres dump, caches excluded |
| `uptime-kuma` | Uptime Kuma | SQLite file copy (WAL-recoverable) |
| `yourls` | YOURLS | MySQL/MariaDB auto-dump |

Every template's description explains the trade-offs and what to keep offline;
read it with `templates show <name>` before relying on it. Templates never run
their commands on their own — hooks always need `--allow-hooks`.

```bash
sudo docker-backup create /opt/gitlab --from-template gitlab \
     --target /mnt/backups --allow-hooks
```

Settings resolve as **explicit CLI flag > template value > built-in default**;
`--exclude` patterns are **added** to the template's. Operators can override a
shipped template per host under `/etc/docker-backup/templates/<app>.json`. Schema
and precedence rules: [Template reference](templates.md).

## Hooks run as root — explicit opt-in required

A hook is **arbitrary shell, run as root** on an unattended timer. Therefore a hook
**never runs until you approve it**:

- Commands are stored in the config but stay **disabled** (`hooks_allowed=false`)
  until you pass **`--allow-hooks`** (or run `docker-backup set <name>
  --allow-hooks` after reviewing).
- If hooks exist but aren't approved, the backup **fails hard** — it is never
  silently skipped (a quietly-skipped GitLab pre-dump would mean data loss).
- On approval a **fingerprint** of the command strings is stored; if a command
  changes afterwards, the next run **refuses to execute** until you re-approve. The
  separate cross-server binding also covers phase/order, cwd, timeout and failure
  policy.
- Backend credentials (S3/B2/SFTP keys) are **stripped** from the hook environment.
- The cross-server **manifest never carries shell** (it's a plaintext file on the
  drive) — see [Restore with a custom command](#restore-with-a-custom-command).

Hooks get these env vars: `DOCKER_BACKUP_STACK_PATH`, `DOCKER_BACKUP_NAME`,
`DOCKER_BACKUP_COMPOSE_FILE`, `DOCKER_BACKUP_PROJECT`, `DOCKER_BACKUP_PHASE`. They
run with the stack folder as working directory by default.

!!! warning "Known limitation"
    Once approved, a command runs unattended on the timer with no per-run
    confirmation. There is no sandboxing (stdlib-only). The controls are provenance
    (in-repo, PR-reviewed templates), human approval, and the change-fingerprint.

## Building configs by hand (without a template)

```bash
sudo docker-backup create /opt/app --target /mnt/backups --no-db-detect \
     --exclude 'app/logs' --exclude 'app/cache' --keep-within 30d \
     --pre-cmd 'docker exec app app-dump' \
     --post-cmd 'rm -f app/dump/*.bak' \
     --restore-cmd 'docker exec app app-restore' \
     --allow-hooks

# Adjust an existing stack (changing a command resets approval):
sudo docker-backup set app --exclude 'app/tmp' --keep-within 14d
sudo docker-backup set app --pre-cmd 'docker exec app app-dump --fast' --allow-hooks
sudo docker-backup set app --no-allow-hooks    # revoke approval
sudo docker-backup set app --clear-hooks       # remove all hooks
```

Exclude pattern semantics: a pattern **with** a `/` (e.g. `app/logs`) is anchored
to the stack root, so it only matches `<stack>/app/logs`. A bare name **without** a
`/` (e.g. `*.log`) matches that basename anywhere in the tree. `--keep-within 30d`
maps to restic `--keep-within` (additive: keeps **all** snapshots in the window, on
top of the daily/weekly/monthly counts).

## Restore with a custom command

For a stack with a restore hook, `docker-backup restore` brings the stack **up**
first (a `docker exec` restore needs a running container — the built-in path
leaves it stopped), shows the root commands, asks for default-no confirmation,
then runs them. `--force` only allows a guarded clean replacement of a non-empty
target; it never approves root commands. `--no-custom-restore` forces the built-in
DB-import path instead.

### Cross-server restore with template hooks

With `--from-repo` the manifest is bound to one full restic snapshot ID and carries
a shell-free template descriptor (name, schema version, local source and full
hook-definition hash), but **no commands**. `--use-template-hooks` loads the exact
locally installed template, verifies its hash, displays every command plus cwd,
timeout and failure policy, and asks for confirmation before restic starts. Add
`--save-config` to persist a complete target-local backup config only after the
entire restore succeeds:

```bash
sudo docker-backup restore /opt/gitlab --from-repo /mnt/backups/gitlab \
     --key-file ./gitlab.key \
     --use-template-hooks --save-config
```

The supplied key is copied to `/etc/docker-backup/keys/gitlab.key` with mode `0600`
(an existing different key is never overwritten). The saved schedule is installed,
and any stale timer with that name is stopped and disabled until review:
`systemctl enable --now docker-backup@gitlab.timer`.

A legacy/unbound manifest, a different `--snapshot`, customized template-owned
settings (hooks, excludes, schedule, retention or DB auto-detection), or a local
template version/hash mismatch fails before restore; take one fresh backup with the
updated tool or restore first and recreate the customized config manually. A
restore-only CLI command cannot be combined with `--save-config`, because it cannot
reconstruct the pre/post hooks required for future backups.

The sidecar descriptor is plaintext and its hash is a **compatibility binding**,
not a signature. It never causes automatic execution: commands come only from the
exact local builtin/operator template and still require a visible, default-no
confirmation.

## Contributing a template

Templates are JSON files under
[docker_backup/templates/](https://github.com/larseberhardt/docker-backup-debian/tree/main/docker_backup/templates)
(an operator can override per host under `/etc/docker-backup/templates/`). Add one
app per file via PR; a test validates **every** shipped template, so a malformed
contribution fails CI. See the [Template reference](templates.md) for the schema
and the "no auto-run shell" rule.
