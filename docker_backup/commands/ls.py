"""``ls`` — list configured backup configs."""

from __future__ import annotations

from typing import Any, List, Tuple

from .. import config, manifest, restic, runtime, systemd_units, util


def cmd_ls(args) -> int:
    on_repo = getattr(args, "on_repo", None)
    if on_repo:
        return _ls_on_repo(on_repo)

    names = config.list_names()
    if not names:
        print("No backups configured. Create one with 'docker-backup create <path>'.")
        return 0

    show_snap = getattr(args, "snapshots", False)
    header = ("NAME", "STACK", "TARGET", "SCHEDULE", "TIMER")  # type: Tuple[str, ...]
    if show_snap:
        header = header + ("LAST SNAPSHOT",)

    rows = []  # type: List[Tuple[str, ...]]
    for n in names:
        try:
            cfg = config.load(n)
        except Exception:
            rows.append((n, "(corrupt)", "", "", "") + (("",) if show_snap else ()))
            continue
        sched = (cfg.get("schedule") or {}).get("oncalendar", "-")
        repo = cfg.get("repo", "-")
        timer = systemd_units.timer_active(n) or "-"
        row = (n, cfg.get("stack_path", "-"), repo, sched, timer)  # type: Tuple[str, ...]
        if show_snap:
            snap = "-"
            if cfg.get("key_file"):
                runtime.load_backend_env(cfg)
                s = restic.last_snapshot(repo, cfg["key_file"])
                if s:
                    snap = str(s.get("time", "-"))
            row = row + (snap,)
        rows.append(row)

    util.print_table(header, rows)
    return 0


def _ls_on_repo(base: str) -> int:
    """List backed-up stacks on a mounted backup drive (manifest scan)."""
    found = manifest.find_manifests(base)
    if not found:
        print("No backup manifests found under %s." % base)
        return 0

    header = ("NAME", "STACK", "REPO", "DB-SERVICES")  # type: Tuple[str, ...]
    rows = []  # type: List[Tuple[str, ...]]
    for repo, man in found:
        dbs = ", ".join(db.get("service", "?") for db in man.get("db_services") or []) or "-"
        rows.append((man.get("name", "?"), man.get("stack_path", "-"), repo, dbs))

    util.print_table(header, rows)
    print("\nRestore e.g.: docker-backup restore <dest> --from-repo <REPO> --key-file <key>")
    return 0
