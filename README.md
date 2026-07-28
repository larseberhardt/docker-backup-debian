# docker-backup

CLI tool for **backing up and restoring complete Docker Compose stacks** on
Debian servers. A backup unit is the whole stack folder: `docker-compose.yml`,
**all** env files (regardless of name — `.env`, `config.env`, …) and the volume data.
Databases are **dumped consistently** (not copied raw); the backend is
[restic](https://restic.net) (deduplication, encryption, retention), and scheduling
is done via a **systemd timer** per stack.

- **Stack introspection** via `docker compose config --format json` (resolves `env_file`
  and variables).
- **DB detection** from the image (`mysql`/`mariadb`/`postgres`); credentials from the
  resolved environment. With `MYSQL_RANDOM_ROOT_PASSWORD=yes`, falls back to the app user.
  A real root password is still used for reliable access. Immediately before every
  MySQL/MariaDB dump, all visible non-system databases are enumerated; a declared
  `MYSQL_DATABASE`/`MARIADB_DATABASE` must be among them. This includes additional user
  databases created later, but excludes the image-owned `mysql`, `sys`, `ndbinfo`,
  `performance_schema` and `information_schema` databases.
- **Consistent DB dump:** `mysqldump --single-transaction` / `mariadb-dump` or `pg_dump`.
  The raw DB data directories are **excluded** from the file backup.
- **Supabase-aware Postgres:** a `supabase/postgres` image is detected automatically and
  dumped as the superuser **`supabase_admin`**, with **cluster globals** (roles + passwords
  via `pg_dumpall --globals-only`) and **every database** (`postgres` **and** `_supabase`),
  preserving ownership/privileges. Override per stack with `--dump-user` and
  `--dump-globals`/`--no-dump-globals` (also via `docker-backup set`). See
  [Supabase & multi-database Postgres](#supabase--multi-database-postgres).
- **Volumes:** bind mounts (located inside the stack folder) **and** named volumes
  (tarred **uncompressed** via a temporary `busybox` container — restic deduplicates
  and compresses; legacy `.tar.gz` archives still restore fine).
- **External bind mounts:** sources OUTSIDE the stack folder (e.g. `/srv/appdata:/data`)
  are detected at `create` and included as `extra_backup_paths` (interactively after a
  prompt; system paths like `docker.sock`/`/etc/localtime` are skipped). Previously they
  were silently missing from the backup.
- **Built-in quiesce for MongoDB & Redis:** detected at `create` like the SQL engines.
  MongoDB is frozen via `db.adminCommand({fsync:1,lock:true})` **only around the file
  capture** (named volume → only during the tar step, before the restic upload; reads
  keep working) and released in a `finally`; Redis/Valkey/KeyDB get a fresh RDB
  checkpoint via `BGSAVE` first. Auth is picked up from the usual env vars
  (`MONGO_INITDB_ROOT_*`, bitnami `MONGODB_ROOT_PASSWORD`, `REDIS_PASSWORD`). Opt out
  with `create --no-quiesce` or `set <name> --no-quiesce`. A failed Mongo lock aborts
  the run (an unlocked WiredTiger copy may be unrestorable); a failed unlock fails it
  loudly (manual `db.fsyncUnlock()` needed).
- **Other stateful services without dump support** (Elasticsearch, ClickHouse, …) are
  flagged at `create`: they are captured as a live file copy only (crash-consistent) —
  add a pre-backup dump hook for guaranteed consistency.
- **3-2-1:** optional offsite repo per stack via `restic copy`. The offsite repo is
  **checked** (weekly `check`) and **pruned** like the primary one (own policy via
  `set <name> --offsite-retention 30/12/24`, opt out with `--no-offsite-prune`).
- **Backends:** local/network drive **as well as** S3, SFTP/SSH, B2, REST, … (natively via restic).
- **Self-healing locks:** stale restic locks (crash/reboot mid-run) are cleared
  before every backup/check run (`restic unlock`).

> Python standard library only, no pip dependencies. Tested on Python 3.9 and up.
> restic **>= 0.17** required (unambiguous missing-repository exit codes and
> `restore --sparse`); **>= 0.19.1** recommended. `install.sh` upgrades older
> distro packages to the latest official release; `doctor` warns if an older
> binary is still active.

---

## Installation

Clone from the git repo and install once on each server:

```bash
git clone https://github.com/larseberhardt/docker-backup-debian.git
cd docker-backup-debian
sudo ./install.sh
```

The installer copies the program to `/opt/docker-backup`, creates the symlink
`/usr/local/bin/docker-backup`, installs `restic`/`git` (if needed), the
systemd units, and the directories under `/etc/docker-backup`. A missing `restic`
is bootstrapped through APT and then upgraded to the latest official release with
restic's built-in, GPG-verified `self-update`; this also upgrades an older existing
installation. When installed from a git checkout, it records the origin URL in
`/etc/docker-backup/update.conf`, so **auto-update** works right away (see
[Updating](#updating)).

Automatic backup and integrity-check services use a systemd-managed restic cache at
`/var/cache/docker-backup/restic`. This is needed by restic 0.19 and newer because
system services do not reliably receive `HOME` or `XDG_CACHE_HOME`; `CacheDirectory=`
creates the protected cache root automatically, including after a tool update.

---

## Updating

The program is updated from the **release tags** (`vX.Y.Z`) of the git repo.

```bash
sudo docker-backup update           # update to the latest release
sudo docker-backup update --check   # only check whether a newer release exists
sudo docker-backup update --yes     # without prompting (for scripts/fleets)
```

`update` fetches the highest `vX.Y.Z` tag into a checkout under
`/opt/docker-backup/repo`, runs `install.sh` there (idempotent — configs, keys,
and backend credentials under `/etc/docker-backup` are preserved) and updates
the version cache. Instead of `docker-backup update` you can also run
`sudo /opt/docker-backup/update.sh` directly.

**Note on an outdated version:** A `docker-backup-update-check.timer` checks
daily (with a randomized delay) for new releases and writes the result to
`/etc/docker-backup/.update-check.json`. If a `docker-backup` command then runs
**interactively** (TTY), a one-line notice appears on stderr, e.g.:

```
[!] Update available: docker-backup 1.1.0 → 1.1.1. Update with 'sudo docker-backup update'.
```

The notice itself does **no** network/git access (it only reads the cache) and can be
suppressed with `DOCKER_BACKUP_NO_UPDATE_NOTICE=1`. **Nothing** is installed
automatically — you start updates yourself.

**Publishing a release:** bump `__version__` in `docker_backup/__init__.py`,
commit, then tag and push:

```bash
git tag v1.1.1 && git push origin main --tags
```

---

## Quick start

```bash
# Set up a stack interactively (detects DB, asks for target + frequency)
sudo docker-backup create /opt/xibo

# Pick from all running stacks (the wizard asks Y/n per stack)
sudo docker-backup create --all

# Set up all running stacks fully automatically with one shared target (no prompts)
sudo docker-backup create --all --auto --target /mnt/backups

# Overview of the configured backups
sudo docker-backup ls
sudo docker-backup ls --snapshots      # incl. last restic snapshot
sudo docker-backup doctor              # health check (incl. last run result)

# Trigger a backup manually (otherwise via timer)
sudo docker-backup run xibo
sudo docker-backup run --all           # all stacks, one after another

# Restore (see runbook below)
sudo docker-backup restore /opt/xibo-test
```

### Managing stacks

```bash
sudo docker-backup snapshots xibo      # list all restic snapshots
sudo docker-backup check xibo          # verify repo integrity (restic check)
sudo docker-backup check --all         # verify all repos
sudo docker-backup logs xibo -n 50     # journal of the last run
sudo docker-backup logs xibo -f        # follow live
sudo docker-backup key show xibo       # restic key + escrow notice (SECRET!)
sudo docker-backup set xibo --schedule 'weekly Mon 04:00'   # change the schedule
sudo docker-backup set xibo --retention 14/8/12             # change retention
sudo docker-backup set xibo --refresh-db-detection           # refresh DBs/scope from Compose
sudo docker-backup rm xibo             # remove the setup (repo + key are kept!)
```

- **`create --all`** lists all running stacks and asks **Y/n** per stack — on "yes" it
  runs the full wizard (target/offsite/frequency per stack).
- **`create --all --auto --target <base>`** sets up **all** running stacks without
  prompting, under one shared target base (for quick whole-server setup).

#### Refreshing legacy MySQL/MariaDB scope

Configs created by versions up to 1.0.3 may contain `all_databases=true` when a root
password was available, even though Compose declares one application database. Such a
dump includes MariaDB's `mysql` system database and can fail when imported into a fresh
container. Legacy app-user configs can have the opposite problem: their static list does
not include additional databases created later. After upgrading, every auto-detected
legacy MySQL/MariaDB plan must therefore be reviewed and refreshed explicitly:

```bash
sudo docker-backup --dry-run set snipeit --refresh-db-detection
sudo docker-backup set snipeit --refresh-db-detection
sudo docker-backup run snipeit
```

The refresh changes only the detected DB/volume plan; repo, key, schedule, retention,
offsite settings, hooks, template and user exclude patterns are preserved. Existing
dump-user and Postgres-globals overrides are preserved too and are shown in the
dry-run diff. A legacy ambiguous config fails before hooks or dump staging and points
to this command instead of being changed silently. At each subsequent backup the
configured application database must be visible and all other visible non-system
databases are included too. The corrected scope applies only to **new** snapshots:
existing all-database snapshots are not rewritten, so create a fresh snapshot and test
that exact snapshot during the restore drill.

This portable scope deliberately does **not** copy accounts/grants from the `mysql` system
database; the fresh container recreates the users declared by Compose. If the DB container
has additional manually managed users or grants, record and recreate those separately and
verify them in the drill. With a non-root dump user, enumeration is privilege-dependent; if
a visible user database cannot be dumped, the whole backup fails instead of silently omitting
it.

The stored dynamic MySQL scope is deliberately backward-safe. Current versions resolve
it to the exact non-system database list before each dump and record that list in the
snapshot manifest. Therefore a refreshed JSON config can intentionally contain both
`database_scope="non-system"` and `all_databases=true`; the marker controls current
versions and the boolean is only the safe legacy fallback. If the program is rolled back
to v1.0.3, that older version ignores
the new scope marker and falls back to `--all-databases`: this may reintroduce the old
system-database restore conflict, but it does not silently omit additional user
databases. Refresh and create a new snapshot again after returning to the current
version.

### Specifying the backup target

The target is a **base** (path or restic URL); per stack, `<base>/<name>` is used:

| Backend          | Example for the target base                          |
|------------------|------------------------------------------------------|
| Network drive    | `/mnt/backups`  → repo `/mnt/backups/xibo`           |
| S3               | `s3:s3.amazonaws.com/my-bucket`                      |
| SFTP / SSH       | `sftp:user@host:/srv/restic`                         |
| Backblaze B2     | `b2:my-bucket`                                        |
| REST server      | `rest:https://backup.example.com/`                   |

Credentials for object/remote storage do **not** belong in the config, but in
`/etc/docker-backup/backends/<name>.env` (mode 0600), e.g.:

```ini
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

This file is loaded automatically by the systemd service
(`EnvironmentFile=-/etc/docker-backup/backends/%i.env`).

---

## Supabase & multi-database Postgres

A `supabase/postgres` image is recognized automatically and changes the Postgres dump
in three ways needed for a faithful Supabase backup:

1. **Role:** dumps as the superuser **`supabase_admin`** (the unprivileged `postgres`
   role can hit *permission denied* on internal schemas like `pgsodium`/`vault`).
2. **Globals:** `pg_dumpall --globals-only` captures **all roles and their passwords**
   (a single-database `pg_dump` cannot). Written to `<service>.globals.sql`.
3. **All databases + ownership:** dumps **both** `postgres` **and** `_supabase`
   (Logflare analytics / Supavisor pooler) to `<service>.<db>.sql`, **keeping**
   `OWNER`/`GRANT` (so e.g. the `auth` schema stays owned by `supabase_auth_admin`).

On **restore** the globals are imported first (existing-role errors from the image's
init scripts are tolerated, so `ALTER ROLE … PASSWORD` still applies — any *other*
error is surfaced as a warning, not silently swallowed), then each database is created
if missing and imported **atomically** (`psql --single-transaction -v ON_ERROR_STOP=1`),
so a failed import rolls back instead of leaving a half-restored database.

> **Restore expects a fresh target.** The DB data dir is excluded from the file backup
> (the logical dump replaces it), so restore brings the DB up on a freshly initialized
> data dir — the `supabase/postgres` image's init scripts run, then the ownership-
> preserving `pg_dump --clean --if-exists` is applied on top. On a fresh target the
> `--clean` DROPs collide with Supabase's own bootstrap objects (inherited
> `realtime.messages_*` partition keys, the `extensions` schema and its
> `grant_pg_*_access()` functions); those failed DROPs are **non-destructive and are
> tolerated** — the restore completes instead of falsely reporting an incomplete DB. A
> failed `CREATE`/`COPY`, by contrast, still aborts the restore. Works for a
> same-version restore; always **test-restore into a throwaway dir** before relying on it.

```bash
# Supabase is auto-detected — nothing special needed:
sudo docker-backup create /opt/supabase-production --target /mnt/backups

# Override the dump role / globals for any Postgres stack:
sudo docker-backup create /opt/foo --target /mnt/backups --dump-user supabase_admin --dump-globals
sudo docker-backup set foo --dump-user supabase_admin --dump-globals   # change an existing setup
```

For a **non-Supabase** Postgres stack nothing changes (single DB, `--no-owner
--no-privileges`, no globals) unless you pass `--dump-globals` / `--dump-user`.

> ⚠️ **Not covered:** if Storage uses an **external S3 backend** (`STORAGE_BACKEND=s3`),
> the uploaded file **objects live in that bucket, not in the stack** — Postgres holds
> only the `storage.objects` metadata. Back up that bucket separately. Also keep the
> **`db-config` named volume** (pgsodium/Vault key) — it is captured by default, and is
> required to decrypt Vault/encrypted columns after a restore.

---

## App templates & hooks (GitLab, …)

Some apps don't fit the "detect the DB → `pg_dump`/`mysqldump`" model — they bring their
own consistent backup command and store large, regenerable live data you don't want in the
archive. **GitLab** is the canonical case: `gitlab-backup create` produces the consistent
dump, the Gitaly/Postgres/Redis/registry/… live dirs should be excluded, and restore is its
own sequence. Three building blocks cover this:

1. **Hooks** — shell commands that run **before** the backup (`pre_backup`), **after** it
   (`post_backup`, always — even on failure, for cleanup), and as a custom **restore**
   command instead of the built-in DB import.
2. **User excludes** (`--exclude`) — extra paths/globs, relative to the stack folder.
3. **`--no-db-detect`** — skip DB auto-detection entirely (the app dumps its own DB).

### Hooks run as root — explicit opt-in required

A hook is **arbitrary shell, run as root** on an unattended timer. Therefore a hook **never
runs until you approve it**:

- Commands are stored in the config but stay **disabled** (`hooks_allowed=false`) until you
  pass **`--allow-hooks`** (or run `docker-backup set <name> --allow-hooks` after reviewing).
- If hooks exist but aren't approved, the backup **fails hard** — it is never silently
  skipped (a quietly-skipped GitLab pre-dump would mean data loss).
- On approval a **fingerprint** of the command strings is stored; if a command changes
  afterwards, the next run **refuses to execute** until you re-approve. The separate
  cross-server binding also covers phase/order, cwd, timeout and failure policy.
- Backend credentials (S3/B2/SFTP keys) are **stripped** from the hook environment.
- The cross-server **manifest never carries shell** (it's a plaintext file on the drive) —
  see the restore note below.

Hooks get these env vars: `DOCKER_BACKUP_STACK_PATH`, `DOCKER_BACKUP_NAME`,
`DOCKER_BACKUP_COMPOSE_FILE`, `DOCKER_BACKUP_PROJECT`, `DOCKER_BACKUP_PHASE`. They run with
the stack folder as working directory by default.

> **Known limitation:** once approved, a command runs unattended on the timer with no
> per-run confirmation. There is no sandboxing (stdlib-only). The controls are provenance
> (in-repo, PR-reviewed templates), human approval, and the change-fingerprint.

### GitLab quick start (via the shipped template)

```bash
# See what ships, then inspect the gitlab template (shows the root commands):
sudo docker-backup create --list-templates
sudo docker-backup templates show gitlab

# Create from the template and approve its commands in one go:
sudo docker-backup create /opt/gitlab --from-template gitlab \
     --target /mnt/backups --allow-hooks
```

This produces a config with `db_autodetect=false`, the live-data excludes,
`retention { daily 7, keep_within "30d" }`, `daily 04:00`, and the three commands:

| Phase    | Command                                                                 |
|----------|-------------------------------------------------------------------------|
| pre      | remove stale generated archives, then `gitlab-backup create CRON=1`      |
| post     | `rm -f gitlab/data/backups/*.tar` *(cwd = stack)*                       |
| restore  | stop Puma/Sidekiq, then run `GITLAB_ASSUME_YES=1 … restore`            |

A backup run then: pre-hook creates the consistent dump → the DB loop is skipped →
restic archives `/opt/gitlab` **with** the fresh `.tar` but **without** the live dirs →
the post-hook deletes the archived `.tar` (runs even if the backup failed).

> The `.tar` that `gitlab-backup create` writes (under `gitlab/data/backups/`) is
> deliberately **not** in the exclude list — it is the only consistent DB copy and must be
> archived. In contrast, the raw `gitlab/data/git-data` repository tree is explicitly
> excluded because those repositories are already contained in that application archive.
> The template ships tests asserting both invariants.

With GitLab's local filesystem storage, that application archive also contains repositories,
uploads, artifacts, packages, LFS objects and local container-registry images. The matching
live directories can therefore stay excluded from restic. GitLab does **not** put blobs or
registry data held in external object storage into this archive; those buckets need their own
provider-level backup. `docker-backup` captures the Compose stack separately; verify that the
GitLab config volume (including `gitlab-secrets.json`) is either inside that stack or selected
as an external bind.

GitLab requires the target container to use the **exact same GitLab version and edition**
as the archive. The restored Compose file should therefore pin the image version; GitLab
itself aborts a mismatched restore. The hook requires exactly one regular
`*_gitlab_backup.tar`, derives its validated ID, and passes it as `BACKUP=...`; a stale or
ambiguous archive set therefore fails closed. It also sets `GITLAB_ASSUME_YES=1` explicitly
so an unattended restore cannot wait at GitLab's destructive-action prompts. On a fresh
server it waits up to 30 minutes for Omnibus first-boot reconfiguration to report successful
completion and for its Cinc/Chef process to exit. It then requires the image's
`gitlab-healthcheck` and `gitlab-rake gitlab:env:info` to pass twice consecutively before
the import. Status lines remain visible throughout, and a stopped container fails early with
its recent logs instead of racing reconfiguration against the destructive restore. Only the
`gitlab` Compose service is started for this hook (not Runner or DinD), without published host
ports and with automatic restart and health checks disabled for the transient restore container.
The hook deliberately does not restart Puma or Sidekiq after importing; `docker-backup`
stops the temporary restore service in its cleanup path, avoiding even a brief window for
restored jobs, email or webhooks to leave the drill server. Restore-time Compose cleanup
allows containers up to 120 seconds to stop gracefully; process-heavy Omnibus GitLab
shutdowns can exceed Compose's ten-second default, which would otherwise end in SIGKILL
(exit 137) even after a successful import.

Existing GitLab configs keep all previously stored settings; `docker-backup set` cannot add
the builtin template provenance or replace the complete exclude/hook set. Before creating
the first v5 manifest, recreate an older config from the current builtin template. First run
`docker-backup ls` and record its existing repository target. Pass the repository's **parent
base** to `--target` (for example, repository `/mnt/backups/gitlab` means target base
`/mnt/backups`):

```bash
sudo docker-backup ls
sudo docker-backup create /opt/gitlab --name gitlab \
  --from-template gitlab --target /mnt/backups \
  --allow-hooks --force --non-interactive
```

`--force` replaces only the local config; the existing
`/etc/docker-backup/keys/gitlab.key` is deliberately reused, so the existing repository
remains readable. If the old config used an offsite repository or non-default scheduling,
pass the corresponding `--offsite`/`--schedule` values as well. Review the resulting config,
then run one fresh backup to publish a template-bound v5 manifest.

### Building configs by hand (without a template)

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

Exclude pattern semantics: a pattern **with** a `/` (e.g. `app/logs`) is anchored to the
stack root, so it only matches `<stack>/app/logs`. A bare name **without** a `/` (e.g.
`*.log`) matches that basename anywhere in the tree. `--keep-within 30d` maps to restic
`--keep-within` (additive: keeps **all** snapshots in the window, on top of the
daily/weekly/monthly counts).

### Restore with a custom command

For a stack with a restore hook, `docker-backup restore` brings the stack **up** first (a
`docker exec` restore needs a running container — the built-in path leaves it stopped),
shows the root commands, asks for default-no confirmation, then runs them. `--force` only
allows a guarded clean replacement of a non-empty target; it never approves root commands.
`--no-custom-restore` forces the built-in DB-import path instead.

**Cross-server restore** (`--from-repo`): the manifest is bound to one full restic snapshot
ID and carries a shell-free template descriptor (name, schema version, local source and
full hook-definition hash), but **no commands**. `--use-template-hooks` loads the exact
locally installed template, verifies its hash, displays every command plus cwd, timeout and
failure policy, and asks for confirmation before restic starts. Add `--save-config` to
persist a complete target-local backup config only after the entire restore succeeds:

```bash
sudo docker-backup restore /opt/gitlab --from-repo /mnt/backups/gitlab \
     --key-file ./gitlab.key \
     --use-template-hooks --save-config
```

The supplied key is copied to `/etc/docker-backup/keys/gitlab.key` with mode `0600` (an
existing different key is never overwritten). The saved schedule is installed, and any
stale timer with that name is stopped and disabled until review:
`systemctl enable --now docker-backup@gitlab.timer`. A legacy/unbound manifest, a different
`--snapshot`, customized template-owned settings (hooks, excludes, schedule, retention or
DB auto-detection), or a local template version/hash mismatch fails before restore; take
one fresh backup with the updated tool or restore first and recreate the customized config
manually. A restore-only CLI command cannot be combined with
`--save-config`, because it cannot reconstruct the pre/post hooks required for future
GitLab backups.

The sidecar descriptor is plaintext and its hash is a **compatibility binding**, not a
signature. It never causes automatic execution: commands come only from the exact local
builtin/operator template and still require a visible, default-no confirmation.

### Contributing a template

Templates are JSON files under [docker_backup/templates/](docker_backup/templates/) (an
operator can override per host under `/etc/docker-backup/templates/`). Add one app per
file via PR; a test validates **every** shipped template, so a malformed contribution fails
CI. See [docs/templates.md](docs/templates.md) for the schema and the "no auto-run shell"
rule. Templates **never** run their commands on their own — the user must `--allow-hooks`.

---

## Schedule (systemd timer)

Each stack has a timer `docker-backup@<name>.timer`. The schedule comes from
a drop-in `/etc/systemd/system/docker-backup@<name>.timer.d/schedule.conf`.

The frequency (`--schedule`, default `daily 03:00`) is mapped to `OnCalendar`:

| Input                | OnCalendar             |
|----------------------|------------------------|
| `daily 03:00`        | `*-*-* 03:00:00`       |
| `weekly Mon 04:30`   | `Mon *-*-* 04:30:00`   |
| `monthly 02:00`      | `*-*-01 02:00:00`      |
| `hourly`             | `*-*-* *:00:00`        |
| `custom <expr>`      | `<expr>` (1:1)         |

Useful commands:

```bash
systemctl list-timers 'docker-backup@*'
journalctl -u docker-backup@xibo.service     # logs of the last run
systemctl start docker-backup@xibo.service   # run immediately
```

---

## Email notification on failure

Optionally you get an email when a backup fails. This is triggered
via the systemd hook `OnFailure=docker-backup-notify@<name>.service` — it also fires
on crash, timeout, or OOM kill, not only on a "clean" failure exit.

```bash
sudo docker-backup notify setup     # set up SMTP interactively (wizard)
sudo docker-backup notify test      # send a test email
sudo docker-backup notify show      # current config (password masked)
```

The wizard writes `/etc/docker-backup/notify.json` (mode 0600), e.g.:

```json
{
  "enabled": true,
  "method": "smtp",
  "on_failure": true,
  "on_success": false,
  "smtp": {
    "host": "smtp.example.com",
    "port": 587,
    "security": "starttls",
    "username": "backup@example.com",
    "password": "…",
    "from": "backup@example.com",
    "to": ["admin@example.com"],
    "timeout": 30
  }
}
```

- **Default: failures only.** Set `on_success` to `true` (or answer the
  wizard question with "yes") to additionally receive a short confirmation
  (snapshot ID/time) after every successful run.
- `security`: `starttls` (default, port 587), `ssl` (port 465) or `none`.
- The SMTP password can also be set, instead of inline, via `smtp.password_file` (path) or the
  environment variable `DOCKER_BACKUP_SMTP_PASSWORD` (e.g. via
  `/etc/docker-backup/notify.env`, loaded by the notify service).
- Python stdlib only (`smtplib`) — no local mail server needed.

Failure emails contain the last `journalctl` excerpt of the failed run.

## Status & integrity check

After every run, `run` writes the result (success/failure, time, snapshot) to
`/etc/docker-backup/status/<name>.json`. `doctor` evaluates this and additionally shows
repo reachability, key permissions, and timer state:

```bash
sudo docker-backup doctor          # all stacks; exit code 1 on problems
sudo docker-backup doctor xibo     # a single stack
```

A backup you can't restore is worthless — so a **weekly timer**
(`docker-backup-check.timer`) verifies repo integrity via `restic check` — the
**primary and, if configured, the offsite repo** — and writes the result to
`/etc/docker-backup/.check-status.json`. If a check fails, the next
**interactive** command prints a one-line warning on stderr (suppress with
`DOCKER_BACKUP_NO_CHECK_NOTICE=1`). Stale restic locks are cleared beforehand
(`restic unlock` removes only locks of dead processes).

```bash
sudo docker-backup check xibo                  # metadata check only (fast)
sudo docker-backup check --all                 # all repos
sudo docker-backup check xibo --read-data-subset 5%   # also read 5% of the data
```

## Restore runbook

Goal: after the restore, only `docker compose up -d` is needed. The DB dump is
imported automatically; the stack then remains **stopped**.

```bash
# 0) Stop the target stack first. Restore fails closed if any running container
#    has a writable bind mount at, above, or below a restore target.
cd /opt/xibo && docker compose down

# 1) Restore (source is derived from the target's base name, here 'xibo-test'
#    -> uses the config 'xibo-test'. Different source? --from xibo)
sudo docker-backup restore /opt/xibo --from xibo

# 2) What happens during this:
#    - verify no running container has a writable bind overlapping the target
#    - restic restore --sparse latest into a random, root-only scratch folder on
#      the target filesystem
#      (sparse files keep their compact disk usage, and the restored tree can
#      normally be moved into place without a second full copy; use a restore
#      target below a root-controlled path such as /opt)
#    - authenticate the restored Compose model while it is still protected in scratch,
#      stop/remove the exact Compose project, and verify target/external binds are quiet
#    - selected external bind paths are mapped by Compose service + container target
#      to the new server, displayed, and require a separate default-no confirmation;
#      an existing target is clean-replaced only with --force
#    - place the stack tree (compose + env files + bind data + dumps + volume tars)
#      and selected external data as clean replacements under --force
#    - recreate named volumes empty and restore them from the tars; an existing
#      volume fails without --force, while --force deletes/recreates it only after
#      proving no running or stopped container still references it; externally
#      managed/shared Compose volumes require a manual restore
#    - per DB: start only the DB service, wait for "ready", import the dump,
#      then stop/remove the DB service again (data is preserved)
#    - after all imports succeed, remove the temporary dumps and volume tars;
#      failed restores retain them for diagnosis/manual retry

# 3) Check env files (ports, hostnames, secrets) and start the stack:
cd /opt/xibo
docker compose up -d
```

Options: `--from <name>` (source config), `--snapshot <id>` (instead of `latest`),
`--force` (overwrite a non-empty target and clean-replace existing non-external named
volumes). `--force` does not approve hook commands or placement of external bind data.

### Restore on another server (test server, shared drive)

Two servers share the same mounted backup drive and you want to test-restore
server A's backup on server B — which has neither A's config nor its key. Every
backup now writes a small **non-secret manifest** next to the repo
(`<repo>/docker-backup.manifest.json`), so server B can bootstrap a restore
directly from the drive. Only the restic key has to travel (the repo stays
encrypted).

```bash
# On server A: copy the key once (or use `docker-backup key show <name>`)
scp /etc/docker-backup/keys/xibo.key serverB:/root/xibo.key

# On server B: see what's restorable on the mounted drive…
sudo docker-backup ls --on-repo /mnt/backups

# …then restore straight from the repo path — no local config needed.
# --from-repo overrides the repo path, so it works even if the drive is
# mounted elsewhere on B. Dry-run first to inspect the plan:
sudo docker-backup --dry-run restore /opt/xibo-test \
     --from-repo /mnt/backups/xibo --key-file /root/xibo.key \
     --no-custom-restore
sudo docker-backup restore /opt/xibo-test \
     --from-repo /mnt/backups/xibo --key-file /root/xibo.key \
     --no-custom-restore --force
```

Notes: `--from-repo` is for **mounted local drives** (remote `s3:`/`sftp:` repos
keep using `--from <name>`). DBs with `password_source: stored` keep their password only
on server A: file restore is still possible, but `--save-config` refuses to create an
incomplete unattended setup until that credential is moved to the Compose environment or
configured locally. `env:`-based DBs (incl. Supabase) work as-is. The reconstructed config
is ephemeral by default. Persisting it always requires
`--use-template-hooks --save-config`; the exact local template is verified even when it has
no hooks. Template reconstruction uses the full snapshot ID bound into the newest v5
manifest, not a different/older `--snapshot`. The schedule drop-in is restored, the key is
installed into the managed key directory, and the timer deliberately remains disabled.
Every v5 cross-server restore requires one explicit policy choice: trusted local
`--use-template-hooks`, a reviewed `--restore-cmd`, or `--no-custom-restore`. This prevents
a modified plaintext sidecar from hiding that an application-specific restore was needed.

### Manual restore without the tool (emergency)

If the tool is not available, restic + the matching key is enough:

```bash
restic -r /mnt/backups/xibo --password-file /etc/docker-backup/keys/xibo.key snapshots
restic -r /mnt/backups/xibo --password-file /etc/docker-backup/keys/xibo.key restore latest --target /restore --sparse
# Stack is under /restore/opt/xibo, dumps under .docker-backup/dumps/,
# volume tars under .docker-backup/volumes/. Then import the DB dump manually:
#   docker compose up -d <db-service>
#   docker compose exec -T <db-service> mysql -u <user> -p < .docker-backup/dumps/<db-service>.sql
```

---

## Key escrow / 3-2-1 (IMPORTANT)

The restic keys are located under `/etc/docker-backup/keys/<name>.key` (mode 0600,
root). **Without the matching key, a backup is unrecoverable.** If the
key exists only on the source server, it is also lost if that server is lost.

Recommendations:

1. Keep the key **additionally offline** (password manager / vault). `create`
   shows the key once; `docker-backup key show <name>` displays it again later.
2. Store an **encrypted copy** of `/etc/docker-backup/keys/` on a separate
   system.
3. Implement **3-2-1**: an offsite repo per stack (`--offsite <base>`) that is mirrored
   after every run via `restic copy` (different location, separate credentials).
   The same key is used for the primary and offsite repo. After the copy, the offsite
   repo is pruned with the primary retention (override:
   `set <name> --offsite-retention 30/12/24`; keep everything: `--no-offsite-prune`)
   and it is included in the weekly integrity check.
4. For **ransomware-grade** protection, make the offsite repo append-only from the
   server's point of view (e.g. `rest-server --append-only` or S3 object lock): whoever
   has root on the server also has the key and could otherwise prune both repos.

> **`rm` is safe:** `docker-backup rm <name>` only removes the timer + config; the key,
> secrets, and the remote repo are kept. Only `rm <name> --purge-keys` also deletes the
> local key (the remote repo is **never** touched).

---

## Files & layout

```
/opt/docker-backup/            program (Python package + launcher)
├── update.sh                  update mechanism (used by 'docker-backup update')
├── uninstall.sh               uninstaller (counterpart to install.sh)
└── repo/                      git checkout for updates (created by update.sh)
/usr/local/bin/docker-backup   symlink to the launcher
/etc/bash_completion.d/docker-backup   bash completion (if the directory exists)
/var/cache/docker-backup/restic        systemd-managed restic repository cache
/etc/docker-backup/
├── configs/<name>.json        stack config (0640)
├── keys/<name>.key            restic repo key (0600)
├── backends/<name>.env        backend credentials (0600, loaded by systemd)
├── secrets/<name>-<svc>.pw    DB password only if entered interactively (0600)
├── status/<name>.json         result of the last run (0640)
├── templates/<app>.json       operator template overrides (optional)
├── notify.json                email/SMTP settings (0600, optional)
├── update.conf                auto-update: REMOTE_URL/BRANCH (0644)
├── .update-check.json         version cache of the update check (0644)
└── .check-status.json         result cache of the integrity check (0640)
/etc/systemd/system/
├── docker-backup@.service               template (oneshot)
├── docker-backup@.timer                 template
├── docker-backup-notify@.service        failure notification (OnFailure hook)
├── docker-backup-update-check.service   daily update check
├── docker-backup-update-check.timer     timer for it
├── docker-backup-check.service          weekly integrity check (restic check)
├── docker-backup-check.timer            timer for it
└── docker-backup@<name>.timer.d/schedule.conf   schedule drop-in
```

Inside the stack folder, a temporary `.docker-backup/` (dumps + volume tars, `0700`,
dump files `0600`) is created during a run and removed again afterwards — **also when the
run fails** (plaintext DB dumps never stay on disk).

**Uninstall:** `sudo /opt/docker-backup/uninstall.sh` removes the program, timers, and
units. `/etc/docker-backup` (with the keys!) is only deleted after an explicit prompt —
the default is to keep it.

---

## Development / tests

```bash
# Syntax check (Python 3.9 compatible)
python3 -m py_compile docker_backup/*.py docker_backup/commands/*.py

# Bash syntax check of the installer/update scripts
bash -n install.sh update.sh uninstall.sh

# Unit/smoke tests (without Docker/restic; use fixtures and argv builders)
python3 -m unittest discover -s tests

# CLI help
python3 -m docker_backup --help
python3 -m docker_backup create --help

# Dry run (shows the commands that would be executed, changes nothing)
sudo docker-backup --dry-run run xibo
```

## License

[MIT](LICENSE)
