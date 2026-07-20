"""Calls to ``docker compose`` and parsing of ``config --format json``.

The pure functions (``collect_volume_backup_plan``, ``real_volume_name``,
``find_compose_file`` …) are testable without a running Docker.
"""

from __future__ import annotations

import json
import os
import re
import secrets
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

# Process-heavy restore containers such as Omnibus GitLab can need materially
# longer than Compose's ten-second default to stop cleanly.  Give them a bounded
# grace period before Docker resorts to SIGKILL while preserving the existing
# post-down verification below.
RESTORE_CLEANUP_TIMEOUT_SECONDS = 120


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
    compose_file: str, project_dir: str, project_name: Optional[str] = None,
    *, env: Optional[Dict[str, str]] = None, env_replace: bool = False,
) -> Dict[str, Any]:
    """``docker compose config --format json`` — resolves env_file + ${VARS}."""
    argv = _base(compose_file, project_dir, project_name) + ["config", "--format", "json"]
    proc = util.run(argv, capture=True, env=env, env_replace=env_replace)
    return json.loads(proc.stdout)


def ls_json(all_stacks: bool = False) -> List[Dict[str, Any]]:
    argv = ["docker", "compose", "ls", "--format", "json"]
    if all_stacks:
        argv += ["--all"]
    proc = util.run(argv, capture=True)
    data = json.loads(proc.stdout or "[]")
    return data if isinstance(data, list) else []


