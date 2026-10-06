# Installation

## Requirements

- A Debian or Ubuntu server with Docker and the Compose v2 plugin (`docker compose`).
- Python 3.9 or newer. Standard library only, no pip dependencies.
- restic **>= 0.17** (unambiguous missing-repository exit codes and
  `restore --sparse`); **>= 0.19.1** recommended. The installer takes care of this.
- systemd (per-stack timers, `OnFailure` notifications, managed restic cache).

## Install

Clone the repository and run the installer once on each server:

```bash
git clone https://github.com/larseberhardt/docker-backup-debian.git
cd docker-backup-debian
sudo ./install.sh
```

The installer

- copies the program to `/opt/docker-backup` and creates the symlink
  `/usr/local/bin/docker-backup`;
- installs `restic` and `git` if they are missing, the systemd units, and the
  directories under `/etc/docker-backup`;
- installs bash completion to `/etc/bash_completion.d/docker-backup` (if that
  directory exists);
- records the git origin URL in `/etc/docker-backup/update.conf` when installed
  from a checkout, so [updating](#updating) works right away.

Re-running the installer is idempotent: configs, keys, and backend credentials
under `/etc/docker-backup` are preserved.

### restic version

A missing `restic` is bootstrapped through APT and then upgraded to the latest
official release:

- via restic's built-in, GPG-verified `self-update` where available;
- otherwise (the Debian/Ubuntu packages are built without that command) via the
  official GitHub release, whose SHA256 is verified against `SHA256SUMS` before the
  binary is installed to `/usr/local/bin/restic`.

This also upgrades an older existing installation. If GitHub is unreachable, the
installer keeps the version that is already installed and only warns.
`docker-backup doctor` warns if an older binary is still active.

### restic cache for system services

Automatic backup and integrity-check services use a systemd-managed restic cache at
`/var/cache/docker-backup/restic`. restic 0.19 and newer needs this because system
services do not reliably receive `HOME` or `XDG_CACHE_HOME`; `CacheDirectory=`
creates the protected cache root automatically, including after a tool update.

## Updating

The program is updated from the **release tags** (`vX.Y.Z`) of the git repository.

```bash
sudo docker-backup update           # update to the latest release
sudo docker-backup update --check   # only check whether a newer release exists
sudo docker-backup update --yes     # without prompting (for scripts/fleets)
```

`update` fetches the highest `vX.Y.Z` tag into a checkout under
`/opt/docker-backup/repo`, runs `install.sh` there (idempotent — configs, keys, and
backend credentials under `/etc/docker-backup` are preserved) and updates the
version cache. You can also run `sudo /opt/docker-backup/update.sh` directly.

### Update notice

A `docker-backup-update-check.timer` checks daily (with a randomized delay) for new
releases and writes the result to `/etc/docker-backup/.update-check.json`. If a
`docker-backup` command then runs **interactively** (TTY), a one-line notice appears
on stderr:

```
[!] Update available: docker-backup 1.1.1 → 1.1.2. Update with 'sudo docker-backup update'.
```

The notice itself does **no** network or git access (it only reads the cache) and
can be suppressed with `DOCKER_BACKUP_NO_UPDATE_NOTICE=1`. **Nothing** is installed
automatically — you start updates yourself.

!!! note "After upgrading from 1.0.3 or older"
    Auto-detected MySQL/MariaDB configs from those versions must be reviewed and
    refreshed once. See [Refreshing legacy MySQL/MariaDB scope](databases.md#refreshing-legacy-mysqlmariadb-scope).

!!! note "After upgrading from 1.2.0 or older"
    Postgres stacks with a custom `PGDATA` must be refreshed once, otherwise their
    next run fails. See [Custom `PGDATA`](databases.md#custom-pgdata).

## Uninstall

```bash
sudo /opt/docker-backup/uninstall.sh
```

This removes the program, timers, and units. `/etc/docker-backup` (with the keys!)
is only deleted after an explicit prompt — the default is to keep it.
