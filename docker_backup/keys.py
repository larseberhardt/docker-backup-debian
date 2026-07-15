"""Management of the restic repo keys under /etc/docker-backup/keys/.

One key per stack, 0600, readable only by root. Also reused for the
offsite repo.
"""

from __future__ import annotations

import errno
import hmac
import os
import secrets
import stat
import tempfile

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


def install_existing_key(name: str, source: str) -> str:
    """Install a supplied restore key into the managed key directory safely.

    Existing identical keys are reused.  A differing destination is never
    overwritten: silently replacing it could strand another repository.  The
    hard-link publication is atomic and avoids an overwrite race.
    """
    data = _read_regular_key(source)

    config.ensure_dirs()
    target = key_path(name)
    if os.path.lexists(target):
        existing = _read_regular_key(target)
        if not hmac.compare_digest(existing, data):
            raise OSError(
                "Managed key already exists with different contents: %s" % target
            )
        _secure_managed_key(target)
        return target

    tmp_fd, tmp = tempfile.mkstemp(dir=config.keys_dir(), prefix=".%s." % name,
                                   suffix=".key.tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, target)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            existing = _read_regular_key(target)
            if not hmac.compare_digest(existing, data):
                raise OSError(
                    "Managed key appeared with different contents: %s" % target
                )
        _secure_managed_key(target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return target


def _read_regular_key(path: str) -> bytes:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OSError("Restic key must be a regular, non-symlink file: %s" % path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if (not stat.S_ISREG(current.st_mode)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise OSError("Restic key changed while it was opened: %s" % path)
        if current.st_size <= 0 or current.st_size > 16384:
            raise OSError("Restic key has an invalid size: %s" % path)
        with os.fdopen(fd, "rb") as f:
            fd = -1
            return f.read(16385)
    finally:
        if fd >= 0:
            os.close(fd)


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


def _secure_managed_key(path: str) -> None:
    """Enforce and verify the hard guarantee used by restored configs."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("Managed restic key is not a regular file: %s" % path)
        if hasattr(os, "geteuid") and os.geteuid() == 0 and before.st_uid != 0:
            os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o600)
        current = os.fstat(fd)
        if stat.S_IMODE(current.st_mode) != 0o600:
            raise OSError("Could not enforce mode 0600 on managed key: %s" % path)
        if hasattr(os, "geteuid") and os.geteuid() == 0 and current.st_uid != 0:
            raise OSError("Managed restic key is not owned by root: %s" % path)
    finally:
        os.close(fd)


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
