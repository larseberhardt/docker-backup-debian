# Scheduling & monitoring

## Schedule (systemd timer)

Each stack has a timer `docker-backup@<name>.timer`. The schedule comes from a
drop-in `/etc/systemd/system/docker-backup@<name>.timer.d/schedule.conf`.

The frequency (`--schedule`, default `daily 03:00`) is mapped to `OnCalendar`:

| Input                | OnCalendar             |
|----------------------|------------------------|
| `daily 03:00`        | `*-*-* 03:00:00`       |
| `weekly Mon 04:30`   | `Mon *-*-* 04:30:00`   |
| `monthly 02:00`      | `*-*-01 02:00:00`      |
| `hourly`             | `*-*-* *:00:00`        |
| `custom <expr>`      | `<expr>` (1:1)         |

```bash
sudo docker-backup set xibo --schedule 'weekly Mon 04:00'

systemctl list-timers 'docker-backup@*'
journalctl -u docker-backup@xibo.service     # logs of the last run
systemctl start docker-backup@xibo.service   # run immediately
sudo docker-backup logs xibo -f              # same journal, follow live
```

## Status and health check

After every run, `run` writes the result (success/failure, time, snapshot) to
`/etc/docker-backup/status/<name>.json`. `doctor` evaluates this and additionally
shows repo reachability, key permissions, timer state, and the active restic
version:

```bash
sudo docker-backup doctor          # all stacks; exit code 1 on problems
sudo docker-backup doctor xibo     # a single stack
```

The exit code makes `doctor` suitable for external monitoring (a cron job or a
check that alerts on non-zero exit).

## Integrity check

A backup you can't restore is worthless — so a **weekly timer**
(`docker-backup-check.timer`) verifies repo integrity via `restic check` — the
**primary and, if configured, the offsite repo** — and writes the result to
`/etc/docker-backup/.check-status.json`. If a check fails, the next **interactive**
command prints a one-line warning on stderr (suppress with
`DOCKER_BACKUP_NO_CHECK_NOTICE=1`). Stale restic locks are cleared beforehand
(`restic unlock` removes only locks of dead processes).

```bash
sudo docker-backup check xibo                  # metadata check only (fast)
sudo docker-backup check --all                 # all repos
sudo docker-backup check xibo --read-data-subset 5%   # also read 5% of the data
```

## Failure notification

A failed run (also crash, timeout or OOM kill) triggers
`docker-backup-notify@<name>.service` via systemd's `OnFailure=`. Set up email
delivery as described in [Notifications](notifications.md).
