"""Calls to ``docker compose`` and parsing of ``config --format json``.

The pure functions (``collect_volume_backup_plan``, ``real_volume_name``,
``find_compose_file`` …) are testable without a running Docker.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import util

# Data directories of the DB engines (to exclude / detect the raw data dir).
# Several candidates per engine: postgres:18+ mounts the PARENT /var/lib/postgresql
# (PGDATA lives in <parent>/<major>/docker) — older images mount .../data directly.
DB_DATA_DIRS = {
    "mysql": ("/var/lib/mysql",),
    "postgres": ("/var/lib/postgresql/data", "/var/lib/postgresql"),
}


def primary_db_data_dir(engine: str) -> Optional[str]:
    """The canonical data dir of an engine (first candidate) — for config annotation."""
    targets = DB_DATA_DIRS.get(engine)
    return targets[0] if targets else None

_COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

# restic backend schemes: everything else is treated as a local path
_REMOTE_SCHEMES = (
    "s3:", "sftp:", "rest:", "b2:", "gs:", "azure:", "swift:", "rclone:",
    "http:", "https:",
)


# --- filesystem helpers (pure) ----------------------------------------------
def find_compose_file(stack_dir: str) -> Optional[str]:
    for fn in _COMPOSE_FILENAMES:
        p = os.path.join(stack_dir, fn)
        if os.path.exists(p):
            return p
    return None


def find_env_files(stack_dir: str) -> List[str]:
    """All env files in the stack folder (name varies: .env, config.env, …)."""
    out = []  # type: List[str]
    try:
        entries = os.listdir(stack_dir)
    except OSError:
        return out
    for fn in sorted(entries):
        full = os.path.join(stack_dir, fn)
        if not os.path.isfile(full):
            continue
        if fn == ".env" or fn.endswith(".env") or fn.startswith(".env"):
            out.append(full)
    return out


def is_local_repo(repo: str) -> bool:
    return not any(repo.startswith(s) for s in _REMOTE_SCHEMES)


def repo_for(base: str, name: str) -> str:
    """Appends the stack name to the target base (works for path and URL)."""
    return "%s/%s" % (base.rstrip("/"), name)


# --- docker compose calls ---------------------------------------------------
def _base(compose_file: str, project_dir: str, project_name: Optional[str] = None) -> List[str]:
    argv = ["docker", "compose", "-f", compose_file, "--project-directory", project_dir]
    if project_name:
        argv += ["-p", project_name]
    return argv


def config_json(
    compose_file: str, project_dir: str, project_name: Optional[str] = None
) -> Dict[str, Any]:
    """``docker compose config --format json`` — resolves env_file + ${VARS}."""
    argv = _base(compose_file, project_dir, project_name) + ["config", "--format", "json"]
    proc = util.run(argv, capture=True)
    return json.loads(proc.stdout)


def ls_json(all_stacks: bool = False) -> List[Dict[str, Any]]:
    argv = ["docker", "compose", "ls", "--format", "json"]
    if all_stacks:
        argv += ["--all"]
    proc = util.run(argv, capture=True)
    data = json.loads(proc.stdout or "[]")
    return data if isinstance(data, list) else []


def _parse_ps(stdout: str) -> List[Dict[str, Any]]:
    """`docker compose ps --format json` returns an array OR NDJSON depending on the version."""
    out = []  # type: List[Dict[str, Any]]
    stdout = (stdout or "").strip()
    if not stdout:
        return out
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def service_running(
    compose_file: str, project_dir: str, service: str, project_name: Optional[str] = None
) -> bool:
    argv = _base(compose_file, project_dir, project_name) + ["ps", "--format", "json", service]
    proc = util.run(argv, capture=True, check=False)
    for s in _parse_ps(proc.stdout):
        if str(s.get("State", "")).lower() == "running":
            return True
    return False


def up_service(compose_file, project_dir, service, project_name=None) -> None:
    util.run(_base(compose_file, project_dir, project_name) + ["up", "-d", service],
             mutating=True, capture=False)


def up_all(compose_file, project_dir, project_name=None) -> None:
    """Brings up the ENTIRE stack (``docker compose up -d``).

    Needed for the custom restore command: a ``docker exec <svc> …`` hook needs
    a running container, whereas the built-in restore leaves the stack stopped.
    """
    util.run(_base(compose_file, project_dir, project_name) + ["up", "-d"],
             mutating=True, capture=False)


def stop_service(compose_file, project_dir, service, project_name=None) -> None:
    util.run(_base(compose_file, project_dir, project_name) + ["stop", service],
             mutating=True, capture=False, check=False)


def rm_service(compose_file, project_dir, service, project_name=None) -> None:
    # -s stops it first, -f without prompting. Volumes/data are preserved.
    util.run(_base(compose_file, project_dir, project_name) + ["rm", "-f", "-s", service],
             mutating=True, capture=False, check=False)


def exec_args(
    compose_file: str,
    project_dir: str,
    service: str,
    command: List[str],
    env: Optional[Dict[str, str]] = None,
    tty: bool = False,
    project_name: Optional[str] = None,
) -> List[str]:
    """Builds the argv for ``docker compose exec`` (without running it → testable)."""
    argv = _base(compose_file, project_dir, project_name) + ["exec"]
    if not tty:
        argv += ["-T"]
    for k, v in (env or {}).items():
        argv += ["-e", "%s=%s" % (k, v)]
    argv += [service] + list(command)
    return argv


def create_volume(real_name: str) -> None:
    util.run(["docker", "volume", "create", real_name], mutating=True, capture=True, check=False)


# --- volume analysis (pure, without Docker) ---------------------------------
def real_volume_name(top_volumes: Dict[str, Any], key: str, project: Optional[str]) -> str:
    entry = top_volumes.get(key) if isinstance(top_volumes, dict) else None
    if isinstance(entry, dict) and entry.get("name"):
        return entry["name"]
    if project:
        return "%s_%s" % (project, key)
    return key


def collect_volume_backup_plan(
    compose_json: Dict[str, Any], db_services: List[Dict[str, Any]]
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Plans file backup vs. logical DB dump.

    Annotates each ``db_services`` entry with ``raw_data_exclude`` (absolute
    bind path of the raw data dir) and returns:
      - ``exclude_paths``  : raw DB bind directories → restic ``--exclude``
      - ``named_volumes``  : named volumes (except the DB data dir) → back up via tar
    """
    db_data_targets = {}  # type: Dict[str, Tuple[str, ...]]
    for db in db_services:
        db_data_targets[db["service"]] = DB_DATA_DIRS.get(db["engine"]) or ()
        db.setdefault("data_dir_target", primary_db_data_dir(db["engine"]))
        db.setdefault("raw_data_exclude", None)

    services = compose_json.get("services") or {}
    top = compose_json.get("volumes") or {}
    project = compose_json.get("name")

    exclude_paths = []  # type: List[str]
    named_volumes = []  # type: List[Dict[str, Any]]

    for svc_name, svc in services.items():
        data_targets = db_data_targets.get(svc_name) or ()
        for vol in (svc.get("volumes") or []):
            vtype = vol.get("type")
            target = vol.get("target")
            source = vol.get("source")
            is_db_data = target in data_targets

            if vtype == "bind":
                if is_db_data and source:
                    exclude_paths.append(source)
                    for db in db_services:
                        if db["service"] == svc_name:
                            db["raw_data_exclude"] = source
                            db["data_dir_target"] = target
                # other bind mounts live in the stack folder → covered by the file backup
            elif vtype == "volume":
                if is_db_data:
                    # raw named volume of the DB: do NOT tar it (the logical dump replaces it)
                    continue
                if not source:
                    # anonymous volume (e.g. '- /data'): no stable name → cannot be
                    # backed up/restored by name; the image recreates it on start.
                    continue
                named_volumes.append({
                    "key": source,
                    "real_name": real_volume_name(top, source, project),
                    "target": target,
                    "service": svc_name,
                })

    # deduplicate named volumes (several services can use the same one)
    seen = set()  # type: set
    uniq = []  # type: List[Dict[str, Any]]
    for nv in named_volumes:
        if nv["key"] in seen:
            continue
        seen.add(nv["key"])
        uniq.append(nv)

    # deduplicate exclude paths, preserve order
    excl_seen = set()  # type: set
    excl = []  # type: List[str]
    for p in exclude_paths:
        if p not in excl_seen:
            excl_seen.add(p)
            excl.append(p)

    return excl, uniq


