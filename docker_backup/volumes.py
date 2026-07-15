"""Back up/restore Docker named volumes via a temporary busybox container.

Bind mounts live in the stack folder and are already covered by the file backup;
this only deals with named volumes that live outside the folder.

Archives are deliberately UNCOMPRESSED (``.tar``): restic chunks and compresses
(repo v2) itself — a gzip layer would change the whole file on every run and
force a full re-upload instead of deduplicated increments. Restore still accepts
legacy ``.tar.gz`` archives (from snapshots made by early development builds).
"""

from __future__ import annotations

import os
import stat
import tarfile
from typing import List

from . import compose, util

HELPER_IMAGE = "busybox"


def archive_name(key: str) -> str:
    return "%s.tar" % key


def legacy_archive_name(key: str) -> str:
    return "%s.tar.gz" % key


def build_backup_cmd(real_name: str, staging_dir: str, key: str) -> List[str]:
    # Stream to a host FD owned by the parent process. Giving this helper a
    # writable host bind would let container root replace arbitrary staging
    # entries if the host path changed or was pre-seeded with a symlink.
    return [
        "docker", "run", "--rm",
        "-v", "%s:/volume:ro" % real_name,
        HELPER_IMAGE,
        "tar", "cf", "-", "-C", "/volume", ".",
    ]


def build_restore_cmd(real_name: str, staging_dir: str, archive: str) -> List[str]:
    # busybox tar does not reliably auto-detect compression → pick flags by name.
    # Do not route manifest-derived archive names through ``sh -c``.  A direct argv
    # also makes metacharacters inert; basename validation closes path traversal.
    if (not archive or os.path.basename(archive) != archive
            or archive in (".", "..") or "\0" in archive):
        raise ValueError("Invalid named-volume archive: %r" % archive)
    flags = "xzf" if archive.endswith(".gz") else "xf"
    return [
        "docker", "run", "--rm", "-i",
        "-v", "%s:/volume" % real_name,
        HELPER_IMAGE,
        "tar", flags, "-", "-C", "/volume",
    ]


def backup_named_volume(
    real_name: str, staging_dir: str, key: str, *, staging_fd=None,
) -> None:
    util.info("Backing up named volume %s" % real_name)
    if util.DRY_RUN:
        util.run(
            build_backup_cmd(real_name, staging_dir, key),
            capture=False, mutating=True,
        )
        return
    before = compose.volume_identity(real_name)
    if before is None:
        raise util.CommandError(
            ["docker", "volume", "inspect", real_name], 1,
            "Named volume does not exist; refusing Docker's implicit creation "
            "of an empty backup source.",
        )
    owned_staging_fd = archive_fd = -1
    filename = archive_name(key)
    try:
        if (os.path.basename(filename) != filename or filename in (".", "..")
                or "\0" in filename):
            raise ValueError("Invalid named-volume archive: %r" % filename)
        if staging_fd is None:
            owned_staging_fd = _open_staging_dir(staging_dir)
            staging_fd = owned_staging_fd
        archive_fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=staging_fd,
        )
        os.fchmod(archive_fd, 0o600)
        with os.fdopen(archive_fd, "wb", closefd=False) as stream:
            util.run(
                build_backup_cmd(real_name, staging_dir, key),
                stdout=stream, text=False, capture=True, mutating=True,
            )
            stream.flush()
            os.fsync(archive_fd)
        if os.fstat(archive_fd).st_size == 0:
            raise util.CommandError(
                ["volume-backup", real_name], 1,
                "Named-volume tar stream is empty/truncated.",
            )
        after = compose.volume_identity(real_name)
        if after != before:
            raise util.CommandError(
                ["docker", "volume", "inspect", real_name], 1,
                "Named volume identity changed during backup; the archive is "
                "not accepted.",
            )
    except Exception:
        if staging_fd is not None:
            try:
                os.unlink(filename, dir_fd=staging_fd)
            except OSError:
                pass
        raise
    finally:
        if archive_fd >= 0:
            os.close(archive_fd)
        if owned_staging_fd >= 0:
            os.close(owned_staging_fd)


def _open_staging_dir(path: str) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe no-follow directory opens are unsupported")
    absolute = os.path.abspath(path)
    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(os.path.sep, flags)
    try:
        for part in (part for part in absolute.split(os.path.sep) if part):
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_archive(staging_fd: int, key: str):
    archive = archive_name(key)
    legacy = legacy_archive_name(key)
    for candidate in (archive, legacy):
        try:
            before = os.stat(candidate, dir_fd=staging_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(before.st_mode):
            raise util.CommandError(
                ["volume-restore", candidate], 1,
                "Named-volume archive is not a regular file.",
            )
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging_fd)
        current = os.fstat(fd)
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(current.st_mode)):
            os.close(fd)
            raise util.CommandError(
                ["volume-restore", candidate], 1,
                "Named-volume archive changed while opening.",
            )
        return candidate, fd
    raise util.CommandError(
        ["volume-restore", archive], 1, "Named-volume archive is missing.",
    )


def validate_archive_fd(archive_fd: int, archive: str) -> None:
    """Read every tar header before any destination volume is mutated."""
    try:
        os.lseek(archive_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(archive_fd), "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r:*") as tar:
                for member in tar:
                    name = member.name
                    parts = [part for part in name.split("/") if part not in ("", ".")]
                    if name.startswith("/") or ".." in parts:
                        raise ValueError("archive member escapes the volume: %r" % name)
                    if member.issym() or member.islnk():
                        link = member.linkname
                        link_parts = [
                            part for part in link.split("/") if part not in ("", ".")
                        ]
                        if link.startswith("/") or ".." in link_parts:
                            raise ValueError(
                                "archive link escapes the volume: %r" % link
                            )
        os.lseek(archive_fd, 0, os.SEEK_SET)
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise util.CommandError(
            ["volume-restore", archive], 1,
            "Named-volume archive failed validation: %s" % exc,
        )


def restore_named_volume(
    real_name: str, staging_dir: str, key: str, *, staging_fd=None,
    archive_fd=None, archive: str = "",
) -> None:
    archive = archive or archive_name(key)
    util.info("Restoring named volume %s" % real_name)
    if util.DRY_RUN:
        util.run(
            build_restore_cmd(real_name, staging_dir, archive),
            capture=False, mutating=True,
        )
        return
    owned_staging_fd = -1
    owned_archive_fd = -1
    try:
        if archive_fd is None:
            if staging_fd is None:
                owned_staging_fd = _open_staging_dir(staging_dir)
                staging_fd = owned_staging_fd
            archive, owned_archive_fd = _open_archive(staging_fd, key)
            archive_fd = owned_archive_fd
            validate_archive_fd(archive_fd, archive)
        os.lseek(archive_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(archive_fd), "rb") as stream:
            util.run(
                build_restore_cmd(real_name, staging_dir, archive),
                stdin=stream, text=False, capture=False, mutating=True,
            )
    finally:
        if owned_archive_fd >= 0:
            os.close(owned_archive_fd)
        if owned_staging_fd >= 0:
            os.close(owned_staging_fd)
