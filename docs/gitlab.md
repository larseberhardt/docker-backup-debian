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
| pre      | remove stale generated archives, then `gitlab-backup create CRON=1`      |
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
