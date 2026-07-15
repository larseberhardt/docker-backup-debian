"""``check [name] [--all] [--read-data-subset=PCT]`` — verify restic repo integrity.

``--refresh-cache`` (internal, from the weekly timer) checks all stacks and writes
the result to the check cache; the next interactive command then warns on failures.
"""

from __future__ import annotations

import datetime
from typing import Any, List, Optional, Tuple

from .. import config, restic, runtime, status, util


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _normalize_subset(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip().rstrip("%")
    try:
        pct = int(s)
    except ValueError:
        raise util.CommandError(["--read-data-subset"], 2,
                                "Invalid value for --read-data-subset: %r" % value)
    if not (0 < pct <= 100):
        raise util.CommandError(["--read-data-subset"], 2,
                                "--read-data-subset must be between 1 and 100.")
    return "%d%%" % pct


def cmd_check(args) -> int:
    util.require_root()
    subset = _normalize_subset(getattr(args, "read_data_subset", None))

    if getattr(args, "refresh_cache", False):
        return _refresh_cache(subset)

    name = getattr(args, "name", None)
    if name:
        names = [config.sanitize_name(name)]
        if not config.exists(names[0]):
            util.error("No config named '%s'." % names[0])
            return 1
    else:
        names = config.list_names()
    if not names:
        print("No backups configured.")
        return 0

    rows = []  # type: List[Tuple[Any, ...]]
    worst = 0
    for n in names:
        ok, detail = _check_one(n, subset)
        rows.append((n, "ok" if ok else "FAILED", detail))
        if not ok:
            worst = 1
    util.print_table(("NAME", "INTEGRITY", "DETAIL"), rows)
    return 0 if worst == 0 else 1


def _repos_of(cfg) -> List[Tuple[str, str]]:
    """(label, repo) pairs to verify: primary plus — if configured — offsite.

    The offsite repo is the one needed in a disaster; leaving it unchecked would
    mean its integrity is unknown exactly when it matters."""
    out = [("primary", cfg.get("repo"))]
    if cfg.get("offsite"):
        out.append(("offsite", cfg["offsite"]))
    return out


def _check_one(name: str, subset: Optional[str]) -> Tuple[bool, str]:
    try:
        cfg = config.load(name)
    except Exception:
        return (False, "Config corrupt")
    repo, key = cfg.get("repo"), cfg.get("key_file")
    if not repo or not key:
        return (False, "Repo/key missing in config")
    runtime.load_backend_env(cfg)
    if util.DRY_RUN:
        util.info("DRY-RUN: would run 'restic check' for %s." % repo)
        return (True, "dry-run")
    problems = _check_repos(cfg, key, subset)
    return (not problems, "; ".join(problems))


def _check_repos(cfg, key: str, subset: Optional[str]) -> List[str]:
    """Runs 'restic check' against primary (+ offsite); returns problem strings."""
    problems = []  # type: List[str]
    for label, repo in _repos_of(cfg):
        try:
            restic.unlock(repo, key)  # clear stale locks so the check itself can lock
            if not restic.repo_initialized(repo, key):
                problems.append("%s repo not reachable" % label)
                continue
            if not restic.check(repo, key, read_data_subset=subset):
                problems.append("%s: restic check reported errors" % label)
        except util.CommandError as exc:
            # A password/cache/backend failure is not proof that the repository
            # is absent. Keep checking the other repositories, while recording
            # this one as an access/setup error rather than an integrity failure.
            problems.append(
                "%s repo not reachable (restic rc=%s)" % (label, exc.returncode)
            )
    return problems


def _refresh_cache(subset: Optional[str]) -> int:
    results = {}  # type: dict
    worst = 0
    for n in config.list_names():
        try:
            cfg = config.load(n)
        except Exception:
            results[n] = {"status": "error", "checked_at": _utcnow(), "error": "Config corrupt"}
            worst = 1
            continue
        repo, key = cfg.get("repo"), cfg.get("key_file")
        runtime.load_backend_env(cfg)
        if not repo or not key:
            results[n] = {"status": "error", "checked_at": _utcnow(),
                          "error": "Repo/key missing in config"}
            worst = 1
            continue
        problems = _check_repos(cfg, key, subset)
        if not problems:
            results[n] = {"status": "ok", "checked_at": _utcnow(), "error": None}
            continue
        # unreachable → 'error' (setup problem), check errors → 'failed' (integrity)
        status_kind = "error" if all("not reachable" in p for p in problems) else "failed"
        results[n] = {"status": status_kind, "checked_at": _utcnow(),
                      "error": "; ".join(problems)}
        worst = 1
    status.write_check_cache(_utcnow(), results)
    return 0 if worst == 0 else 1
