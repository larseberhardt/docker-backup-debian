# Restore

Goal: after the restore, only `docker compose up -d` is needed. The DB dump is
imported automatically; the stack then remains **stopped**.

!!! tip "Drill it"
    A backup you have never restored is a hope, not a backup. Test-restore every
    stack into a throwaway directory (`/opt/xibo-test`) at least once, and again
    after changing its DB scope or template.

## Runbook (same server)

**1. Stop the target stack.** Restore fails closed if any running container has a
writable bind mount at, above, or below a restore target.

```bash
cd /opt/xibo && docker compose down
```

**2. Restore.** The source config is derived from the target's base name (here
`xibo`). Restoring to a different path? Pass the source with `--from`:

```bash
sudo docker-backup restore /opt/xibo
sudo docker-backup restore /opt/xibo-test --from xibo
```

**3. Check the env files** (ports, hostnames, secrets) and start the stack:

```bash
cd /opt/xibo
docker compose up -d
```

### Options

| Option | Effect |
|---|---|
| `--from <name>` | source config (default: target base name) |
| `--snapshot <id>` | restore this snapshot instead of `latest` |
| `--force` | overwrite a non-empty target and clean-replace existing non-external named volumes |
| `--no-custom-restore` | use the built-in DB import even if the config has a restore hook |
| `--from-repo`, `--key-file` | restore from a mounted drive without a local config (below) |

`--force` does **not** approve hook commands or placement of external bind data;
both always ask for a separate default-no confirmation.

### What happens during a restore

1. Verify that no running container has a writable bind overlapping the target.
2. `restic restore --sparse latest` into a random, root-only scratch folder on the
   target filesystem. Sparse files keep their compact disk usage, and the restored
   tree can normally be moved into place without a second full copy; use a restore
   target below a root-controlled path such as `/opt`.
3. Authenticate the restored Compose model while it is still protected in scratch,
   stop/remove the exact Compose project, and verify that target and external binds
   are quiet.
4. Selected external bind paths are mapped by Compose service + container target
   to the new server, displayed, and require a separate default-no confirmation;
   an existing target is clean-replaced only with `--force`.
5. Place the stack tree (compose + env files + bind data + dumps + volume tars)
   and selected external data as clean replacements under `--force`.
6. Recreate named volumes empty and restore them from the tars. An existing volume
   fails without `--force`; `--force` deletes/recreates it only after proving no
   running or stopped container still references it. Externally managed/shared
   Compose volumes require a manual restore.
7. Per DB: start only the DB service, wait for "ready", import the dump, then
   stop/remove the DB service again (data is preserved).
8. After all imports succeed, remove the temporary dumps and volume tars; failed
   restores retain them for diagnosis or a manual retry.

Stacks with a restore hook (GitLab, …) follow a different sequence — see
[Restore with a custom command](hooks.md#restore-with-a-custom-command).

## Restore on another server

Two servers share the same mounted backup drive and you want to test-restore
server A's backup on server B — which has neither A's config nor its key. Every
backup writes a small **non-secret manifest** next to the repo
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

Notes:

- `--from-repo` is for **mounted local drives**; remote `s3:`/`sftp:` repos keep
  using `--from <name>` with a local config.
- DBs with `password_source: stored` keep their password only on server A: file
  restore is still possible, but `--save-config` refuses to create an incomplete
  unattended setup until that credential is moved to the Compose environment or
  configured locally. `env:`-based DBs (incl. Supabase) work as-is.
- The reconstructed config is ephemeral by default. Persisting it always requires
  `--use-template-hooks --save-config`; the exact local template is verified even
  when it has no hooks. Template reconstruction uses the full snapshot ID bound into
  the newest v5 manifest, not a different/older `--snapshot`.
- With `--save-config`, the schedule drop-in is restored, the key is installed into
  the managed key directory, and the timer deliberately remains disabled until you
  enable it.
- Every v5 cross-server restore requires one explicit policy choice: trusted local
  `--use-template-hooks`, a reviewed `--restore-cmd`, or `--no-custom-restore`. This
  prevents a modified plaintext sidecar from hiding that an application-specific
  restore was needed.

## Manual restore without the tool (emergency)

If the tool is not available, restic + the matching key is enough:

```bash
restic -r /mnt/backups/xibo --password-file /etc/docker-backup/keys/xibo.key snapshots
restic -r /mnt/backups/xibo --password-file /etc/docker-backup/keys/xibo.key \
       restore latest --target /restore --sparse
```

The stack is then under `/restore/opt/xibo`, dumps under `.docker-backup/dumps/`,
volume tars under `.docker-backup/volumes/`. Move the stack folder into place,
recreate named volumes from the tars, then import the DB dump manually:

```bash
docker compose up -d <db-service>
docker compose exec -T <db-service> mysql -u <user> -p'<password>' < .docker-backup/dumps/<db-service>.sql
```
