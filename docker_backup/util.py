"""Shared helper functions: subprocess wrapper, logging, secret scrubbing, locking.

Deliberately standard library only and compatible with Python 3.9.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shlex
import subprocess
import sys
from typing import IO, Any, Dict, Optional, Sequence

# --- global state (set by cli.py) ------------------------------------------
DRY_RUN = False
VERBOSE = False

_SECRETS = set()  # type: set


def set_dry_run(value: bool) -> None:
    global DRY_RUN
    DRY_RUN = bool(value)


def set_verbose(value: bool) -> None:
    global VERBOSE
    VERBOSE = bool(value)


# --- secret scrubbing -------------------------------------------------------
def register_secret(secret: Optional[str]) -> None:
    """Remembers a secret to be replaced with *** in logs."""
    if secret:
        _SECRETS.add(str(secret))


def scrub(text: str) -> str:
    out = text
    for s in _SECRETS:
        if s:
            out = out.replace(s, "***")
    return out


# --- Logging ----------------------------------------------------------------
def log(msg: str) -> None:
    sys.stderr.write(scrub(msg) + "\n")
    sys.stderr.flush()


def info(msg: str) -> None:
    log("[*] " + msg)


def warn(msg: str) -> None:
    log("[!] " + msg)


def error(msg: str) -> None:
    log("[ERROR] " + msg)


def debug(msg: str) -> None:
    if VERBOSE:
        log("[debug] " + msg)


def fmt_argv(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in argv)


def print_table(header: Sequence[Any], rows: Sequence[Sequence[Any]]) -> None:
    """Prints a simple, left-aligned column table to stdout."""
    cols = len(header)
    widths = [len(str(h)) for h in header]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))
    fmt = "  ".join("%-" + str(w) + "s" for w in widths)
    print(fmt % tuple(str(h) for h in header))
    print(fmt % tuple("-" * w for w in widths))
    for r in rows:
        print(fmt % tuple(str(x) for x in r))


# --- command execution ------------------------------------------------------
class CommandError(RuntimeError):
    def __init__(self, argv, returncode, stderr=""):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr or ""
        super().__init__(
            "command failed (rc=%s): %s" % (returncode, scrub(fmt_argv(self.argv)))
        )


def run(
    argv,
    env: Optional[Dict[str, str]] = None,
    env_replace: bool = False,
    input: Optional[str] = None,
    timeout: Optional[float] = None,
    check: bool = True,
    capture: bool = True,
    mutating: bool = False,
    text: bool = True,
    stdout: Optional[IO] = None,
    stdin: Optional[IO] = None,
    cwd: Optional[str] = None,
):
    """Runs a command.

    - ``mutating=True`` commands are only logged under ``--dry-run``, not run.
    - ``env`` is layered on top of the inherited environment (not replaced). With
      ``env_replace=True``, ``env`` is the COMPLETE environment — os.environ is
      NOT inherited. This makes it possible to deliberately keep inherited secrets
      (e.g. backend credentials that ``runtime.load_backend_env`` puts into os.environ) out.
    - ``stdout``/``stdin`` allow file redirection (e.g. for DB dumps/imports).
    """
    argv = [str(a) for a in argv]

    if DRY_RUN and mutating:
        info("DRY-RUN: " + fmt_argv(argv))
        empty = "" if text else b""
        return subprocess.CompletedProcess(argv, 0, empty, empty)

    debug("run: " + fmt_argv(argv))

    run_env = None
    if env is not None:
        run_env = {} if env_replace else dict(os.environ)
        run_env.update({k: str(v) for k, v in env.items()})

    if stdout is not None:
        stdout_arg = stdout  # type: ignore
    elif capture:
        stdout_arg = subprocess.PIPE
    else:
        stdout_arg = None
    stderr_arg = subprocess.PIPE if capture else None

    try:
        proc = subprocess.run(
            argv,
            env=run_env,
            input=input,
            timeout=timeout,
            stdout=stdout_arg,
            stderr=stderr_arg,
            stdin=stdin,
            text=text,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise CommandError(argv, 127, str(exc))

    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, proc.stderr or "")
    return proc


# --- permissions / mounts ---------------------------------------------------
def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        error(
            "This command must run as root "
            "(needs access to /etc/docker-backup, the Docker socket and /opt)."
        )
        sys.exit(1)


def assert_mounted(path: Optional[str]) -> None:
    """Aborts if ``path`` is not on a mounted filesystem.

    Prevents a backup from being written to an unmounted network drive and thus
    silently filling up the root partition.

    Checks whether the target is on a *different* filesystem than ``/``
    (comparing ``st_dev``). The target need not be the mountpoint itself — a
    subfolder of a network share (e.g.
    ``/mnt/backup/<host>/<stack>`` under the CIFS mount ``/mnt/backup``) is
    explicitly allowed. If the share is not mounted, the target falls back to
    the root partition and the check correctly trips.
    """
    if not path:
        return
    # The target folder may not exist yet on the first run (restic creates it
    # later) → check the nearest already-existing ancestor.
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        on_root_fs = os.stat(probe).st_dev == os.stat(os.sep).st_dev
    except OSError as exc:
        raise CommandError(
            [path], 1, "Backup target '%s' not checkable: %s" % (path, exc)
        )
    if on_root_fs:
        raise CommandError(
            [path],
            1,
            "Backup target '%s' is not on a mounted filesystem "
            "(same partition as '/'). Is the network share mounted?"
            % path,
        )


# --- locking ----------------------------------------------------------------
class FileLock:
    """Exclusive, non-blocking flock — prevents overlapping runs."""

    def __init__(self, path: str):
        self.path = path
        self.fd = None  # type: Optional[int]

    def __enter__(self) -> "FileLock":
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise CommandError(
                    [self.path], 1, "Another docker-backup run holds the lock."
                )
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
