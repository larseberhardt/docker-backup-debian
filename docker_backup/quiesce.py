"""Consistent point-in-time capture for stateful engines WITHOUT dump support.

Two built-in strategies (fixed commands like the DB dumps — no user shell, so
no ``--allow-hooks`` approval needed):

* **mongo** — ``db.adminCommand({fsync:1,lock:true})`` freezes writes (reads
  keep working) while the files are captured; ``fsyncUnlock`` releases them.
  A failed LOCK aborts the run: an unlocked live copy of WiredTiger may be
  unrestorable, and a backup that lies is worse than a red one. A failed
  UNLOCK also fails the run loudly — the database would stay write-locked.
* **redis** (redis/valkey/keydb) — ``BGSAVE`` + wait until the background
  save finished: the RDB on disk is then a fresh checkpoint. Best-effort
  (warn on failure): the RDB is atomic anyway, just possibly minutes old.

The lock window is kept as small as possible: services whose data dir is a
NAMED VOLUME are released right after the tar-to-staging step (scope
"staging"), BEFORE the restic upload. Only bind-mounted data (read live by
restic) holds its lock until ``restic backup`` returns (scope "live").

Credentials reach the in-container shell exclusively via environment
variables (never argv) and are registered for log scrubbing.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from . import compose, util

# Known data directories per engine (official + bitnami images) — used at
# create time to decide the lock scope (see data_scope).
DATA_TARGETS = {
    "mongo": ("/data/db", "/data/configdb", "/bitnami/mongodb"),
    "redis": ("/data", "/bitnami/redis"),
}

_REDIS_WAIT_SECONDS = 300  # BGSAVE of a large dataset can take minutes


# --- create-time helper (pure) ------------------------------------------------
def data_scope(svc_volumes: List[Dict[str, Any]], engine: str) -> str:
    """"staging" if the engine's data dir is a named volume (tarred into
    staging → lock released before the restic upload), "live" if it is a bind
    mount or cannot be identified (conservative: hold through the backup)."""
    targets = DATA_TARGETS.get(engine) or ()
    found_volume = False
    for vol in (svc_volumes or []):
        target = vol.get("target") or ""
        if not any(target == t or target.startswith(t + "/") for t in targets):
            continue
        if vol.get("type") == "bind":
            return "live"
        if vol.get("type") == "volume":
            found_volume = True
    return "staging" if found_volume else "live"


# --- command builders (pure) ----------------------------------------------------
_MONGO_LOCK_EVAL = "var r=db.adminCommand({fsync:1,lock:true});if(!r.ok)quit(1);"
_MONGO_UNLOCK_EVAL = "var r=db.adminCommand({fsyncUnlock:1});if(!r.ok)quit(1);"


def build_mongo_cmd(lock: bool) -> List[str]:
    """fsyncLock/fsyncUnlock via ``mongosh`` (5+) or the legacy ``mongo`` shell.

    The shell probe runs inside the container; auth only when the env vars are
    set (see _auth_env)."""
    ev = _MONGO_LOCK_EVAL if lock else _MONGO_UNLOCK_EVAL
    script = (
        'tool="$(command -v mongosh || command -v mongo)" || exit 127\n'
        'if [ -n "${DOCKER_BACKUP_MONGO_USER:-}" ]; then\n'
        '  exec "$tool" --quiet -u "$DOCKER_BACKUP_MONGO_USER"'
        ' -p "$DOCKER_BACKUP_MONGO_PW" --authenticationDatabase admin'
        " --eval '%s'\n"
        "else\n"
        "  exec \"$tool\" --quiet --eval '%s'\n"
        "fi\n"
    ) % (ev, ev)
    return ["sh", "-c", script]


def build_redis_bgsave_cmd(wait_seconds: int = _REDIS_WAIT_SECONDS) -> List[str]:
    """BGSAVE, then poll INFO persistence until the background save finished OK.

    Auth (if any) comes via the ``REDISCLI_AUTH`` env var, which redis-cli
    reads natively."""
    script = (
        'cli="$(command -v redis-cli || command -v valkey-cli || command -v keydb-cli)" || exit 127\n'
        '"$cli" BGSAVE >/dev/null 2>&1\n'
        "i=0\n"
        'while [ "$i" -lt %d ]; do\n'
        '  info="$("$cli" INFO persistence)" || exit 1\n'
        '  case "$info" in\n'
        "    *rdb_bgsave_in_progress:0*)\n"
        '      case "$info" in *rdb_last_bgsave_status:ok*) exit 0;; esac\n'
        "      exit 1;;\n"
        "  esac\n"
        "  i=$((i+1)); sleep 1\n"
        "done\n"
        "exit 1\n"
    ) % wait_seconds
    return ["sh", "-c", script]


# --- runtime --------------------------------------------------------------------
def _auth_env(entry: Dict[str, Any], svc_env: Dict[str, Any]) -> Dict[str, str]:
    env = {}  # type: Dict[str, str]
    pw_key = entry.get("password_env_key")
    pw = svc_env.get(pw_key) if pw_key else None
    if entry.get("engine") == "mongo":
        user = entry.get("user_value")
        if entry.get("user_env_key"):
            user = svc_env.get(entry["user_env_key"]) or user
        if user and pw:
            util.register_secret(pw)
            env["DOCKER_BACKUP_MONGO_USER"] = str(user)
            env["DOCKER_BACKUP_MONGO_PW"] = str(pw)
    else:  # redis
        if pw:
            util.register_secret(pw)
            env["REDISCLI_AUTH"] = str(pw)
    return env


def begin(cfg: Dict[str, Any], cj: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Quiesces all configured services; returns the entries HOLDING A LOCK.

    Only mongo holds one (redis is a checkpoint, nothing stays locked). Stopped
    services are skipped — their files are already consistent on disk. Pass the
    returned list to :func:`release`; every code path must release it."""
    entries = [e for e in (cfg.get("quiesce_services") or []) if e.get("service")]
    if not entries:
        return []
    if cfg.get("quiesce_disabled"):
        util.info("Quiesce disabled for this stack ('docker-backup set %s --quiesce' "
                  "re-enables it)." % cfg.get("name", "?"))
        return []
    compose_file = cfg["compose_file"]
    stack = cfg["stack_path"]
    project = cfg.get("project_name")
    if cj is None and any(e.get("user_env_key") or e.get("password_env_key") for e in entries):
        cj = compose.config_json(compose_file, stack, project)

    locked = []  # type: List[Dict[str, Any]]
    for e in entries:
        svc = e["service"]
        if not compose.service_running(compose_file, stack, svc, project):
            util.info("Quiesce: service '%s' is stopped — files are consistent as-is." % svc)
            continue
        svc_env = (((cj or {}).get("services") or {}).get(svc) or {}).get("environment") or {}
        env = _auth_env(e, svc_env)
        if e.get("engine") == "mongo":
            util.info("Quiesce: freezing writes on '%s' (db.fsyncLock; reads continue)." % svc)
            argv = compose.exec_args(compose_file, stack, svc, build_mongo_cmd(lock=True),
                                     env=env, tty=False, project_name=project)
            try:
                util.run(argv, capture=True, mutating=True)
            except util.CommandError as exc:
                raise util.CommandError(
                    ["quiesce", svc], exc.returncode,
                    "fsyncLock on '%s' failed — an unlocked live copy of MongoDB may be "
                    "unrestorable, aborting the run. Disable deliberately with "
                    "'docker-backup set %s --no-quiesce' (backup is then only "
                    "crash-consistent)." % (svc, cfg.get("name", "?")),
                )
            locked.append(dict(e, _env=env))
        else:  # redis family — best-effort checkpoint, nothing stays locked
            util.info("Quiesce: RDB checkpoint on '%s' (BGSAVE)." % svc)
            argv = compose.exec_args(compose_file, stack, svc, build_redis_bgsave_cmd(),
                                     env=env, tty=False, project_name=project)
            try:
                util.run(argv, capture=True, mutating=True)
            except util.CommandError:
                util.warn("BGSAVE on '%s' failed — the existing (older) RDB is backed "
                          "up instead." % svc)
    return locked


