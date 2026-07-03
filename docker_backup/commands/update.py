"""``update`` — update docker-backup from the git repo.

Thin wrapper: replaces the process via ``os.execv`` with ``update.sh``. This
preserves the TTY for the confirmation and ensures no stale Python module is
still loaded while ``install.sh`` overwrites the ``docker_backup`` package.
"""

from __future__ import annotations

import os

from .. import selfupdate, util


def cmd_update(args) -> int:
    util.require_root()
    script = selfupdate.UPDATE_SCRIPT
    if not os.path.exists(script):
        util.error(
            "update.sh not found (%s). Was docker-backup installed via install.sh "
            "from a git checkout?" % script
        )
        return 1

    argv = [script]
    if getattr(args, "check", False):
        argv.append("--check")
    if getattr(args, "yes", False):
        argv.append("--yes")
    if getattr(args, "branch", None):
        argv += ["--branch", args.branch]

    # Replaces the running process — does not return on success.
    os.execv(script, argv)
    return 0  # only reachable if execv fails (e.g. missing permissions)
