"""``rm <name>`` (alias ``remove``) — safely remove a backup setup.

Default: disable the timer, delete drop-in + config + status. **Key, secrets,
backend credentials and the remote repo are left untouched** — without the key
no restore would be possible. With ``--purge-keys`` these local secrets are
deleted too (but never the remote repo).
"""

from __future__ import annotations

import glob
import os
import shutil

from .. import config, keys, status, systemd_units, util, wizard


def cmd_remove(args) -> int:
    util.require_root()
    name = config.sanitize_name(args.name)
    if not config.exists(name):
        util.error("No config named '%s'." % name)
        return 1

    if not args.yes:
        if not wizard.confirm(
            "Remove the backup setup for '%s'? "
            "(repo data and key are preserved)" % name, default=False
        ):
            util.info("Aborted.")
            return 0

    # 1) Stop + disable the timer, remove the drop-in
    systemd_units.disable_timer(name)
    _rmtree(systemd_units.dropin_dir(name))
    systemd_units.daemon_reload()

    # 2) Config + status
    if util.DRY_RUN:
        util.info("DRY-RUN: would remove config '%s' and status." % name)
    else:
        config.delete(name)
    status.delete_status(name)

    # 3) Optional: delete local secrets
    if args.purge_keys:
        if not args.yes:
            if not wizard.confirm(
                "REALLY delete the key? Without it NO restore is possible "
                "(IRREVERSIBLE)", default=False
            ):
                util.info("Keeping key/secrets.")
            else:
                _purge_keys(name)
        else:
            _purge_keys(name)

    util.warn(
        "Repo data at the backup target and the restic key are preserved. "
        "To delete permanently, remove the repo manually (e.g. delete it at the target)."
    )
    util.info("Backup setup '%s' removed." % name)
    return 0


def _purge_keys(name: str) -> None:
    _unlink(keys.key_path(name))
    _unlink(os.path.join(config.backends_dir(), name + ".env"))
    for pw in glob.glob(os.path.join(config.secrets_dir(), name + "-*.pw")):
        _unlink(pw)
    util.warn("Key/secrets/backend credentials for '%s' deleted." % name)


def _unlink(path: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: would delete: %s" % path)
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        util.warn("Could not delete %s: %s" % (path, exc))


def _rmtree(path: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: would remove directory: %s" % path)
        return
    shutil.rmtree(path, ignore_errors=True)
