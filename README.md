# docker-backup

Back up and restore **complete Docker Compose stacks** on Debian servers.

A backup unit is the whole stack folder: the Compose file, **all** env files
(`.env`, `config.env`, …), bind-mount data and named volumes. Databases are
**dumped consistently** instead of copied raw. The backend is
[restic](https://restic.net) (deduplication, encryption, retention); every stack
gets its own **systemd timer**.

> 📖 **Documentation:** <https://larseberhardt.github.io/docker-backup-debian/>
> (source in [docs/](docs/))

## Highlights

- **Whole-stack unit.** Compose file, every env file, bind mounts inside the stack
  folder, named volumes, and external bind mounts outside the stack folder.
- **Consistent database dumps.** MySQL/MariaDB (`--single-transaction`), Postgres
  (`pg_dump`) and Supabase-aware Postgres (superuser, cluster globals, all
  databases). Raw DB data directories are excluded from the file backup.
- **Built-in quiesce** for MongoDB (fsync lock only around the file capture) and
  Redis/Valkey/KeyDB (`BGSAVE` checkpoint).
- **App templates and hooks.** 21 shipped templates (GitLab, Nextcloud, mailcow,
  Immich, Paperless, …). Hooks run as root and only after explicit approval.
- **3-2-1 ready.** Optional offsite repo per stack via `restic copy`, checked and
  pruned like the primary.
- **Any restic backend.** Local or network drive, S3, SFTP/SSH, Backblaze B2, REST server.
- **Operations built in.** Weekly `restic check`, `doctor` health check, email on
  failure, self-healing stale locks, daily update check.
- **No dependencies.** Python standard library only (3.9+), restic ≥ 0.17
  (≥ 0.19.1 recommended).

## Installation

```bash
git clone https://github.com/larseberhardt/docker-backup-debian.git
cd docker-backup-debian
sudo ./install.sh
```

The installer copies the program to `/opt/docker-backup`, links
`/usr/local/bin/docker-backup`, installs restic (latest upstream release) and the
systemd units. Update later with `sudo docker-backup update`.
Details: [Installation](docs/installation.md).

## Quick start

```bash
# Set up a stack interactively (detects DBs, asks for target + schedule)
sudo docker-backup create /opt/xibo

# Or: all running stacks at once, one shared target, no prompts
sudo docker-backup create --all --auto --target /mnt/backups

# Overview and health
sudo docker-backup ls
sudo docker-backup doctor

# Run a backup now (otherwise the timer does it)
sudo docker-backup run xibo

# Restore (read the runbook first)
cd /opt/xibo && docker compose down
sudo docker-backup restore /opt/xibo
```

**Keep the restic key safe.** Without `/etc/docker-backup/keys/<name>.key` a
backup is unrecoverable. See [Key escrow & 3-2-1](docs/keys.md).

## Documentation

| Topic | Page |
|---|---|
| Install, update, uninstall | [Installation](docs/installation.md) |
| First backup, managing stacks, backup targets | [Quick start](docs/quickstart.md) |
| What a backup contains, how a run works | [How it works](docs/how-it-works.md) |
| MySQL/MariaDB, Postgres, Supabase, MongoDB, Redis | [Databases](docs/databases.md) |
| App templates, hooks, custom restore commands | [Templates & hooks](docs/hooks.md) |
| GitLab | [GitLab](docs/gitlab.md) |
| Restore runbook, cross-server restore, emergency restore | [Restore](docs/restore.md) |
| Timers, status, integrity check | [Scheduling & monitoring](docs/operations.md) |
| Email on failure | [Notifications](docs/notifications.md) |
| Keys, offsite repos, ransomware protection | [Key escrow & 3-2-1](docs/keys.md) |
| Paths on disk | [Files & layout](docs/layout.md) |
| Template schema, contributing | [Template reference](docs/templates.md) |
| Tests, releases, building the docs | [Development](docs/development.md) |

## Development

```bash
python3 -m unittest discover -s tests     # unit/smoke tests (no Docker/restic needed)
python3 -m docker_backup --help
sudo docker-backup --dry-run run xibo     # show what would run, change nothing
```

## License

[MIT](LICENSE)
