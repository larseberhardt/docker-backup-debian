# GitLab

GitLab (Omnibus in a container) is backed up with the shipped `gitlab` template:
`gitlab-backup create` produces the consistent application archive, the live
directories are excluded from restic, and restore runs GitLab's own restore. DB
auto-detection is off (GitLab dumps itself).

## Quick start

```bash
# See what ships, then inspect the gitlab template (shows the root commands):
sudo docker-backup templates list
sudo docker-backup templates show gitlab

# Create from the template and approve its commands in one go:
sudo docker-backup create /opt/gitlab --from-template gitlab \
     --target /mnt/backups --allow-hooks
```

This produces a config with `db_autodetect=false`, the live-data excludes,
`retention { daily 7, keep_within "30d" }`, `daily 04:00`, and the three commands:

| Phase    | Command                                                                 |
|----------|-------------------------------------------------------------------------|
| pre      | remove stale generated archives, then `gitlab-backup create CRON=1 GZIP_RSYNCABLE=yes` |
| post     | `rm -f gitlab/data/backups/*.tar` *(cwd = stack)*                       |
| restore  | stop Puma/Sidekiq, then run `GITLAB_ASSUME_YES=1 … restore`            |

A backup run then: pre-hook creates the consistent dump → the DB loop is skipped →
restic archives `/opt/gitlab` **with** the fresh `.tar` but **without** the live
dirs → the post-hook deletes the archived `.tar` (runs even if the backup failed).

## What is (not) in the archive

The `.tar` that `gitlab-backup create` writes (under `gitlab/data/backups/`) is
deliberately **not** in the exclude list — it is the only consistent DB copy and
must be archived. In contrast, the raw `gitlab/data/git-data` repository tree is
explicitly excluded because those repositories are already contained in that
application archive. The template ships tests asserting both invariants.

With GitLab's local filesystem storage, that application archive also contains
repositories, uploads, artifacts, packages, LFS objects and local
container-registry images. The matching live directories can therefore stay
excluded from restic. GitLab does **not** put blobs or registry data held in
external object storage into this archive; those buckets need their own
provider-level backup.

`docker-backup` captures the Compose stack separately; verify that the GitLab config
volume (including `gitlab-secrets.json`) is either inside that stack or selected as
an external bind.

## Why `GZIP_RSYNCABLE=yes`

GitLab packs every part of the archive with `gzip -1` (`registry.tar.gz`,
`artifacts.tar.gz`, `uploads.tar.gz`, `db/database.sql.gz`, …). Plain gzip output
changes from the first changed byte to the end, so a single new image pushed to a
local container registry made restic store the whole registry part again the next
night. On one production instance with a 27 GB registry that was about 26 GB per
night with any push, which filled the backup drive through the 30-day retention.

With `GZIP_RSYNCABLE=yes` GitLab runs `gzip --rsyncable -c -1`, which restarts the
compression at content-defined points. Unchanged files inside a `.tar.gz` part then
compress to the same bytes again and restic deduplicates them. Measured with
restic 0.19 and GNU gzip 1.12, as GitLab ships it:

| Second night | plain gzip | `--rsyncable` |
|---|---|---|
| 290 MB `.tar.gz` of 80 files, 3 new files between existing ones | 293 MB added | 25 MB added |
| 320 MB SQL dump, 0.5 % of rows changed throughout | 118 MB added | 121 MB added |

The flag helps the file parts (registry, artifacts, uploads, packages, LFS), not
the database dump: its changes are spread so evenly that every restic chunk
(about 1 MB) contains one. The archive grows by about 2 %. Restores are unaffected;
`gitlab-backup restore` reads either form with `gzip -cd`.

## Restore

GitLab requires the target container to use the **exact same GitLab version and
edition** as the archive. The restored Compose file should therefore pin the image
version; GitLab itself aborts a mismatched restore.

The restore hook:

- requires exactly one regular `*_gitlab_backup.tar`, derives its validated ID, and
  passes it as `BACKUP=...`; a stale or ambiguous archive set fails closed;
- sets `GITLAB_ASSUME_YES=1` explicitly so an unattended restore cannot wait at
  GitLab's destructive-action prompts;
- on a fresh server waits up to 30 minutes for Omnibus first-boot reconfiguration
  to finish: its Cinc/Chef process must be absent from three consecutive
  in-container probes, and a probe that cannot run at all (a container that is up
  but not yet exec-ready) resets that count instead of opening the gate;
- then requires the image's `gitlab-healthcheck` and `gitlab-rake gitlab:env:info`
  to pass twice consecutively before the import — that pair, not a log line, is
  what proves GitLab is genuinely up. Nothing in the wait is keyed on `docker logs`
  output: the container streams all of `/var/log/gitlab` to stdout, so any one-shot
  marker leaves a bounded `--tail` window within seconds and a gate built on one
  would never open;
- keeps status lines visible throughout; a stopped container fails early with its
  recent logs instead of racing reconfiguration against the destructive restore.

Only the `gitlab` Compose service is started for this hook (not Runner or DinD),
without published host ports and with automatic restart and health checks disabled
for the transient restore container. The hook deliberately does not restart Puma or
Sidekiq after importing; `docker-backup` stops the temporary restore service in its
cleanup path, avoiding even a brief window for restored jobs, email or webhooks to
leave the drill server. Restore-time Compose cleanup allows containers up to 120
seconds to stop gracefully; process-heavy Omnibus GitLab shutdowns can exceed
Compose's ten-second default, which would otherwise end in SIGKILL (exit 137) even
after a successful import.

For the cross-server flow (`--from-repo … --use-template-hooks --save-config`) see
[Restore with a custom command](hooks.md#restore-with-a-custom-command).

## Migrating an older GitLab config

Existing GitLab configs keep all previously stored settings; `docker-backup set`
cannot add the builtin template provenance or replace the complete exclude/hook set.
Before creating the first v5 manifest, recreate an older config from the current
builtin template. First run `docker-backup ls` and record its existing repository
target. Pass the repository's **parent base** to `--target` (for example,
repository `/mnt/backups/gitlab` means target base `/mnt/backups`):

```bash
sudo docker-backup ls
sudo docker-backup create /opt/gitlab --name gitlab \
  --from-template gitlab --target /mnt/backups \
  --allow-hooks --force --non-interactive
```

`--force` replaces only the local config; the existing
`/etc/docker-backup/keys/gitlab.key` is deliberately reused, so the existing
repository remains readable. If the old config used an offsite repository or
non-default scheduling, pass the corresponding `--offsite`/`--schedule` values as
well. Review the resulting config, then run one fresh backup to publish a
template-bound v5 manifest.

### Configs created before `GZIP_RSYNCABLE=yes`

A config keeps the hook commands it was created with, so an existing GitLab config
does not pick up the flag from the updated template. Set the new pre-hook and
approve it again in one step:

```bash
sudo docker-backup set gitlab --pre-cmd \
  'rm -f gitlab/data/backups/*_gitlab_backup.tar && docker exec gitlab gitlab-backup create CRON=1 GZIP_RSYNCABLE=yes' \
  --allow-hooks
```

The config then matches the template again, and the next backup publishes a
manifest whose hook fingerprint the updated template accepts. Until that backup has
run, the manifest on the drive still carries the old fingerprint: a cross-server
restore with `--use-template-hooks` and the updated tool refuses it during that
window and needs a reviewed `--restore-cmd` instead (the restore command itself did
not change). The same happens on a server that still runs the old tool and meets a
new manifest, so update the tool everywhere before relying on it.
