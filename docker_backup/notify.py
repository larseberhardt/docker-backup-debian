"""Email notifications via SMTP (standard library only).

Global config under /etc/docker-backup/notify.json (mode 0600). Default:
notify only on failures; success messages can be enabled optionally
(``on_success``).

Failures are triggered via the systemd hook ``OnFailure=docker-backup-notify@%i.service``
(robust even on crash/timeout/OOM); success messages are sent by ``run`` itself.
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from . import config, util


def notify_config_path() -> str:
    return os.path.join(config.etc_dir(), "notify.json")


def load() -> Optional[Dict[str, Any]]:
    path = notify_config_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save(cfg: Dict[str, Any]) -> str:
    config.ensure_dirs()
    path = notify_config_path()
    data = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return path


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown-host"


def _recipients(smtp: Dict[str, Any]) -> List[str]:
    to = smtp.get("to") or []
    if isinstance(to, str):
        to = [t.strip() for t in to.split(",") if t.strip()]
    return [t for t in to if t]


def _smtp_password(smtp: Dict[str, Any]) -> Optional[str]:
    # order: ENV override -> file -> inline in the config
    env_pw = os.environ.get("DOCKER_BACKUP_SMTP_PASSWORD")
    if env_pw:
        return env_pw
    pf = smtp.get("password_file")
    if pf and os.path.exists(pf):
        with open(pf) as f:
            return f.read().strip()
    return smtp.get("password")


def send(subject: str, body: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Sends an email. Returns True on success/dry-run, otherwise False."""
    cfg = cfg if cfg is not None else load()
    if not cfg or not cfg.get("enabled", True):
        util.debug("Notification disabled or not configured.")
        return False

    smtp = cfg.get("smtp") or {}
    to = _recipients(smtp)
    sender = smtp.get("from") or smtp.get("username")
    if not to or not sender:
        util.warn("Notification: 'from'/'to' missing in notify.json — skipped.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    host = smtp.get("host")
    port = int(smtp.get("port") or 587)
    security = (smtp.get("security") or "starttls").lower()
    timeout = float(smtp.get("timeout") or 30)
    username = smtp.get("username")
    password = _smtp_password(smtp)
    if password:
        util.register_secret(password)

    if util.DRY_RUN:
        util.info("DRY-RUN: would send email to %s: %s" % (", ".join(to), subject))
        return True

    if not host:
        util.warn("Notification: 'smtp.host' missing — skipped.")
        return False

    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(msg)
        util.info("Notification sent to %s" % ", ".join(to))
        return True
    except Exception as exc:  # SMTP/network/SSL — never fail hard
        util.error("Email delivery failed: %s" % exc)
        return False


def journal_tail(name: str, lines: int = 80) -> str:
    try:
        proc = util.run(
            ["journalctl", "-u", "docker-backup@%s.service" % name,
             "-n", str(lines), "--no-pager"],
            capture=True, check=False,
        )
        return proc.stdout or ""
    except util.CommandError:
        return "(journalctl not available)"


def notify_failure(name: str) -> bool:
    cfg = load()
    if not cfg or not cfg.get("enabled", True) or not cfg.get("on_failure", True):
        return False
    host = _hostname()
    log = journal_tail(name)
    subject = "[docker-backup] FAILURE: stack '%s' on %s" % (name, host)
    body = (
        "The backup for stack '%s' on host '%s' FAILED.\n\n"
        "Last log output:\n"
        "------------------------------------------------------------\n"
        "%s\n"
        "------------------------------------------------------------\n\n"
        "Details: journalctl -u docker-backup@%s.service\n"
    ) % (name, host, util.scrub(log.strip()), name)
    return send(subject, body, cfg)


def notify_success(name: str, summary: str = "") -> bool:
    cfg = load()
    if not cfg or not cfg.get("enabled", True) or not cfg.get("on_success", False):
        return False
    host = _hostname()
    subject = "[docker-backup] OK: stack '%s' on %s" % (name, host)
    body = "Backup for stack '%s' on host '%s' completed successfully.\n\n%s\n" % (
        name, host, summary)
    return send(subject, body, cfg)


def send_test(cfg: Optional[Dict[str, Any]] = None) -> bool:
    host = _hostname()
    return send(
        "[docker-backup] test notification from %s" % host,
        "This is a test email from docker-backup on host '%s'.\n"
        "If you receive it, SMTP delivery works.\n" % host,
        cfg,
    )
