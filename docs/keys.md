# Key escrow & 3-2-1

The restic keys are located under `/etc/docker-backup/keys/<name>.key` (mode 0600,
root). **Without the matching key, a backup is unrecoverable.** If the key exists
only on the source server, it is also lost if that server is lost.

## Recommendations

1. Keep the key **additionally offline** (password manager / vault). `create`
   shows the key once; `docker-backup key show <name>` displays it again later.
2. Store an **encrypted copy** of `/etc/docker-backup/keys/` on a separate system.
3. Implement **3-2-1**: an offsite repo per stack (`--offsite <base>`) that is
   mirrored after every run via `restic copy` (different location, separate
   credentials). The same key is used for the primary and offsite repo.
4. For **ransomware-grade** protection, make the offsite repo append-only from the
   server's point of view (e.g. `rest-server --append-only` or S3 object lock):
   whoever has root on the server also has the key and could otherwise prune both
   repos.

## Offsite repo

```bash
sudo docker-backup create /opt/xibo --target /mnt/backups --offsite s3:s3.amazonaws.com/my-offsite
sudo docker-backup set xibo --offsite-retention 30/12/24   # own policy for the offsite repo
sudo docker-backup set xibo --no-offsite-prune             # keep everything offsite
```

After the copy, the offsite repo is pruned with the primary retention unless you
set its own policy (`--offsite-retention`) or opt out of pruning
(`--no-offsite-prune`). It is included in the weekly integrity check. Credentials
for the offsite backend go into `/etc/docker-backup/backends/<name>.env` like the
primary's — see [Backup target and backends](quickstart.md#backup-target-and-backends).

## Removing a stack

!!! note "`rm` is safe"
    `docker-backup rm <name>` only removes the timer + config; the key, secrets,
    and the remote repo are kept. Only `rm <name> --purge-keys` also deletes the
    local key (the remote repo is **never** touched).
