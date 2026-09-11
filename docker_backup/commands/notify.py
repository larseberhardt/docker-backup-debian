"""``notify`` — set up, test and show email notifications.

``notify failure <name>`` is called internally by the systemd OnFailure hook.
"""

from __future__ import annotations

import json

from .. import notify as core
from .. import util, wizard


def cmd_notify(args) -> int:
    # Dispatch on the sub-action here as well: on Python 3.9.8/3.10.0 exactly, a
    # parent set_defaults(func=) overrides the sub-parser's (bpo-45235) — and
    # 'notify failure' is the systemd OnFailure entry point.
    action = getattr(args, "notify_action", None)
    if action == "setup":
        return cmd_setup(args)
    if action == "test":
        return cmd_test(args)
    if action == "show":
        return cmd_show(args)
    if action == "failure":
        return cmd_failure(args)
    util.error("Please specify an action: setup | test | show | failure <name>")
    return 1


def cmd_setup(args) -> int:
    util.require_root()
    existing = core.load() or {}
    smtp = existing.get("smtp") or {}

    util.info("Set up SMTP notification (Enter = default in parentheses).")
    host = wizard.prompt("SMTP host", default=smtp.get("host"))
    if not host:
        util.error("No SMTP host given — aborted.")
        return 1
    port_raw = wizard.prompt("SMTP port", default=str(smtp.get("port") or 587))
    try:
        port = int(port_raw)
    except ValueError:
        util.error("Invalid SMTP port: %r — aborted." % port_raw)
        return 1
    if not 1 <= port <= 65535:
        util.error("SMTP port must be between 1 and 65535 — aborted.")
        return 1
    security = wizard.prompt("Security (starttls/ssl/none)",
                             default=(smtp.get("security") or "starttls")).strip().lower()
    if security not in core.SECURITY_MODES:
        util.error("Security must be one of %s — aborted." % "/".join(core.SECURITY_MODES))
        return 1
    if security == "none":
        util.warn("Security 'none': the SMTP login and mail content travel unencrypted.")

    username = wizard.prompt("SMTP user (empty = no login)",
                             default=smtp.get("username") or "")
    password = smtp.get("password") or None
    if username:
        entered = wizard.prompt_secret("SMTP password (empty = leave unchanged)")
        if entered:
            password = entered
    else:
        password = None  # no login → do not keep a stale password around

    sender = wizard.prompt("Sender (From)", default=smtp.get("from") or username)
    to_default = ", ".join(smtp.get("to") or [])
    to_raw = wizard.prompt("Recipients (To, several with comma)", default=to_default)
    to = [t.strip() for t in to_raw.split(",") if t.strip()]
    if not to:
        util.error("No recipient given — aborted.")
        return 1

    on_success = wizard.confirm(
        "Send an email on SUCCESS too? (default: only on failures)",
        default=bool(existing.get("on_success", False)),
    )

    cfg = {
        "enabled": True,
        "method": "smtp",
        "on_failure": True,
        "on_success": on_success,
        "smtp": {
            "host": host,
            "port": port,
            "security": security,
            "username": username or None,
            "password": password,
            "from": sender,
            "to": to,
            "timeout": 30,
        },
    }
    path = core.save(cfg)
    util.info("Notification config saved: %s (mode 0600)" % path)
    util.info("Triggers: failure=always, success=%s" % ("on" if on_success else "off"))

    if wizard.confirm("Send a test email now?", default=True):
        return 0 if core.send_test(cfg) else 1
    return 0


def cmd_test(args) -> int:
    util.require_root()
    cfg = core.load()
    if not cfg:
        util.error("No config. Run 'docker-backup notify setup' first.")
        return 1
    return 0 if core.send_test(cfg) else 1


def cmd_show(args) -> int:
    util.require_root()
    cfg = core.load()
    if not cfg:
        print("No notification configured (notify.json missing).")
        return 0
    redacted = json.loads(json.dumps(cfg))
    smtp = redacted.get("smtp") or {}
    if smtp.get("password"):
        smtp["password"] = "***"
    print(json.dumps(redacted, indent=2, ensure_ascii=False))
    return 0


def cmd_failure(args) -> int:
    # Called by the systemd OnFailure hook — must stay quiet and never fail hard.
    try:
        util.require_root()
        core.notify_failure(args.name)
    except Exception as exc:
        util.error("Failure notification failed: %s" % exc)
    return 0
