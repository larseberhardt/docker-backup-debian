"""Self-describing repo manifest for cross-server restore.

During a backup, a small, NON-secret JSON manifest is written next to the restic
repo on the (mounted) backup drive. A second server can use it to restore
directly from the repo without a local ``/etc`` config::

    docker-backup restore <target> --from-repo <repo-path> --key-file <key>

The manifest deliberately contains NO secrets: DB passwords appear only as a
source reference (``password_source``), never as a value. The restic key is left
out and must be provided separately (``--key-file``) — this keeps the restic
encryption of the drive effective.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import compose, config, util

MANIFEST_SCHEMA_VERSION = 3  # v3: + extra_backup_paths (external bind mounts)
MANIFEST_BASENAME = "docker-backup.manifest.json"


# --- paths ------------------------------------------------------------------
def manifest_path(repo: str) -> str:
    return os.path.join(repo, MANIFEST_BASENAME)


# --- derive / write ---------------------------------------------------------
def derive(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Non-secret subset of the config for the manifest.

    Deliberately omitted: ``key_file``, ``backend_env_file``, ``repo`` (only as
    ``source_repo`` diagnostics), ``offsite``, ``schedule``, ``staging_dir``,
    ``mount_check``, ``env_files`` — all source-server-local, secret, or
    irrelevant for a restore on another server. ``compose_file`` is reduced to
    its basename (restore.py recomputes it against the target).
    """
    # HARD INVARIANT: 'hooks' / restore shell are NEVER written into the manifest.
    # The manifest is a NON-encrypted plaintext file next to the repo; a
    # root-executable command must not end up there (tamperable). For a
    # cross-server restore the restore command is passed via --restore-cmd.
    # A regression test guards this.
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "config_schema_version": cfg.get("schema_version"),
        "name": cfg["name"],
        "stack_path": cfg["stack_path"],
        "compose_file": os.path.basename(cfg["compose_file"]),
        "project_name": cfg.get("project_name"),
        "db_services": cfg.get("db_services") or [],
        "named_volumes": cfg.get("named_volumes") or [],
        "exclude_paths": cfg.get("exclude_paths") or [],
        "extra_backup_paths": cfg.get("extra_backup_paths") or [],
        "exclude_patterns": cfg.get("exclude_patterns") or [],
        "db_autodetect": cfg.get("db_autodetect", True),
        "retention": cfg.get("retention") or dict(config.DEFAULT_RETENTION),
        "created_by_host": _hostname(),
        "written_at": _utcnow(),
        "source_repo": cfg.get("repo"),
    }


def write(cfg: Dict[str, Any]) -> Optional[str]:
    """Writes the manifest next to the repo. Best-effort: NEVER breaks the backup run.

    Only for local repos (path); skipped for restic URLs (s3:, sftp: …), since no
    side-car file can be placed there. Atomic via tempfile + os.replace
    (like ``config.save``).
    """
    repo = cfg.get("repo") or ""
    if not compose.is_local_repo(repo):
        util.info("Repo '%s' is not a local path — manifest skipped." % repo)
        return None
    path = manifest_path(repo)
    if util.DRY_RUN:
        util.info("DRY-RUN: would write manifest to %s" % path)
        return None
    try:
        os.makedirs(repo, exist_ok=True)
        data = json.dumps(derive(cfg), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=repo, prefix=".manifest.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
            tmp = ""  # moved successfully
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        util.info("Manifest written: %s" % path)
        return path
    except OSError as exc:
        # Must not break the backup run (defensive like _short_id).
        util.warn("Manifest could not be written (%s): %s" % (path, exc))
        return None


# --- read -------------------------------------------------------------------
def read(repo: str) -> Optional[Dict[str, Any]]:
    return read_at(manifest_path(repo))


def read_at(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cfg_from_manifest(
    man: Dict[str, Any], repo: str, key_file: str, name: Optional[str] = None
) -> Dict[str, Any]:
    """Builds an in-memory config from a manifest, as restore.py expects it.

    ``repo`` (= --from-repo) and ``key_file`` are set so that the restore works
    regardless of where the drive is mounted on server B.
    ``mount_check``/``backend_env_file`` are source-server-specific → None.
    """
    return {
        "schema_version": man.get("config_schema_version"),
        "name": config.sanitize_name(name or man["name"]),
        "stack_path": man["stack_path"],
        "compose_file": man["compose_file"],  # basename; restore.py computes against the target
        "project_name": man.get("project_name"),
        "db_services": man.get("db_services") or [],
        "named_volumes": man.get("named_volumes") or [],
        "exclude_paths": man.get("exclude_paths") or [],
        "extra_backup_paths": man.get("extra_backup_paths") or [],
        "exclude_patterns": man.get("exclude_patterns") or [],
        "db_autodetect": man.get("db_autodetect", True),
        # The manifest deliberately carries NO shell: empty hooks, not approved. A custom
        # restore command is passed via --restore-cmd for a cross-server restore.
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        "hooks_allowed": False,
        "hooks_fingerprint": None,
        "repo": repo,
        "key_file": key_file,
        "backend_env_file": None,
        "mount_check": None,
        "retention": man.get("retention") or dict(config.DEFAULT_RETENTION),
    }


def find_manifests(base: str, max_depth: int = 2) -> List[Tuple[str, Dict[str, Any]]]:
    """Finds repos with a manifest under ``base`` (for ``ls --on-repo``).

    Scans ``<base>`` up to depth ``max_depth`` for ``docker-backup.manifest.json``.
    A recognized repo is not descended into further (no nested repo) — this saves
    walking the restic internals (data/, index/ …).
    """
    out = []  # type: List[Tuple[str, Dict[str, Any]]]
    base = os.path.abspath(base)
    if os.path.isdir(base):
        _scan(base, max_depth, out)
    out.sort(key=lambda rm: (rm[1].get("name") or "", rm[0]))
    return out


def _scan(d: str, depth: int, out: List[Tuple[str, Dict[str, Any]]]) -> None:
    man = read_at(os.path.join(d, MANIFEST_BASENAME))
    if man is not None:
        out.append((d, man))
        return  # a repo contains no further repo → do not descend
    if depth <= 0:
        return
    try:
        entries = sorted(os.listdir(d))
    except OSError:
        return
    for entry in entries:
        sub = os.path.join(d, entry)
        if os.path.isdir(sub):
            _scan(sub, depth - 1, out)


# --- small helpers ----------------------------------------------------------
def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown-host"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