# Bind sources that are system plumbing, not stack data → never treated as
# external data paths.
_EXTERNAL_BIND_IGNORE_PREFIXES = ("/dev/", "/proc/", "/sys/", "/run/", "/var/run/", "/tmp/")
_EXTERNAL_BIND_IGNORE_FILES = ("/etc/localtime", "/etc/timezone", "/etc/machine-id",
                               "/etc/hosts", "/etc/resolv.conf")


def find_external_binds(
    compose_json: Dict[str, Any], stack_path: str,
    exclude_paths: Optional[List[str]] = None,
) -> List[str]:
    """Bind-mount sources OUTSIDE the stack folder — NOT covered by the file backup.

    The file backup only archives the stack folder; a bind like ``/srv/appdata:/data``
    would silently be lost. ``create`` records these paths as ``extra_backup_paths``
    (after a prompt/warning) so they end up in the snapshot too. Skipped: sources
    inside the stack folder, raw DB data dirs (already in ``exclude_paths``), system
    paths (docker.sock, /etc/localtime, /dev …) and special files (sockets/devices).
    """
    base = os.path.abspath(stack_path or "").rstrip("/")
    excluded = set(exclude_paths or [])
    out = []  # type: List[str]
    seen = set()  # type: set
    for svc in (compose_json.get("services") or {}).values():
        for vol in (svc.get("volumes") or []):
            if vol.get("type") != "bind":
                continue
            src = vol.get("source")
            if not src:
                continue
            src = os.path.abspath(src)
            if src == base or src.startswith(base + os.sep):
                continue  # inside the stack folder → covered by the file backup
            if src in seen or src in excluded:
                continue
            if src in _EXTERNAL_BIND_IGNORE_FILES or os.path.basename(src) == "docker.sock":
                continue
            if any(src.startswith(p) for p in _EXTERNAL_BIND_IGNORE_PREFIXES):
                continue
            if os.path.exists(src) and not (os.path.isdir(src) or os.path.isfile(src)):
                continue  # socket/device/fifo
            seen.add(src)
            out.append(src)
    return out
