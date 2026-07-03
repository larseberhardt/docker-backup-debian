"""``doctor [name] [--all]`` — health check of configured stacks.

Live check per stack: loads the config, verifies repo reachability, key permissions,
timer status and the last recorded run result. Exit code 0 when everything is
healthy, otherwise 1 — usable as a simple health check.
"""

from __future__ import annotations

import os
import stat
from typing import Any, List, Tuple

from .. import config, hooks, keys, restic, runtime, status, systemd_units, util

_HEADER = ("NAME", "CONFIG", "REPO", "KEY", "TIMER", "HOOKS", "NEXT", "LAST RUN")


def cmd_doctor(args) -> int:
    util.require_root()
    _warn_restic_version()
    name = getattr(args, "name", None)
    if name:
        names = [config.sanitize_name(name)]
        if not config.exists(names[0]):
            util.error("No config named '%s'." % names[0])
            return 1
    else:
        names = config.list_names()
    if not names:
        print("No backups configured. Create one with 'docker-backup create <path>'.")
        return 0

    rows = []  # type: List[Tuple[Any, ...]]
    worst = 0
    for n in names:
        row, severity = _check_one(n)
        rows.append(row)
        worst = max(worst, severity)
    util.print_table(_HEADER, rows)
    return 0 if worst == 0 else 1


def _warn_restic_version() -> None:
    """Visible warning when the installed restic is too old (distro packages!)."""
    v = restic.restic_version()
    if v is None:
        util.warn("restic not found in PATH — no backup can run.")
        return
    ver = ".".join(str(x) for x in v)
    if v < restic.MIN_VERSION:
        util.warn("restic %s is too old: offsite backups (init/copy --from-repo) need "
                  ">= %s. Please upgrade (https://restic.net)."
                  % (ver, ".".join(str(x) for x in restic.MIN_VERSION)))
    elif v < restic.RECOMMENDED_VERSION:
        util.warn("restic %s works; >= %s is recommended (removes stale repo locks "
                  "automatically, --retry-lock)."
                  % (ver, ".".join(str(x) for x in restic.RECOMMENDED_VERSION)))


def _check_one(name: str) -> Tuple[Tuple[Any, ...], int]:
    severity = 0
    try:
        cfg = config.load(name)
    except Exception:
        return ((name, "corrupt", "-", "-", "-", "-", "-", "-"), 2)

    # --- KEY ---
    key_file = cfg.get("key_file") or keys.key_path(name)
    if not os.path.exists(key_file):
        key_col = "missing"
        severity = max(severity, 2)
    else:
        mode = stat.S_IMODE(os.stat(key_file).st_mode)
        if mode & 0o077:
            key_col = "%o!" % mode
            severity = max(severity, 1)
        else:
            key_col = "0600"

    # --- REPO ---
    repo = cfg.get("repo", "-")
    repo_col = "?"
    try:
        runtime.load_backend_env(cfg)
        if key_file and os.path.exists(key_file) and restic.repo_initialized(repo, key_file):
            repo_col = "reachable"
        else:
            repo_col = "missing"
            severity = max(severity, 2)
    except Exception:
        repo_col = "missing"
        severity = max(severity, 2)

    # --- TIMER ---
    timer_col = systemd_units.timer_active(name) or "-"
    if timer_col != "active":
        severity = max(severity, 1)
    next_col = systemd_units.timer_next(name) or "-"

    # --- HOOKS ---
    if not hooks.has_commands(cfg):
        hook_col = "-"
    elif cfg.get("hooks_allowed"):
        hook_col = "ok"
    else:
        # Hooks present but not allowed → the next run fails hard.
        hook_col = "BLOCKED"
        severity = max(severity, 1)
    if not cfg.get("db_autodetect", True) and not (cfg.get("db_services") or []) \
            and not hooks.phase_hooks(cfg, "pre_backup"):
        util.warn("Stack '%s': DB auto-detection off, no DB services and no pre-hook — "
                  "NO database will be captured." % name)

    # --- LAST RUN ---
    st = status.read_status(name)
    if not st:
        last_col = "—"
        severity = max(severity, 1)
    else:
        when = str(st.get("finished_at") or "")[:19]
        result = st.get("result")
        if result == "success":
            last_col = "OK %s" % when
        else:
            last_col = "FAILED %s" % when
            severity = max(severity, 2)

    return ((name, "ok", repo_col, key_col, timer_col, hook_col, next_col, last_col), severity)
