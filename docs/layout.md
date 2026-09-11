# Files & layout

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

## Inside the stack folder

A temporary `.docker-backup/` (dumps + volume tars, `0700`, dump files `0600`) is
created during a run and removed again afterwards — **also when the run fails**
(plaintext DB dumps never stay on disk).

## In the repository

Next to each restic repo lives `docker-backup.manifest.json`, a small non-secret
manifest bound to the newest snapshot. It lets another server bootstrap a restore
from a mounted drive — see [Restore on another server](restore.md#restore-on-another-server).

## Uninstall

`sudo /opt/docker-backup/uninstall.sh` removes the program, timers, and units.
`/etc/docker-backup` (with the keys!) is only deleted after an explicit prompt — the
default is to keep it.
