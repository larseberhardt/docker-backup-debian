# Email notifications

Optionally you get an email when a backup fails. This is triggered via the systemd
hook `OnFailure=docker-backup-notify@<name>.service` — it also fires on crash,
timeout, or OOM kill, not only on a "clean" failure exit.

```bash
sudo docker-backup notify setup     # set up SMTP interactively (wizard)
sudo docker-backup notify test      # send a test email
sudo docker-backup notify show      # current config (password masked)
```

The wizard writes `/etc/docker-backup/notify.json` (mode 0600), e.g.:

```json
{
  "enabled": true,
  "method": "smtp",
  "on_failure": true,
  "on_success": false,
  "smtp": {
    "host": "smtp.example.com",
    "port": 587,
    "security": "starttls",
    "username": "backup@example.com",
    "password": "…",
    "from": "backup@example.com",
    "to": ["admin@example.com"],
    "timeout": 30
  }
}
```

- **Default: failures only.** Set `on_success` to `true` (or answer the wizard
  question with "yes") to additionally receive a short confirmation (snapshot
  ID/time) after every successful run.
- `security`: `starttls` (default, port 587), `ssl` (port 465) or `none`.
- The SMTP password can also be set, instead of inline, via `smtp.password_file`
  (path) or the environment variable `DOCKER_BACKUP_SMTP_PASSWORD` (e.g. via
  `/etc/docker-backup/notify.env`, loaded by the notify service).
- Python stdlib only (`smtplib`) — no local mail server needed.

Failure emails contain the last `journalctl` excerpt of the failed run.
