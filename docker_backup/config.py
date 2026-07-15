"""Config schema and persistence under /etc/docker-backup.

The base path can be overridden via the ``DOCKER_BACKUP_ETC`` environment
variable (used for tests).
"""

from __future__ import annotations

import errno
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 2
DB_SCOPE_VERSION = 2
DEFAULT_RETENTION = {"daily": 7, "weekly": 4, "monthly": 6}
DEFAULT_SCHEDULE_INPUT = "daily 03:00"

_HOOK_PHASES = ("pre_backup", "post_backup", "restore")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Fills in missing v2 fields in-memory — idempotent, does NOT persist.

    Older v1 configs without the new fields (``hooks``, ``exclude_patterns``,
    ``db_autodetect`` …) keep working unchanged. Security-relevant defaults are
    NEVER flipped: ``hooks_allowed`` stays ``False`` (hooks only run after
    explicit approval), ``db_autodetect`` stays ``True`` (current behavior).
    Called from :func:`load`, but writes nothing back — the v2 stamp only lands
    on disk on the next :func:`save`.
    """
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        cfg["hooks"] = hooks
    for phase in _HOOK_PHASES:
        if not isinstance(hooks.get(phase), list):
            hooks[phase] = []
    cfg.setdefault("hooks_allowed", False)
    cfg.setdefault("hooks_fingerprint", None)
    cfg.setdefault("exclude_patterns", [])
    cfg.setdefault("db_autodetect", True)
    cfg.setdefault("template", None)
    cfg.setdefault("extra_backup_paths", [])
    # Offsite repos are pruned with the primary retention unless overridden;
    # offsite_prune=False keeps every copied snapshot (unbounded growth).
    cfg.setdefault("offsite_retention", None)
    cfg.setdefault("offsite_prune", True)
    # Built-in consistent capture for mongo/redis (detected at create time).
    cfg.setdefault("quiesce_services", [])
    cfg.setdefault("quiesce_disabled", False)
    ret = cfg.get("retention")
    if not isinstance(ret, dict):
        ret = dict(DEFAULT_RETENTION)
        cfg["retention"] = ret
    ret.setdefault("keep_within", None)
    return cfg


# --- paths ------------------------------------------------------------------
def etc_dir() -> str:
    return os.environ.get("DOCKER_BACKUP_ETC", "/etc/docker-backup")


def configs_dir() -> str:
    return os.path.join(etc_dir(), "configs")


def keys_dir() -> str:
    return os.path.join(etc_dir(), "keys")


def backends_dir() -> str:
    return os.path.join(etc_dir(), "backends")


def secrets_dir() -> str:
    return os.path.join(etc_dir(), "secrets")


def ensure_dirs() -> None:
    os.makedirs(configs_dir(), exist_ok=True)
    for d in (keys_dir(), backends_dir(), secrets_dir()):
        os.makedirs(d, exist_ok=True)
        _try_chmod(d, 0o700)
    _try_chmod(configs_dir(), 0o750)


def _try_chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (PermissionError, OSError):
        pass


def sanitize_name(name: str) -> str:
    # Deliberately NO basename: a name must be a simple token. Path-like inputs
    # (e.g. '../etc/passwd') are rejected rather than silently truncated.
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("Invalid stack name: %r" % name)
    return name


def config_path(name: str) -> str:
    return os.path.join(configs_dir(), sanitize_name(name) + ".json")


def mutation_lock_path(name: str) -> str:
    """Per-name lock shared by create and restore config publication."""
    return os.path.join(configs_dir(), ".%s.config.lock" % sanitize_name(name))


def exists(name: str) -> bool:
    try:
        return os.path.exists(config_path(name))
    except ValueError:
        return False


# --- load / save ------------------------------------------------------------
def load(name: str) -> Dict[str, Any]:
    with open(config_path(name)) as f:
        return _defaults(json.load(f))


def save(cfg: Dict[str, Any]) -> str:
    ensure_dirs()
    name = sanitize_name(cfg["name"])
    cfg["schema_version"] = SCHEMA_VERSION  # v2 stamp on write (not on read)
    path = config_path(name)
    data = json.dumps(cfg, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=configs_dir(), prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
        tmp = ""  # moved successfully
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    return path


def save_new(cfg: Dict[str, Any]) -> str:
    """Atomically publish a config only if that name is still absent.

    ``os.replace`` is correct for normal updates but unsafe for restore bootstrap:
    it could overwrite a config created concurrently after the preflight. A hard
    link gives us an atomic no-replace publication on the local /etc filesystem.
    """
    ensure_dirs()
    name = sanitize_name(cfg["name"])
    cfg["schema_version"] = SCHEMA_VERSION
    path = config_path(name)
    data = json.dumps(cfg, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=configs_dir(), prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        try:
            os.link(tmp, path)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileExistsError("Config already exists: %s" % path)
            raise
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


def delete(name: str) -> None:
    path = config_path(name)
    if os.path.exists(path):
        os.unlink(path)


def list_names() -> List[str]:
    d = configs_dir()
    if not os.path.isdir(d):
        return []
    return sorted(fn[:-5] for fn in os.listdir(d) if fn.endswith(".json"))


def list_configs() -> List[Dict[str, Any]]:
    out = []
    for n in list_names():
        try:
            out.append(load(n))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return out


# --- DB password sidecars (only if entered interactively) -------------------
def secret_path(name: str, service: str) -> str:
    return os.path.join(secrets_dir(), "%s-%s.pw" % (sanitize_name(name), service))


def save_secret(name: str, service: str, value: str) -> str:
    ensure_dirs()
    path = secret_path(name, service)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.chmod(path, 0o600)
    return path


def read_secret(name: str, service: str) -> Optional[str]:
    path = secret_path(name, service)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()
