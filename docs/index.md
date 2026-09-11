# docker-backup

Back up and restore **complete Docker Compose stacks** on Debian servers.

A backup unit is the whole stack folder: the Compose file, **all** env files
(`.env`, `config.env`, …), bind-mount data and named volumes. Databases are
**dumped consistently** instead of copied raw. The backend is
[restic](https://restic.net) (deduplication, encryption, retention); every stack
gets its own **systemd timer**.

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

## Three commands

```bash
sudo ./install.sh                                   # once per server
sudo docker-backup create /opt/xibo                 # set up a stack (wizard)
sudo docker-backup run xibo                         # back it up now
```

Continue with the [Installation](installation.md) and the [Quick start](quickstart.md).

!!! warning "Keep the restic key safe"
    Without `/etc/docker-backup/keys/<name>.key` a backup is unrecoverable.
    `create` shows the key once; store it offline. See [Key escrow & 3-2-1](keys.md).

## Where to go next

| I want to… | Page |
|---|---|
| install, update or uninstall | [Installation](installation.md) |
| set up my first backup | [Quick start](quickstart.md) |
| understand what is captured and how a run works | [How it works](how-it-works.md) |
| back up MySQL, Postgres, Supabase, MongoDB or Redis | [Databases](databases.md) |
| use an app template or add my own hooks | [Templates & hooks](hooks.md) |
| back up GitLab | [GitLab](gitlab.md) |
| restore a stack (same or another server) | [Restore](restore.md) |
| change schedules and monitor health | [Scheduling & monitoring](operations.md) |
| get an email when a backup fails | [Notifications](notifications.md) |
| protect my keys and go 3-2-1 | [Key escrow & 3-2-1](keys.md) |
| know which files live where | [Files & layout](layout.md) |
| write or contribute a template | [Template reference](templates.md) |
| run the tests or publish a release | [Development](development.md) |
