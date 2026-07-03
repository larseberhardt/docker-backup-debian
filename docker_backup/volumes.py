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
from typing import List

from . import util

HELPER_IMAGE = "busybox"


def archive_name(key: str) -> str:
    return "%s.tar" % key


def legacy_archive_name(key: str) -> str:
    return "%s.tar.gz" % key


def build_backup_cmd(real_name: str, staging_dir: str, key: str) -> List[str]:
    archive = "/backup/" + archive_name(key)
    return [
        "docker", "run", "--rm",
        "-v", "%s:/volume:ro" % real_name,
        "-v", "%s:/backup" % staging_dir,
        HELPER_IMAGE,
        "tar", "cf", archive, "-C", "/volume", ".",
    ]


def build_restore_cmd(real_name: str, staging_dir: str, archive: str) -> List[str]:
    # busybox tar does not reliably auto-detect compression → pick flags by name.
    flags = "xzf" if archive.endswith(".gz") else "xf"
    return [
        "docker", "run", "--rm",
        "-v", "%s:/volume" % real_name,
        "-v", "%s:/backup" % staging_dir,
        HELPER_IMAGE,
        "sh", "-c", "cd /volume && tar %s /backup/%s" % (flags, archive),
    ]


def backup_named_volume(real_name: str, staging_dir: str, key: str) -> None:
    if not util.DRY_RUN:
        os.makedirs(staging_dir, exist_ok=True)
    util.info("Backing up named volume %s" % real_name)
    util.run(build_backup_cmd(real_name, staging_dir, key), capture=False, mutating=True)


def restore_named_volume(real_name: str, staging_dir: str, key: str) -> None:
    archive = archive_name(key)
    legacy = legacy_archive_name(key)
    if (not os.path.exists(os.path.join(staging_dir, archive))
            and os.path.exists(os.path.join(staging_dir, legacy))):
        archive = legacy  # snapshot from an early development build (gzip)
    util.info("Restoring named volume %s" % real_name)
    util.run(build_restore_cmd(real_name, staging_dir, archive), capture=False, mutating=True)
