"""Status tracking (standard library only).

Two tasks, both modeled on :mod:`selfupdate`:

* **Per-run status** – after each backup run, ``run`` writes the result here
  (success/failure, time, snapshot). ``doctor``/``ls`` only read it. Single source
  of truth for "did the last backup run succeed?".
* **Integrity check cache** – the weekly ``check --all --refresh-cache`` writes the
  check result per stack; the TTY notice only reads this cache.

All writers are atomic (tempfile + ``os.replace``), no-op under ``--dry-run``
and never raise. All readers are defensive (missing/broken → ``None``/``[]``).
Paths hang off :func:`config.etc_dir`, so they respect ``DOCKER_BACKUP_ETC``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import config, util

STATUS_SCHEMA = 1
CHECK_SCHEMA = 1

# Commands for which no check notice appears (analogous to selfupdate).
_SILENT_COMMANDS = frozenset({"run", "check"})


# --- paths ------------------------------------------------------------------
def status_dir() -> str:
    return os.path.join(config.etc_dir(), "status")


def status_path(name: str) -> str:
    return os.path.join(status_dir(), config.sanitize_name(name) + ".json")


def check_cache_path() -> str:
    return os.path.join(config.etc_dir(), ".check-status.json")


# --- shared atomic writer ---------------------------------------------------
def _atomic_write_json(path: str, data: Dict[str, Any], mode: int) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- per-run status ---------------------------------------------------------
def write_status(
    name: str,
    *,
    result: str,
    started_at: str,
    finished_at: str,
    duration_sec: float,
    snapshot: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[str]:
    """Writes the status of a backup run (``result`` in {success, failure}).

    No-op under ``--dry-run``; never raises (returns ``None`` on error)."""
    if util.DRY_RUN:
        return None
    try:
        d = status_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o750)
        except OSError:
            pass
        data = {
            "schema": STATUS_SCHEMA,
            "name": name,
            "result": result,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_sec": duration_sec,
            "snapshot": snapshot,
            "error": error,
        }
        path = status_path(name)
        _atomic_write_json(path, data, 0o640)
        return path
    except Exception:
        return None


def read_status(name: str) -> Optional[Dict[str, Any]]:
    """Reads the last run status; ``None`` if missing/broken. Never raises."""
    try:
        return _read_json(status_path(name))
    except Exception:
        return None


def delete_status(name: str) -> None:
    """Removes the status file (idempotent). No-op under ``--dry-run``."""
    if util.DRY_RUN:
        util.info("DRY-RUN: would remove status file: %s" % status_path(name))
        return
    try:
        os.unlink(status_path(name))
    except (FileNotFoundError, ValueError):
        pass
    except OSError:
        pass


# --- integrity check cache --------------------------------------------------
def write_check_cache(checked_at: str, stacks: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Writes the check cache (per stack ``{status, checked_at, error}``).

    No-op under ``--dry-run``; never raises."""
    if util.DRY_RUN:
        return None
    try:
        data = {"schema": CHECK_SCHEMA, "checked_at": checked_at, "stacks": stacks}
        path = check_cache_path()
        _atomic_write_json(path, data, 0o640)
        return path
    except Exception:
        return None


def read_check_cache() -> Optional[Dict[str, Any]]:
    try:
        return _read_json(check_cache_path())
    except Exception:
        return None


def failed_checks() -> List[str]:
    """Names of stacks whose last integrity check failed. Never raises."""
    try:
        cache = read_check_cache()
        if not cache:
            return []
        stacks = cache.get("stacks") or {}
        if not isinstance(stacks, dict):
            return []
        return sorted(
            n for n, v in stacks.items()
            if isinstance(v, dict) and v.get("status") == "failed"
        )
    except Exception:
        return []


# --- TTY notice -------------------------------------------------------------
def maybe_print_check_notice(command: Optional[str]) -> None:
    """Prints a one-line notice about failed checks if appropriate.

    Stays silent unless all conditions are met (interactive TTY only, not a
    silent command, no opt-out). Fully wrapped — never breaks the CLI."""
    try:
        if command in _SILENT_COMMANDS:
            return
        if not sys.stderr.isatty():
            return
        if os.environ.get("DOCKER_BACKUP_NO_CHECK_NOTICE"):
            return
        failed = failed_checks()
        if not failed:
            return
        util.warn(
            "Integrity check failed for: %s. Verify with 'docker-backup check %s'."
            % (", ".join(failed), failed[0])
        )
    except Exception:
        return
