"""Version check and update notice (standard library only).

Separation of concerns:

* The **notice** (``maybe_print_update_notice``) runs on *every* CLI call, but
  reads only the small cache file ``.update-check.json`` — never the network,
  never git. It must not break the CLI under any circumstances.
* **Writing** this cache is done exclusively by ``update.sh`` (invoked by the
  operator or the daily systemd timer). This file only reads.

The paths hang off :func:`config.etc_dir`, so they respect ``DOCKER_BACKUP_ETC``
(for tests).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

from . import __version__, config, util

UPDATE_SCRIPT = "/opt/docker-backup/update.sh"

# Commands for which no notice appears: the systemd entry point 'run'
# (otherwise journal noise) and 'update' itself.
_SILENT_COMMANDS = frozenset({"run", "update"})


# --- paths ------------------------------------------------------------------
def cache_path() -> str:
    return os.path.join(config.etc_dir(), ".update-check.json")


def update_conf_path() -> str:
    return os.path.join(config.etc_dir(), "update.conf")


# --- version comparison (pure, no I/O — easily testable) --------------------
def parse_version(s: Optional[str]) -> Tuple[int, ...]:
    """``"1.2.3"`` / ``"v1.2.3"`` → ``(1, 2, 3)``. Unparsable → ``()``.

    A leading ``v`` as well as ``-pre``/``+build`` suffixes are stripped.
    """
    s = (s or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    if not s:
        return ()
    parts = []
    for tok in s.split("."):
        tok = tok.split("-", 1)[0].split("+", 1)[0]
        if not tok.isdigit():
            return ()
        parts.append(int(tok))
    return tuple(parts)


def version_lt(a: Optional[str], b: Optional[str]) -> bool:
    """``True`` if ``a`` is strictly older than ``b``.

    On unparsable input (either one ``()``) deliberately ``False`` — so neither a
    broken cache nor a downgrade ever triggers a notice.
    """
    pa, pb = parse_version(a), parse_version(b)
    if not pa or not pb:
        return False
    return pa < pb


# --- read cache -------------------------------------------------------------
def current_version() -> str:
    return __version__


def read_cache() -> Optional[Dict[str, Any]]:
    """Reads ``.update-check.json``; ``None`` if missing/broken. Never raises."""
    path = cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update_available() -> Optional[Tuple[str, str, int]]:
    """``(current, latest, releases_behind)`` if a newer version is known,
    otherwise ``None``."""
    cache = read_cache()
    if not cache:
        return None
    latest = cache.get("latest_version")
    current = current_version()
    if not version_lt(current, latest):
        return None
    behind = cache.get("releases_behind")
    try:
        behind = int(behind)
    except (TypeError, ValueError):
        behind = 0
    return (current, str(latest), behind)


# --- notice -----------------------------------------------------------------
def notice_line(current: str, latest: str) -> str:
    return (
        "Update available: docker-backup %s → %s. "
        "Update with 'sudo docker-backup update'." % (current, latest)
    )


def maybe_print_update_notice(command: Optional[str]) -> None:
    """Prints a one-line update notice to stderr if appropriate.

    Stays silent unless *all* conditions are met. Fully wrapped in
    ``try/except`` — must never affect the CLI.
    """
    try:
        if command in _SILENT_COMMANDS:
            return
        # Interactive only: keeps the notice out of the journal/pipes (and thus
        # out of systemd's TTY-less 'notify failure' path).
        if not sys.stderr.isatty():
            return
        if os.environ.get("DOCKER_BACKUP_NO_UPDATE_NOTICE"):
            return
        info = update_available()
        if not info:
            return
        current, latest, _behind = info
        util.warn(notice_line(current, latest))
    except Exception:
        return
