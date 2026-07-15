"""restic wrapper: argv builders (testable) + execution helpers.

The repo password is ALWAYS passed via ``--password-file``, never as an argument
and never interactively.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import util

# Feature floor: >= 0.17 provides an unambiguous exit code when a repository is
# absent. It also includes restore --sparse, required for correct-size restores;
# without that flag a source occupying 500 GB can expand to its multi-TiB logical
# size. Older generic exit codes cannot safely drive automatic initialization.
MIN_VERSION = (0, 17, 0)
RECOMMENDED_VERSION = (0, 19, 1)

_VERSION_RE = re.compile(r"restic\s+(\d+)\.(\d+)\.(\d+)")


def _repo_missing_error(exc: util.CommandError) -> bool:
    """Whether ``cat config`` proved that no repository exists.

    restic >= 0.17 has a dedicated exit code 10 for this case. Older versions
    use the ambiguous code 1 for both a missing repository and operational
    failures. Diagnostic text is not a stable interface, so fail closed for
    every code other than 10.
    """
    return exc.returncode == 10


def restic_version() -> Optional[Tuple[int, int, int]]:
    """Installed restic version as a tuple, or ``None`` if not determinable."""
    try:
        proc = util.run(["restic", "version"], capture=True, check=True)
    except util.CommandError:
        return None
    m = _VERSION_RE.search(proc.stdout or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


# --- exclude resolution (pure) ----------------------------------------------
def resolve_excludes(
    stack_path: str, exclude_paths: List[str], exclude_patterns: List[str]
) -> List[str]:
    """Merges auto-detected absolute excludes with user-defined patterns.

    - ``exclude_paths``: already absolute paths (auto-detected DB data dirs) → unchanged.
    - ``exclude_patterns``: relative to the stack. A pattern WITH ``/`` (e.g.
      ``gitlab/logs`` or ``/gitlab/logs``) is anchored at the stack root and thus
      matches ONLY within the stack (no "excludes every ``logs/``" footgun). A
      plain name/glob WITHOUT ``/`` (e.g. ``*.log``, ``node_modules``) is passed
      through unchanged — restic then matches it against the basename anywhere in the tree.
    """
    out = list(exclude_paths or [])
    base = (stack_path or "").rstrip("/")
    for pat in (exclude_patterns or []):
        p = (pat or "").strip()
        if not p:
            continue
        if "/" in p:
            out.append("%s/%s" % (base, p.strip("/")))
        else:
            out.append(p)
    return out


def validate_exclude_pattern(pattern: str) -> str:
    """Validates a user-defined exclude pattern (for create/set/templates).

    Rejects empty patterns and ``..`` traversal. Returns the trimmed pattern.
    """
    p = (pattern or "").strip()
    if not p:
        raise util.CommandError(["--exclude"], 2, "Empty exclude pattern.")
    if ".." in [seg for seg in p.split("/") if seg not in ("", ".")]:
        raise util.CommandError(["--exclude"], 2,
                                "Exclude pattern must not contain '..': %r" % pattern)
    return p


# --- argv builders (pure) ---------------------------------------------------
def base_args(repo: str, key_file: str) -> List[str]:
    return ["restic", "-r", repo, "--password-file", key_file]


def build_init(repo: str, key_file: str) -> List[str]:
    return base_args(repo, key_file) + ["init"]


def build_init_offsite(offsite: str, key_file: str, primary: str) -> List[str]:
    # Same chunker parameters as the primary repo → real deduplication on copy.
    return base_args(offsite, key_file) + [
        "init",
        "--from-repo", primary,
        "--from-password-file", key_file,
        "--copy-chunker-params",
    ]


def build_backup(
    repo: str,
    key_file: str,
    paths: List[str],
    excludes: List[str],
    tags: List[str],
) -> List[str]:
    argv = base_args(repo, key_file) + ["backup"]
    for t in tags:
        argv += ["--tag", t]
    for e in excludes:
        argv += ["--exclude", e]
    argv += list(paths)
    return argv


def build_forget(repo: str, key_file: str, retention: Dict[str, Any], tags: List[str]) -> List[str]:
    # Read count keys defensively: a dict produced by a template or --keep-within
    # must not turn a missing count key into a KeyError mid-run.
    #
    # --group-by tags: the default grouping (host,paths) would strand the previous
    # snapshots in a frozen group whenever the path set changes (extra_backup_paths
    # added/removed) or the host is renamed — its newest snapshots would then be
    # kept forever. One repo holds exactly one stack, so grouping by the fixed tags
    # applies the retention across all of its snapshots.
    argv = base_args(repo, key_file) + [
        "forget", "--prune",
        "--group-by", "tags",
        "--keep-daily", str(retention.get("daily", 0)),
        "--keep-weekly", str(retention.get("weekly", 0)),
        "--keep-monthly", str(retention.get("monthly", 0)),
    ]
    within = retention.get("keep_within")
    if within:
        # restic --keep-within is ADDITIVE: keeps ALL snapshots in the time window
        # (in addition to the keep-daily/weekly/monthly rules), no cap.
        argv += ["--keep-within", str(within)]
    for t in tags:
        argv += ["--tag", t]
    return argv


def build_restore(
    repo: str, key_file: str, snapshot: str, target: str, paths: Optional[List[str]] = None
) -> List[str]:
    # restic stores file contents, not the original sparse-hole map. --sparse
    # recreates holes for long zero ranges instead of allocating physical blocks.
    argv = base_args(repo, key_file) + [
        "restore", snapshot, "--target", target, "--sparse",
    ]
    for p in (paths or []):
        argv += ["--path", p]
    return argv


def build_copy(offsite: str, key_file: str, primary: str) -> List[str]:
    return base_args(offsite, key_file) + [
        "copy", "--from-repo", primary, "--from-password-file", key_file,
    ]


def build_check(
    repo: str, key_file: str, read_data: bool = False, read_data_subset: Optional[str] = None
) -> List[str]:
    argv = base_args(repo, key_file) + ["check"]
    if read_data:
        argv += ["--read-data"]
    elif read_data_subset:
        argv += ["--read-data-subset=%s" % read_data_subset]
    return argv


def build_snapshots(repo: str, key_file: str, latest: bool = True) -> List[str]:
    argv = base_args(repo, key_file) + ["snapshots", "--json"]
    if latest:
        argv += ["--latest", "1"]
    return argv


# --- execution helpers ------------------------------------------------------
def repo_initialized(repo: str, key_file: str) -> bool:
    """Return false only when restic positively reports a missing repository.

    Access/configuration failures are raised so callers retain restic's original
    diagnostic instead of attempting ``init`` on an existing repository.
    """
    try:
        util.run(base_args(repo, key_file) + ["cat", "config"], capture=True, check=True)
        return True
    except util.CommandError as exc:
        if _repo_missing_error(exc):
            return False
        raise


def ensure_init(repo: str, key_file: str) -> None:
    if repo_initialized(repo, key_file):
        return
    util.info("Initializing restic repo: %s" % repo)
    util.run(build_init(repo, key_file), capture=True, mutating=True)


def ensure_init_offsite(offsite: str, key_file: str, primary: str) -> None:
    if repo_initialized(offsite, key_file):
        return
    util.info("Initializing offsite repo: %s" % offsite)
    util.run(build_init_offsite(offsite, key_file, primary), capture=True, mutating=True)


def backup(repo, key_file, paths, excludes, tags) -> None:
    util.info("restic backup → %s" % repo)
    util.run(build_backup(repo, key_file, paths, excludes, tags), capture=False, mutating=True)


def forget_prune(repo, key_file, retention, tags) -> None:
    util.info("restic forget/prune (retention %s)" % retention)
    util.run(build_forget(repo, key_file, retention, tags), capture=False, mutating=True)


def restore(repo, key_file, snapshot, target, paths=None) -> None:
    util.info("restic restore %s → %s" % (snapshot, target))
    util.run(build_restore(repo, key_file, snapshot, target, paths), capture=False, mutating=True)


def check(repo, key_file, read_data=False, read_data_subset=None) -> bool:
    """Verifies repo integrity (``restic check``). Read-only → not ``mutating``.

    Returns True on success, False on an integrity error."""
    util.info("restic check → %s" % repo)
    try:
        util.run(build_check(repo, key_file, read_data, read_data_subset), capture=False)
        return True
    except util.CommandError:
        return False


def copy(offsite, key_file, primary) -> None:
    util.info("restic copy %s → %s" % (primary, offsite))
    util.run(build_copy(offsite, key_file, primary), capture=False, mutating=True)


def unlock(repo, key_file) -> None:
    util.run(base_args(repo, key_file) + ["unlock"], capture=True, mutating=True, check=False)


def last_snapshot(repo: str, key_file: str) -> Optional[Dict[str, Any]]:
    try:
        proc = util.run(build_snapshots(repo, key_file, latest=True), capture=True, check=True)
    except util.CommandError:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data[-1] if isinstance(data, list) and data else None
