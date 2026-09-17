# Quick start

All commands need root (`sudo`): they read Compose environments, talk to Docker,
and write to `/etc/docker-backup`.

## 1. Set up a stack

```bash
# Interactive: detects databases, asks for target, offsite and schedule
sudo docker-backup create /opt/xibo

# Pick from all running stacks (the wizard asks Y/n per stack)
sudo docker-backup create --all

# All running stacks fully automatically with one shared target (no prompts)
sudo docker-backup create --all --auto --target /mnt/backups
```

- **`create --all`** lists all running stacks and asks **Y/n** per stack — on "yes"
  it runs the full wizard (target/offsite/frequency per stack).
- **`create --all --auto --target <base>`** sets up **all** running stacks without
  prompting, under one shared target base (for quick whole-server setup).
- Apps that bring their own backup command (GitLab, MinIO, …) are set up from a
  [template](hooks.md): `sudo docker-backup create /opt/gitlab --from-template gitlab`.

`create` shows the restic key once. **Store it offline** — see
[Key escrow & 3-2-1](keys.md).

## 2. Check the result

```bash
sudo docker-backup ls                  # configured backups (incl. bound template)
sudo docker-backup ls --snapshots      # incl. last restic snapshot
sudo docker-backup doctor              # health check (incl. last run result)
```

## 3. Run a backup

The systemd timer runs backups on the configured schedule (default `daily 03:00`).
To trigger one manually:

```bash
sudo docker-backup run xibo
sudo docker-backup run --all           # all stacks, one after another
sudo docker-backup logs xibo -n 50     # journal of the last run
```

## 4. Restore

```bash
cd /opt/xibo && docker compose down
sudo docker-backup restore /opt/xibo
cd /opt/xibo && docker compose up -d
```

Read the [restore runbook](restore.md) before relying on this, and test-restore into
a throwaway directory at least once.

## Managing stacks

```bash
sudo docker-backup snapshots xibo      # list all restic snapshots
sudo docker-backup check xibo          # verify repo integrity (restic check)
sudo docker-backup check --all         # verify all repos
sudo docker-backup logs xibo -n 50     # journal of the last run
sudo docker-backup logs xibo -f        # follow live
sudo docker-backup key show xibo       # restic key + escrow notice (SECRET!)
sudo docker-backup set xibo --schedule 'weekly Mon 04:00'   # change the schedule
sudo docker-backup set xibo --retention 14/8/12             # change retention
sudo docker-backup set xibo --refresh-db-detection          # refresh DBs/scope from Compose
sudo docker-backup rm xibo             # remove the setup (repo + key are kept!)
```

The global `--dry-run` flag shows what a command would execute without changing
anything:

```bash
sudo docker-backup --dry-run run xibo
sudo docker-backup --dry-run set xibo --refresh-db-detection
```

### Retention

Retention is given as `daily/weekly/monthly` snapshot counts (default `7/4/6`).
`--keep-within 30d` additionally keeps **all** snapshots from the last 30 days
(restic `--keep-within`, additive to the counts). Retention is applied after every
run with `restic forget --prune`.

## Backup target and backends

The target is a **base** (path or restic URL); per stack, `<base>/<name>` is used:

| Backend          | Example for the target base                          |
|------------------|------------------------------------------------------|
| Network drive    | `/mnt/backups`  → repo `/mnt/backups/xibo`           |
| S3               | `s3:s3.amazonaws.com/my-bucket`                      |
| SFTP / SSH       | `sftp:user@host:/srv/restic`                         |
| Backblaze B2     | `b2:my-bucket`                                       |
| REST server      | `rest:https://backup.example.com/`                   |

Credentials for object/remote storage do **not** belong in the config, but in
`/etc/docker-backup/backends/<name>.env` (mode 0600), e.g.:

```ini
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

This file is loaded automatically by the systemd service
(`EnvironmentFile=-/etc/docker-backup/backends/%i.env`).

An optional **offsite** repo per stack (`--offsite <base>`) is mirrored after every
run via `restic copy`. See [Key escrow & 3-2-1](keys.md).
