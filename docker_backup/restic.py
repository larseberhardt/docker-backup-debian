"""restic wrapper: argv builders (testable) + execution helpers.

The repo password is ALWAYS passed via ``--password-file``, never as an argument
and never interactively.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import util

# Feature floor: >= 0.17 provides an unambiguous exit code when a repository is
# absent. It also includes restore --sparse, required for correct-size restores;
# without that flag a source occupying 500 GB can expand to its multi-TiB logical
# size. Older generic exit codes cannot safely drive automatic initialization.
MIN_VERSION = (0, 17, 0)
# Current stable release when this floor was last reviewed. Newer restic versions
# contain important restore progress, hard-link and metadata error handling fixes.
RECOMMENDED_VERSION = (0, 19, 1)

_VERSION_RE = re.compile(r"restic\s+(\d+)\.(\d+)\.(\d+)")
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _repo_missing_error(exc: util.CommandError) -> bool:
    """Whether ``cat config`` proved that no repository exists.

    restic >= 0.17 has a dedicated exit code 10 for this case.  Older versions
    use the ambiguous code 1 for both a missing repository and operational
    failures.  Diagnostic text is not a stable interface, so fail closed for
    every code other than 10: treating an access/cache/password error as an
    empty location and calling ``init`` would hide the real failure behind
    ``config file already exists``.
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
    json_output: bool = False,
) -> List[str]:
    argv = base_args(repo, key_file) + ["backup"]
    if json_output:
        argv += ["--json"]
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
    # detects long zero runs while restoring and creates holes again. Without it,
    # logical zero ranges can consume physical disk blocks after a restore.
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


def build_snapshots(
    repo: str, key_file: str, latest: bool = True,
    tags: Optional[List[str]] = None,
    group_by: Optional[str] = None,
) -> List[str]:
    argv = base_args(repo, key_file) + ["snapshots", "--json"]
    if tags:
        # A comma-separated tag list is AND in restic; repeated --tag values are
        # OR. The manifest must bind to this stack's just-created tagged snapshot,
        # not an unrelated manual snapshot that happened to be newer.
        argv += ["--tag", ",".join(tags)]
    if group_by:
        argv += ["--group-by", group_by]
    if latest:
        argv += ["--latest", "1"]
    return argv


# --- execution helpers ------------------------------------------------------
def repo_initialized(repo: str, key_file: str) -> bool:
    """Return false only when restic positively reports a missing repository.

    Access/configuration failures are intentionally raised so callers retain
    restic's original exit code and diagnostic instead of attempting ``init``.
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


def _snapshot_id_from_backup_message(line: str) -> Optional[str]:
    """Extracts the full id from one ``restic backup --json`` summary line."""
    try:
        message = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(message, dict) or message.get("message_type") != "summary":
        return None
    snapshot_id = message.get("snapshot_id")
    if isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        return snapshot_id
    return None


def backup(repo, key_file, paths, excludes, tags) -> Optional[str]:
    """Runs a backup and returns the exact full id emitted by that invocation.

    JSON output is forwarded line-by-line instead of being captured until the
    command exits. This keeps long-running backups visibly active in a terminal
    or the systemd journal while letting the caller bind metadata to the summary
    produced by this exact process.

    ``None`` means restic succeeded but did not emit a valid summary id. Callers
    must not replace it with a ``latest`` lookup, which could select a different
    snapshot created concurrently or manually.
    """
    util.info("restic backup → %s" % repo)
    argv = build_backup(repo, key_file, paths, excludes, tags, json_output=True)

    # Preserve util.run's dry-run contract without starting a subprocess.
    if util.DRY_RUN:
        util.run(argv, capture=False, mutating=True)
        return None

    util.debug("run: " + util.fmt_argv(argv))
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            # Keep stderr attached to the terminal/journal, matching the old
            # capture=False behaviour and preserving restic diagnostics.
            stderr=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise util.CommandError(argv, 127, str(exc))

    snapshot_id = None  # type: Optional[str]
    assert proc.stdout is not None
    for line in proc.stdout:
        # --json is deliberately still visible: status messages arrive while a
        # large backup runs rather than appearing only after it completes.
        sys.stdout.write(line)
        sys.stdout.flush()
        parsed = _snapshot_id_from_backup_message(line)
        if parsed is not None:
            snapshot_id = parsed

    returncode = proc.wait()
    if returncode != 0:
        raise util.CommandError(argv, returncode)
    return snapshot_id


def forget_prune(repo, key_file, retention, tags) -> None:
    util.info("restic forget/prune (retention %s)" % retention)
    util.run(build_forget(repo, key_file, retention, tags), capture=False, mutating=True)


def restore(repo, key_file, snapshot, target, paths=None) -> None:
    util.info("restic restore %s → %s" % (snapshot, target))
    util.run(
        build_restore(repo, key_file, snapshot, target, paths),
        capture=False, mutating=True,
    )


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


def last_snapshot(
    repo: str, key_file: str, tags: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    try:
        proc = util.run(
            # Match the retention grouping when filtering by the fixed stack
            # tags. Otherwise restic's default host,paths grouping can return
            # several "latest" snapshots after a host rename or path change.
            build_snapshots(
                repo, key_file, latest=True, tags=tags,
                group_by="tags" if tags else None,
            ),
            capture=True, check=True,
        )
    except util.CommandError:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data[-1] if isinstance(data, list) and data else None


def snapshot_by_id(
    repo: str, key_file: str, snapshot_id: str,
) -> Optional[Dict[str, Any]]:
    """Returns ``snapshot_id`` only when that exact full snapshot still exists."""
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        return None
    try:
        proc = util.run(
            base_args(repo, key_file) + ["snapshots", "--json", snapshot_id],
            capture=True, check=True,
        )
    except util.CommandError:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    for snapshot in data:
        if isinstance(snapshot, dict) and snapshot.get("id") == snapshot_id:
            return snapshot
    return None