def release(cfg: Dict[str, Any], locked: List[Dict[str, Any]],
            scope: Optional[str] = None) -> None:
    """Releases held locks (fsyncUnlock) and removes them from ``locked``.

    ``scope`` filters ("staging" = data already tarred, "live" = after the
    restic backup); ``None`` releases everything still held (error paths).
    A failed unlock is an incident (DB stays write-locked): error + non-zero
    run — unless another exception is already propagating (never mask it)."""
    take = [e for e in list(locked) if scope is None or e.get("scope") == scope]
    if not take:
        return
    compose_file = cfg["compose_file"]
    stack = cfg["stack_path"]
    project = cfg.get("project_name")
    failed = []  # type: List[str]
    for e in take:
        locked.remove(e)
        svc = e["service"]
        util.info("Quiesce: releasing writes on '%s' (fsyncUnlock)." % svc)
        argv = compose.exec_args(compose_file, stack, svc, build_mongo_cmd(lock=False),
                                 env=e.get("_env") or {}, tty=False, project_name=project)
        try:
            util.run(argv, capture=True, mutating=True)
        except util.CommandError:
            util.error(
                "fsyncUnlock on '%s' FAILED — the database may still be WRITE-LOCKED. "
                "Release manually: docker compose exec %s mongosh --eval "
                "'db.fsyncUnlock()' (or restart the container)." % (svc, svc)
            )
            failed.append(svc)
    if failed and sys.exc_info()[0] is None:
        raise util.CommandError(
            ["quiesce-unlock"] + failed, 1,
            "fsyncUnlock failed for: %s — release the write lock manually!" % ", ".join(failed),
        )
