"""Management of the restic repo keys under /etc/docker-backup/keys/.

One key per stack, 0600, readable only by root. Also reused for the
offsite repo.
"""

from __future__ import annotations

import os
import secrets
import stat

from . import config
from .util import warn


def key_path(name: str) -> str:
    return os.path.join(config.keys_dir(), config.sanitize_name(name) + ".key")


def ensure_key(name: str) -> str:
    """Generates a new key if needed; existing keys are reused.

    Important: NEVER regenerate an existing key — that would make the
    associated restic repo inaccessible.
    """
    config.ensure_dirs()
    path = key_path(name)
    if os.path.exists(path):
        _check_perms(path)
        return path
    token = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)
    return path


def read_key(name: str) -> str:
    with open(key_path(name)) as f:
        return f.read().strip()


def _check_perms(path: str) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        warn(
            "Key file %s is readable by group/others (mode %o); "
            "setting it to 0600." % (path, mode)
        )
        try:
            os.chmod(path, 0o600)
        except (PermissionError, OSError):
            pass


def escrow_notice(name: str, key_value: str) -> str:
    """Notice printed after `create` (key escrow / 3-2-1)."""
    return (
        "\n"
        "============================================================\n"
        " IMPORTANT: back up the restic key (key escrow)\n"
        "============================================================\n"
        " Stack:        %s\n"
        " Key:          %s\n"
        " File:         %s\n"
        "\n"
        " Without this key the backups are NOT restorable.\n"
        " If the key only lives on this server, it is lost too if the\n"
        " server is lost. Keep an additional offline copy\n"
        " (password manager / vault / encrypted offsite).\n"
        "============================================================\n"
        % (name, key_value, key_path(name))
    )