def running_writable_bind_mounts_overlapping(path: str) -> List[Dict[str, str]]:
    """Return running containers whose writable bind overlaps ``path``.

    A restore scratch directory may live inside the target filesystem. Container
    root bypasses host directory modes, so any running container with a writable
    bind at, above, or below the target could alter authenticated scratch data or
    live target data while the restore is running. Inspect the daemon directly;
    Compose project membership alone is not sufficient because another project
    can bind the same host tree.
    """
    target = os.path.realpath(os.path.normpath(path))
    listed = util.run(
        ["docker", "container", "ls", "--quiet", "--no-trunc"],
        capture=True,
    )
    ids = [line.strip() for line in (listed.stdout or "").splitlines() if line.strip()]
    if not ids:
        return []
    if any(not all(c in "0123456789abcdefABCDEF" for c in container_id)
           for container_id in ids):
        raise util.CommandError(
            ["docker", "container", "ls"], 1,
            "Docker returned an invalid running-container id.",
        )
    inspected = util.run(
        ["docker", "container", "inspect"] + ids,
        capture=True,
    )
    try:
        containers = json.loads(inspected.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise util.CommandError(
            ["docker", "container", "inspect"], 1,
            "Docker returned invalid inspect JSON: %s" % exc,
        )
    if not isinstance(containers, list):
        raise util.CommandError(
            ["docker", "container", "inspect"], 1,
            "Docker inspect output is not a list.",
        )

    def overlaps(left: str, right: str) -> bool:
        return (left == right
                or left.startswith(right.rstrip(os.path.sep) + os.path.sep)
                or right.startswith(left.rstrip(os.path.sep) + os.path.sep))

    found = []
    for container in containers:
        if not isinstance(container, dict):
            raise util.CommandError(
                ["docker", "container", "inspect"], 1,
                "Docker inspect contains a non-object entry.",
            )
        name = str(container.get("Name") or container.get("Id") or "unknown").lstrip("/")
        mounts = container.get("Mounts") or []
        if not isinstance(mounts, list):
            raise util.CommandError(
                ["docker", "container", "inspect"], 1,
                "Docker inspect Mounts is not a list for %s." % name,
            )
        for mount in mounts:
            if (not isinstance(mount, dict) or mount.get("Type") != "bind"
                    or mount.get("RW") is False):
                continue
            source = mount.get("Source")
            if not isinstance(source, str) or not os.path.isabs(source):
                raise util.CommandError(
                    ["docker", "container", "inspect"], 1,
                    "Docker returned an unsafe writable bind source for %s." % name,
                )
            canonical_source = os.path.realpath(os.path.normpath(source))
            if overlaps(target, canonical_source):
                found.append({"container": name, "source": canonical_source})
    return found


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


def up_service(
    compose_file, project_dir, service, project_name=None, *, no_deps=False,
) -> None:
    argv = _base(compose_file, project_dir, project_name) + ["up", "-d", "--no-build"]
    if no_deps:
        argv.append("--no-deps")
    util.run(argv + [service], mutating=True, capture=False)


def up_services(
    compose_file, project_dir, services, project_name=None, *, no_deps=False,
) -> None:
    """Bring up only an explicit non-empty service list.

    Custom application restores use ``no_deps=True`` so a GitLab service cannot
    implicitly start a runner, DinD daemon, or another production integration.
    Callers validate service identities against the authenticated Compose model.
    """
    if not isinstance(services, list) or not services:
        raise ValueError("services must be a non-empty list")
    argv = _base(compose_file, project_dir, project_name) + ["up", "-d", "--no-build"]
    if no_deps:
        argv.append("--no-deps")
    util.run(argv + list(services), mutating=True, capture=False)


def up_all(compose_file, project_dir, project_name=None) -> None:
    """Brings up the ENTIRE stack (``docker compose up -d``).

    Needed for the custom restore command: a ``docker exec <svc> …`` hook needs
    a running container, whereas the built-in restore leaves the stack stopped.
    """
    util.run(_base(compose_file, project_dir, project_name) + ["up", "-d", "--no-build"],
             mutating=True, capture=False)


def down_all(compose_file, project_dir, project_name=None) -> None:
    """Stop/remove restore-time containers while preserving data volumes."""
    base = _base(compose_file, project_dir, project_name)
    down_argv = base + [
        "down", "--remove-orphans", "--timeout",
        str(RESTORE_CLEANUP_TIMEOUT_SECONDS),
    ]
    util.run(
        down_argv,
        mutating=True, capture=False,
    )
    if util.DRY_RUN:
        return
    remaining = util.run(
        base + ["ps", "--all", "--quiet"],
        capture=True,
    )
    if (remaining.stdout or "").strip():
        raise util.CommandError(
            down_argv, 1,
            "Restore-time project containers remain after cleanup: %s"
            % (remaining.stdout or "").strip(),
        )


def stop_service(compose_file, project_dir, service, project_name=None) -> None:
    util.run(_base(compose_file, project_dir, project_name) + ["stop", service],
             mutating=True, capture=False, check=False)


def rm_service(compose_file, project_dir, service, project_name=None) -> None:
    # -s stops it first, -f without prompting. Volumes/data are preserved.
    base = _base(compose_file, project_dir, project_name)
    util.run(
        base + ["rm", "-f", "-s", service],
        mutating=True, capture=False,
    )
    if util.DRY_RUN:
        return
    remaining = util.run(
        base + ["ps", "--all", "--quiet", service],
        capture=True,
    )
    if (remaining.stdout or "").strip():
        raise util.CommandError(
            base + ["rm", "-f", "-s", service], 1,
            "Restore-time service container remains after cleanup: %s"
            % (remaining.stdout or "").strip(),
        )


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


_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_RESTORE_VOLUME_TOKEN_LABEL = "com.docker-backup.restore-token"
_VOLUME_IDENTITY_UNSET = object()


def _validated_volume_name(real_name: str) -> str:
    if (not isinstance(real_name, str)
            or not _VOLUME_NAME_RE.fullmatch(real_name)):
        raise util.CommandError(
            ["docker", "volume"], 1,
            "Unsafe Docker volume name: %r" % real_name,
        )
    return real_name


def volume_exists(real_name: str) -> bool:
    real_name = _validated_volume_name(real_name)
    proc = util.run(
        ["docker", "volume", "ls", "--quiet", "--filter", "name=%s" % real_name],
        capture=True,
    )
    return real_name in {
        line.strip() for line in (proc.stdout or "").splitlines() if line.strip()
    }


def volume_container_ids(real_name: str) -> List[str]:
    """Return every running or stopped container referencing a volume."""
    real_name = _validated_volume_name(real_name)
    proc = util.run(
        [
            "docker", "container", "ls", "--all", "--quiet", "--no-trunc",
            "--filter", "volume=%s" % real_name,
        ],
        capture=True,
    )
    ids = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if any(not all(c in "0123456789abcdefABCDEF" for c in container_id)
           for container_id in ids):
        raise util.CommandError(
            ["docker", "container", "ls"], 1,
            "Docker returned an invalid volume-user container id.",
        )
    return ids


def volume_identity(real_name: str) -> Optional[Dict[str, Any]]:
    """Return stable inspect fields for one exact existing volume, else None."""
    real_name = _validated_volume_name(real_name)
    if not volume_exists(real_name):
        return None
    proc = util.run(
        ["docker", "volume", "inspect", real_name],
        capture=True,
    )
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise util.CommandError(
            ["docker", "volume", "inspect", real_name], 1,
            "Docker returned invalid volume inspect JSON: %s" % exc,
        )
    if (not isinstance(data, list) or len(data) != 1
            or not isinstance(data[0], dict) or data[0].get("Name") != real_name):
        raise util.CommandError(
            ["docker", "volume", "inspect", real_name], 1,
            "Docker did not return exactly the requested volume.",
        )
    item = data[0]
    return {
        key: item.get(key)
        for key in (
            "Name", "Driver", "Mountpoint", "CreatedAt", "Scope",
            "Options", "Labels",
        )
    }


def prepare_volume_for_restore(
    real_name: str, *, force: bool, driver: str = "local",
    labels: Optional[Dict[str, str]] = None,
    project_name: Optional[str] = None, volume_key: Optional[str] = None,
    expected_identity: Any = _VOLUME_IDENTITY_UNSET,
) -> None:
    """Create one empty, tool-owned volume or fail before extraction.

    Existing volumes are never untarred in place: without ``--force`` they are
    refused, and with ``--force`` they are removed then recreated. Docker itself
    rejects removal while *any* running or stopped container still references
    the volume. A random label proves that an identically named volume created in
    the remove/create race is ours before restored data is written.
    """
    real_name = _validated_volume_name(real_name)
    if driver != "local":
        raise util.CommandError(
            ["docker", "volume", "create", real_name], 1,
            "Automatic restore supports only the default local volume driver; "
            "%s uses %r." % (real_name, driver),
        )
    expected_labels = {}  # type: Dict[str, str]
    for key, value in (labels or {}).items():
        if (not isinstance(key, str) or not key or "\0" in key
                or not isinstance(value, str) or "\0" in value):
            raise util.CommandError(
                ["docker", "volume", "create", real_name], 1,
                "Volume labels must be NUL-free strings.",
            )
        expected_labels[key] = value
    if project_name:
        expected_labels["com.docker.compose.project"] = project_name
    if volume_key:
        expected_labels["com.docker.compose.volume"] = volume_key
    if expected_identity is _VOLUME_IDENTITY_UNSET:
        current_identity = volume_identity(real_name)
    else:
        current_identity = volume_identity(real_name)
        if expected_identity is None and current_identity is not None:
            raise util.CommandError(
                ["docker", "volume", "create", real_name], 1,
                "Named volume appeared after preflight; refusing to delete or "
                "write to it.",
            )
        if expected_identity is not None and current_identity != expected_identity:
            raise util.CommandError(
                ["docker", "volume", "inspect", real_name], 1,
                "Named volume identity changed after preflight; refusing restore.",
            )
    exists = current_identity is not None
    if exists:
        users = volume_container_ids(real_name)
        if users:
            raise util.CommandError(
                ["docker", "volume", "rm", real_name], 1,
                "Named volume %s is still referenced by container(s): %s. "
                "Remove those containers before restoring."
                % (real_name, ", ".join(ids[:12] for ids in users)),
            )
        if not force:
            raise util.CommandError(
                ["docker", "volume", "create", real_name], 1,
                "Named volume %s already exists. Use --force only after review; "
                "it will be deleted and recreated." % real_name,
            )
        util.run(
            ["docker", "volume", "rm", real_name],
            mutating=True, capture=True,
        )

    token = secrets.token_hex(16)
    expected_labels[_RESTORE_VOLUME_TOKEN_LABEL] = token
    create_argv = [
        "docker", "volume", "create", "--driver", driver,
    ]
    for key, value in sorted(expected_labels.items()):
        create_argv += ["--label", "%s=%s" % (key, value)]
    create_argv.append(real_name)
    util.run(
        create_argv,
        mutating=True, capture=True,
    )
    inspected = util.run(
        [
            "docker", "volume", "inspect", "--format", "{{json .Labels}}",
            real_name,
        ],
        capture=True,
    )
    try:
        labels = json.loads((inspected.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise util.CommandError(
            ["docker", "volume", "inspect", real_name], 1,
            "Docker returned invalid volume labels: %s" % exc,
        )
    if (not isinstance(labels, dict)
            or any(labels.get(key) != value
                   for key, value in expected_labels.items())):
        raise util.CommandError(
            ["docker", "volume", "inspect", real_name], 1,
            "Named volume appeared concurrently or its authenticated labels "
            "were not preserved by this restore.",
        )
    users = volume_container_ids(real_name)
    if users:
        raise util.CommandError(
            ["docker", "volume", "inspect", real_name], 1,
            "Named volume was attached concurrently before restore: %s"
            % ", ".join(ids[:12] for ids in users),
        )


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


# Exact bind sources that are fixed host plumbing, not stack data.  Prefix
# allowlists are unsafe here: /dev/shm and /proc/<pid>/root can contain mutable
# application paths and must go through normal external-path selection.
_EXTERNAL_BIND_IGNORE_FILES = ("/etc/localtime", "/etc/timezone", "/etc/machine-id",
                               "/etc/hosts", "/etc/resolv.conf",
                               "/run/docker.sock", "/var/run/docker.sock",
                               "/dev/fuse", "/dev/kvm", "/dev/net/tun",
                               "/sys/fs/cgroup")

_EXTERNAL_BIND_DESCRIPTOR_KEYS = {"service", "target", "source"}


def is_system_bind_source(source: Any) -> bool:
    """True only for fixed host-plumbing binds that are never application data."""
    if not isinstance(source, str) or not os.path.isabs(source):
        return False
    return source in _EXTERNAL_BIND_IGNORE_FILES


def _portable_bind_error(message: str) -> util.CommandError:
    return util.CommandError(["external-bind-descriptors"], 2, message)


def _validated_absolute_path(value: Any, label: str) -> str:
    """Return a lexically canonical absolute path or fail closed.

    These values can originate in the plaintext repository manifest.  Do not
    silently turn a relative/tampered value into an absolute path using the
    restore process' current working directory, and do not normalize traversal
    on the caller's behalf.  A descriptor must contain the exact canonical path
    that Docker Compose reported when it was created.
    """
    if (not isinstance(value, str) or not value or "\0" in value
            or not os.path.isabs(value) or value.startswith("//") or value == "/"):
        raise _portable_bind_error(
            "%s must be a non-root absolute path with exactly one leading slash." % label
        )
    canonical = os.path.normpath(value)
    if canonical != value:
        raise _portable_bind_error(
            "%s is not canonical (expected %r, got %r)." % (label, canonical, value)
        )
    return value


def _validated_bind_identity(service: Any, target: Any, *, label: str) -> Tuple[str, str]:
    if not isinstance(service, str) or not service or "\0" in service:
        raise _portable_bind_error("%s.service must be a non-empty string." % label)
    target = _validated_absolute_path(target, "%s.target" % label)
    return service, target


def describe_selected_external_binds(
    compose_json: Dict[str, Any], selected_paths: Any,
) -> List[Dict[str, str]]:
    """Build portable descriptors for explicitly selected external bind paths.

    A descriptor is keyed by the Compose service and container target, while
    ``source`` records the original absolute host path stored in the snapshot.
    Only paths already present in ``selected_paths`` are emitted: discovering a
    bind in the Compose model is never enough to opt it into a future backup.

    One host source may be mounted by several services/targets, so it can yield
    several descriptors.  The destination resolver later requires all of those
    identities to agree on one target host source.
    """
    if not isinstance(selected_paths, list):
        raise _portable_bind_error("selected external bind paths must be a list.")

    selected = []  # type: List[str]
    selected_seen = set()  # type: set
    for index, raw in enumerate(selected_paths):
        path = _validated_absolute_path(raw, "selected path #%d" % index)
        if path not in selected_seen:
            selected_seen.add(path)
            selected.append(path)
    if not selected:
        return []

    by_source = {path: [] for path in selected}  # type: Dict[str, List[Dict[str, str]]]
    identities = {}  # type: Dict[Tuple[str, str], set]
    services = compose_json.get("services") or {}
    if not isinstance(services, dict):
        raise _portable_bind_error("Compose services must be an object.")

    for service, svc in services.items():
        if not isinstance(svc, dict):
            continue
        volumes = svc.get("volumes") or []
        if not isinstance(volumes, list):
            continue
        for vol in volumes:
            if not isinstance(vol, dict) or vol.get("type") != "bind":
                continue
            source = vol.get("source")
            if not isinstance(source, str) or source not in selected_seen:
                continue
            service, target = _validated_bind_identity(
                service, vol.get("target"), label="selected bind"
            )
            source = _validated_absolute_path(source, "selected bind.source")
            identity = (service, target)
            identities.setdefault(identity, set()).add(source)
            descriptor = {"service": service, "target": target, "source": source}
            if descriptor not in by_source[source]:
                by_source[source].append(descriptor)

    for (service, target), sources in identities.items():
        if len(sources) > 1:
            raise _portable_bind_error(
                "Compose bind %s:%s has multiple selected host sources: %s."
                % (service, target, ", ".join(sorted(sources)))
            )

    missing = [path for path in selected if not by_source[path]]
    if missing:
        raise _portable_bind_error(
            "Selected external path(s) are not bind sources in the Compose model: %s."
            % ", ".join(missing)
        )

    result = []  # type: List[Dict[str, str]]
    for source in selected:
        result.extend(sorted(
            by_source[source], key=lambda item: (item["service"], item["target"])
        ))
    return result


def resolve_external_bind_descriptors(
    compose_json: Dict[str, Any], descriptors: Any,
) -> List[Tuple[str, str]]:
    """Resolve selected source descriptors against a destination Compose model.

    Returns stable ``(original_snapshot_source, target_host_source)`` pairs.  No
    destination bind absent from ``descriptors`` is returned.  Missing or
    ambiguous service/target identities, two target paths for one snapshot
    source, and two snapshot sources converging on one target path all fail
    closed instead of silently restoring/merging the wrong data.
    """
    if not isinstance(descriptors, list):
        raise _portable_bind_error("external bind descriptors must be a list.")

    parsed = []  # type: List[Tuple[str, str, str]]
    requested = set()  # type: set
    for index, raw in enumerate(descriptors):
        label = "descriptor #%d" % index
        if not isinstance(raw, dict):
            raise _portable_bind_error("%s must be an object." % label)
        unknown = set(raw) - _EXTERNAL_BIND_DESCRIPTOR_KEYS
        missing = _EXTERNAL_BIND_DESCRIPTOR_KEYS - set(raw)
        if unknown:
            raise _portable_bind_error(
                "%s has unknown field(s): %s."
                % (label, ", ".join(sorted(unknown)))
            )
        if missing:
            raise _portable_bind_error(
                "%s is missing field(s): %s."
                % (label, ", ".join(sorted(missing)))
            )
        service, target = _validated_bind_identity(
            raw.get("service"), raw.get("target"), label=label
        )
        source = _validated_absolute_path(raw.get("source"), "%s.source" % label)
        parsed.append((service, target, source))
        requested.add((service, target))
    if not parsed:
        return []

    candidates = {identity: set() for identity in requested}  # type: Dict[Tuple[str, str], set]
    services = compose_json.get("services") or {}
    if not isinstance(services, dict):
        raise _portable_bind_error("Compose services must be an object.")
    for service, svc in services.items():
        if not isinstance(service, str) or not isinstance(svc, dict):
            continue
        volumes = svc.get("volumes") or []
        if not isinstance(volumes, list):
            continue
        for vol in volumes:
            if not isinstance(vol, dict) or vol.get("type") != "bind":
                continue
            target = vol.get("target")
            if not isinstance(target, str):
                continue
            identity = (service, target)
            if identity not in requested:
                continue  # a newly discovered/unselected bind is deliberately ignored
            source = _validated_absolute_path(
                vol.get("source"), "target bind %s:%s source" % identity
            )
            candidates[identity].add(source)

    resolved_by_original = {}  # type: Dict[str, str]
    original_by_target = {}  # type: Dict[str, str]
    original_order = []  # type: List[str]
    for service, target, original in parsed:
        options = candidates[(service, target)]
        if not options:
            raise _portable_bind_error(
                "Selected bind %s:%s is missing from the target Compose model."
                % (service, target)
            )
        if len(options) > 1:
            raise _portable_bind_error(
                "Selected bind %s:%s is ambiguous on the target (host sources: %s)."
                % (service, target, ", ".join(sorted(options)))
            )
        target_source = next(iter(options))
        previous_target = resolved_by_original.get(original)
        if previous_target is not None and previous_target != target_source:
            raise _portable_bind_error(
                "Snapshot source %s maps to conflicting target paths %s and %s."
                % (original, previous_target, target_source)
            )
        previous_original = original_by_target.get(target_source)
        if previous_original is not None and previous_original != original:
            raise _portable_bind_error(
                "Target path %s maps from conflicting snapshot sources %s and %s."
                % (target_source, previous_original, original)
            )
        if previous_target is None:
            original_order.append(original)
        resolved_by_original[original] = target_source
        original_by_target[target_source] = original

    # Restoring two selected roots into ancestor/descendant locations is order
    # dependent: moving the parent can swallow or overwrite the child mapping.
    # The same is true for nested snapshot roots. Refuse both shapes instead of
    # producing a partially reconstructed target.
    originals = list(resolved_by_original)
    targets = list(original_by_target)
    for label, paths in (("snapshot sources", originals), ("target paths", targets)):
        ordered = sorted(paths)
        for index, path in enumerate(ordered):
            for other in ordered[index + 1:]:
                if other.startswith(path.rstrip("/") + os.path.sep):
                    raise _portable_bind_error(
                        "External bind %s overlap: %s contains %s."
                        % (label, path, other)
                    )

    return [(source, resolved_by_original[source]) for source in original_order]


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
            if is_system_bind_source(src):
                continue
            if os.path.exists(src) and not (os.path.isdir(src) or os.path.isfile(src)):
                continue  # socket/device/fifo
            seen.add(src)
            out.append(src)
    return out
