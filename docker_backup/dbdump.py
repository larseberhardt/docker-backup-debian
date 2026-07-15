"""Consistent DB dumps and imports via ``docker compose exec``.

Passwords are passed into the exec environment via ``MYSQL_PWD`` / ``PGPASSWORD``
(not into the argv of the in-container process) and scrubbed before any logging.
"""

from __future__ import annotations

import os
import stat
import time
from typing import Any, Dict, List, Optional

from . import compose, util


def _probe_tool(
    compose_file: str, project_dir: str, service: str,
    candidates: List[str], project_name: Optional[str],
) -> Optional[str]:
    """Returns the first binary name that exists inside the container."""
    script = " || ".join("command -v %s" % c for c in candidates)
    argv = compose.exec_args(
        compose_file, project_dir, service, ["sh", "-c", script],
        tty=False, project_name=project_name,
    )
    try:
        proc = util.run(argv, capture=True, check=True)
    except util.CommandError:
        return None
    out = (proc.stdout or "").strip().splitlines()
    return os.path.basename(out[0].strip()) if out and out[0].strip() else None


# --- Dump -------------------------------------------------------------------
def dump(
    db: Dict[str, Any], password: Optional[str],
    compose_file: str, project_dir: str, project_name: Optional[str], dumps_dir: str,
    *, dumps_fd=None,
) -> None:
    """Writes one or more dump files to ``dumps_dir``.

    MySQL: a single file ``<service>.sql``. Postgres: optionally ``<service>.globals.sql``
    (roles/passwords via ``pg_dumpall --globals-only``) plus one ``<service>.<db>.sql``
    per database (e.g. Supabase: ``db.postgres.sql`` + ``db._supabase.sql``).
    """
    if db["engine"] == "mysql":
        out_path = _safe_dump_path(dumps_dir, db["service"] + ".sql")
        _dump_mysql(
            db, password, compose_file, project_dir, project_name, out_path,
            dumps_fd=dumps_fd,
        )
    elif db["engine"] == "postgres":
        _dump_postgres(
            db, password, compose_file, project_dir, project_name, dumps_dir,
            dumps_fd=dumps_fd,
        )
    else:
        raise util.CommandError(["dump"], 1, "Unknown engine: %s" % db["engine"])


def _mysql_dump_cmd(tool, auth_user, all_databases, databases, with_events):
    """Builds the mysqldump/mariadb-dump argv. ``with_events`` toggles ``--events``
    so the caller can retry without it (see :func:`_dump_mysql`)."""
    # CAUTION: do NOT add --compact/--skip-comments/--skip-dump-date — they strip the
    # "-- Dump completed" footer that _verify_dump_file uses to detect completeness;
    # without it every valid MySQL dump would be wrongly treated as truncated and abort.
    cmd = [tool, "--single-transaction", "--quick", "--routines", "--triggers"]
    if with_events:
        cmd += ["--events"]
    cmd += ["--default-character-set=utf8mb4", "-h", "127.0.0.1", "-u", auth_user]
    if all_databases:
        cmd += ["--all-databases"]
    else:
        if not databases:
            raise util.CommandError(
                [tool, "--databases"], 2,
                "A scoped MySQL dump requires at least one database.",
            )
        cmd += ["--databases"] + (databases or [])
    return cmd


_MYSQL_SYSTEM_DATABASES = frozenset({
    "information_schema", "mysql", "ndbinfo", "performance_schema", "sys",
})


def _parse_mysql_database_names(stdout: str) -> List[str]:
    """Decode hex-encoded schema names and drop image-owned schemas.

    The resulting names are persisted into the exact snapshot manifest, whose
    strict format deliberately rejects path/control characters.  Fail here
    before creating a dump if a server exposes a name that cannot be represented
    safely and portably by the restore metadata.
    """
    names = []  # type: List[str]
    seen = set()
    for raw in (stdout or "").splitlines():
        encoded = raw.rstrip("\r")
        try:
            if (not encoded or encoded != encoded.strip() or len(encoded) % 2
                    or any(c not in "0123456789abcdefABCDEF" for c in encoded)):
                raise ValueError("invalid hex")
            name = bytes.fromhex(encoded).decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError):
            raise util.CommandError(
                ["mysql", "list-databases"], 1,
                "MySQL returned a malformed hex-encoded database name.",
            )
        if name in _MYSQL_SYSTEM_DATABASES:
            continue
        if (not name or name != name.strip() or len(name) > 256
                or name.startswith("-") or "\0" in name
                or "/" in name or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise util.CommandError(
                ["mysql", "SHOW DATABASES"], 1,
                "MySQL returned an unsafe or unsupported database name: %r" % name,
            )
        if name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        raise util.CommandError(
            ["mysql", "SHOW DATABASES"], 1,
            "No non-system MySQL/MariaDB database is visible to the dump user.",
        )
    if len(names) > 64:
        raise util.CommandError(
            ["mysql", "SHOW DATABASES"], 1,
            "More than 64 non-system databases were found; refusing an "
            "unrepresentable restore manifest.",
        )
    # Stable dump/manifest ordering improves restic deduplication and keeps
    # snapshot metadata reproducible even if the schema catalog order changes.
    names.sort()
    return names


def _mysql_non_system_databases(
    db, password, compose_file, project_dir, project_name,
) -> List[str]:
    """Enumerate every database visible to the configured user except the five
    MySQL/MariaDB system schemas.

    The schema catalog is queried immediately before mysqldump, after the service
    readiness check. This captures user databases created after the original
    docker-backup configuration instead of trusting only the image-init
    ``MYSQL_DATABASE`` value. Names are transferred as hex to avoid delimiter or
    client-escaping ambiguity.
    """
    tool = _probe_tool(
        compose_file, project_dir, db["service"], ["mariadb", "mysql"], project_name,
    ) or "mysql"
    env = {}  # type: Dict[str, str]
    if password:
        env["MYSQL_PWD"] = password
        util.register_secret(password)
    cmd = [
        tool, "--batch", "--skip-column-names",
        "-h", "127.0.0.1", "-u", db["auth_user"],
        "-e", ("SELECT HEX(SCHEMA_NAME) FROM information_schema.SCHEMATA "
               "ORDER BY SCHEMA_NAME"),
    ]
    argv = compose.exec_args(
        compose_file, project_dir, db["service"], cmd,
        env=env, tty=False, project_name=project_name,
    )
    proc = util.run(argv, capture=True, check=True)
    names = _parse_mysql_database_names(proc.stdout or "")
    seeds = db.get("databases") or []
    missing = [name for name in seeds if name not in names]
    if missing:
        raise util.CommandError(
            [tool, "SHOW DATABASES"], 1,
            "Configured application database(s) not visible to dump user '%s': %s"
            % (db["auth_user"], ", ".join(missing)),
        )
    return names


# mariadb-dump/mysqldump abort on ``SHOW EVENTS`` with error 1577 when the server's event
# scheduler is disabled (the MariaDB default: event_scheduler=OFF/DISABLED). --events then
# makes the whole dump fail even though there is nothing to dump. Detect exactly that case
# and retry once without --events instead of failing the backup.
def _is_event_scheduler_error(stderr) -> bool:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    low = (stderr or "").lower()
    return "event scheduler is disabled" in low or (
        "show events" in low and "1577" in low)


def _dump_mysql(
    db, password, compose_file, project_dir, project_name, out_path, *, dumps_fd=None,
) -> None:
    scope = db.get("database_scope")
    if scope not in (None, "non-system"):
        raise util.CommandError(
            ["mysql", "database-scope"], 2,
            "Unsupported dynamic MySQL database scope: %r" % scope,
        )
    if scope == "non-system":
        databases = _mysql_non_system_databases(
            db, password, compose_file, project_dir, project_name,
        )
        db["all_databases"] = False
        db["databases"] = databases
        # The manifest must contain the exact authenticated import plan, not a
        # dynamic source-side instruction.  Config is loaded afresh next run, so
        # removing this in-memory marker does not disable future enumeration.
        db.pop("database_scope", None)
        util.info(
            "MySQL dump scope for '%s': all non-system databases (%s)."
            % (db["service"], ", ".join(databases))
        )
    tool = _probe_tool(
        compose_file, project_dir, db["service"],
        ["mariadb-dump", "mysqldump"], project_name,
    ) or "mysqldump"
    env = {}  # type: Dict[str, str]
    if password:
        env["MYSQL_PWD"] = password
        util.register_secret(password)

    def run(with_events):
        cmd = _mysql_dump_cmd(tool, db["auth_user"], db.get("all_databases"),
                              db.get("databases"), with_events)
        argv = compose.exec_args(compose_file, project_dir, db["service"], cmd,
                                 env=env, tty=False, project_name=project_name)
        _run_to_file(argv, out_path, "mysql", parent_fd=dumps_fd)

    try:
        run(with_events=True)
    except util.CommandError as exc:
        if not _is_event_scheduler_error(exc.stderr):
            raise
        util.warn("mariadb-dump: event scheduler is disabled — retrying dump without "
                  "--events (no scheduled events to back up).")
        run(with_events=False)


# --- Postgres: file layout + argv builder (pure, testable without Docker) ----
_MAINT_DB = "postgres"  # exists in every cluster → maintenance/globals connection


def globals_filename(service: str) -> str:
    return "%s.globals.sql" % service


def db_filename(service: str, dbname: str) -> str:
    return "%s.%s.sql" % (service, dbname)


def postgres_dump_targets(db, dumps_dir):
    """Plans the Postgres dump files: ``(globals_path|None, [(dbname, path), …])``."""
    service = db["service"]
    databases = db.get("databases") or [_MAINT_DB]
    globals_path = (_safe_dump_path(dumps_dir, globals_filename(service))
                    if db.get("dump_globals") else None)
    targets = [(name, _safe_dump_path(dumps_dir, db_filename(service, name)))
               for name in databases]
    return globals_path, targets


def _safe_dump_path(dumps_dir: str, filename: str) -> str:
    """Keep manifest/config-derived dump names below the staging directory."""
    if (not isinstance(filename, str) or not filename or "\0" in filename
            or "/" in filename or "\\" in filename):
        raise util.CommandError(["dump"], 1, "Unsafe dump filename: %r" % filename)
    root = os.path.abspath(dumps_dir)
    candidate = os.path.abspath(os.path.join(root, filename))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained:
        raise util.CommandError(["dump"], 1, "Dump path escapes staging: %r" % filename)
    return candidate


def build_pg_dump_cmd(user: str, dbname: str, preserve_ownership: bool) -> List[str]:
    cmd = ["pg_dump", "--clean", "--if-exists", "-h", "127.0.0.1", "-U", user, "-d", dbname]
    if not preserve_ownership:
        # Generic case: portable, discard owner/privileges.
        cmd += ["--no-owner", "--no-privileges"]
    # With globals (Supabase/superuser) OWNER/GRANT are preserved — roles are restored
    # via the globals, so that e.g. supabase_auth_admin stays the owner.
    return cmd


def build_pg_dumpall_globals_cmd(user: str) -> List[str]:
    return ["pg_dumpall", "--globals-only", "-h", "127.0.0.1", "-U", user]


def quote_ident(name: str) -> str:
    """Quote an SQL identifier in double quotes, doubling embedded ``\"``."""
    return '"%s"' % str(name).replace('"', '""')


def build_create_database_cmd(user: str, dbname: str) -> List[str]:
    return ["psql", "-h", "127.0.0.1", "-U", user, "-d", _MAINT_DB,
            "-c", "CREATE DATABASE %s" % quote_ident(dbname)]


def build_psql_import_cmd(
    user: str, dbname: str, single_transaction: bool = True, on_error_stop: bool = True
) -> List[str]:
    # ON_ERROR_STOP=1 + --single-transaction → the import is atomic: on any error the
    # whole DB restore is rolled back instead of being left half-applied.
    # For the lenient re-import (see _import_db_two_pass) both can be turned off, so that
    # a harmless pg_dump ordering error (circular view dependency on a user-defined type
    # → stub view before CREATE TYPE) does not roll back the entire run.
    cmd = ["psql"]
    if on_error_stop:
        cmd += ["-v", "ON_ERROR_STOP=1"]
    if single_transaction:
        cmd += ["--single-transaction"]
    cmd += ["-h", "127.0.0.1", "-U", user, "-d", dbname]
    return cmd


# Error lines that are expected/harmless during re-import. Two classes:
#  • "… already exists": object from the image init scripts is already there → CREATE no-op.
#  • Failed DROPs ("cannot drop …"): non-destructive — the object stays in place (e.g.
#    inherited partition PKs of the realtime.messages_* tables, the 'extensions' schema
#    created by Supabase along with grant_pg_*_access()). A --clean dump against a freshly
#    initialized supabase/postgres container hits exactly these bootstrap-owned objects.
#    Real incompleteness shows up as failed CREATE/COPY statements, which are still reported.
_BENIGN_IMPORT_ERRORS = (
    "already exists", "existiert bereits", "bereits vorhanden",
    "cannot drop",                 # EN: any failed DROP is non-destructive
    "kann nicht gelöscht werden",  # DE-locale equivalent
)


def _scan_import_errors(stderr: str) -> List[str]:
    out = []  # type: List[str]
    for line in (stderr or "").splitlines():
        low = line.lower()
        if "error" not in low and "fehler" not in low:  # "fehler": DE-locale equivalent
            continue
        if any(b in low for b in _BENIGN_IMPORT_ERRORS):
            continue
        out.append(line.strip())
    return out


def _dump_postgres(
    db, password, compose_file, project_dir, project_name, dumps_dir, *, dumps_fd=None,
) -> None:
    user = db["auth_user"]
    preserve = bool(db.get("dump_globals"))
    env = {}  # type: Dict[str, str]
    if password:
        env["PGPASSWORD"] = password
        util.register_secret(password)
    globals_path, targets = postgres_dump_targets(db, dumps_dir)
    if globals_path:
        argv = compose.exec_args(compose_file, project_dir, db["service"],
                                 build_pg_dumpall_globals_cmd(user), env=env, tty=False,
                                 project_name=project_name)
        _run_to_file(
            argv, globals_path, "postgres-globals", parent_fd=dumps_fd,
        )
        util.info("DB globals (roles/passwords): %s → %s" % (db["service"], globals_path))
    for dbname, path in targets:
        argv = compose.exec_args(compose_file, project_dir, db["service"],
                                 build_pg_dump_cmd(user, dbname, preserve), env=env, tty=False,
                                 project_name=project_name)
        _run_to_file(argv, path, "postgres", parent_fd=dumps_fd)
        util.info("DB dump: %s/%s → %s" % (db["service"], dbname, path))


# --- Dump validation --------------------------------------------------------
# pg_dump/pg_dumpall/mysqldump write a fixed, NON-localized completeness marker at the
# END of a SUCCESSFUL dump. If it is missing, the dump was truncated (container killed,
# disk full, pipe broken) even though the command returned rc=0 — the file is then NOT a
# reliable dump.
_DUMP_COMPLETE_MARKERS = {
    "postgres": b"PostgreSQL database dump complete",
    "postgres-globals": b"PostgreSQL database cluster dump complete",
    "mysql": b"Dump completed",
}

# Schema/data statements. If ALL are missing, the dump has only a header/footer without
# content — usually a misconfigured/empty database (e.g. POSTGRES_DB points at the empty
# 'postgres' maintenance DB instead of the app DB). Only WARN, do not abort: a deliberately
# empty DB is a rare but legitimate backup.
_DUMP_CONTENT_MARKERS = {
    "postgres": (b"\nCREATE ", b"\nCOPY ", b"\nINSERT INTO", b"\nALTER TABLE"),
    "postgres-globals": (b"\nCREATE ROLE", b"\nALTER ROLE"),
    "mysql": (b"\nCREATE TABLE", b"\nINSERT INTO"),
}

_DUMP_HEAD_BYTES = 65536   # schema statements sit right after the SET header
_DUMP_TAIL_BYTES = 8192    # the completeness marker is in the last lines


def _verify_dump_file(out_path: str, kind: str) -> None:
    """Checks a freshly written dump BEFORE it counts as a success.

    HARD (CommandError): file missing, 0 bytes, or the completeness marker at the end is
    missing → truncated/broken despite rc=0. This keeps an empty/half dump from silently
    ending up in the snapshot; the caller aborts with a non-zero exit.
    WARNING: dump is complete but contains no schema/data statement → probably the
    wrong/empty database; report visibly but do not abort.
    """
    try:
        fd = os.open(out_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise util.CommandError(["dump"], 1, "Dump file missing: %s" % out_path)
    try:
        _verify_dump_fd(fd, out_path, kind)
    finally:
        os.close(fd)


def _verify_dump_fd(fd: int, display_path: str, kind: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise util.CommandError(["dump"], 1, "Dump is not a regular file: %s" % display_path)
    size = info.st_size
    if size == 0:
        raise util.CommandError(
            ["dump"], 1, "Dump file is empty (0 bytes): %s" % display_path,
        )
    head = os.pread(fd, _DUMP_HEAD_BYTES, 0)
    if size > _DUMP_HEAD_BYTES:
        tail = os.pread(fd, _DUMP_TAIL_BYTES, max(0, size - _DUMP_TAIL_BYTES))
    else:
        tail = head

    marker = _DUMP_COMPLETE_MARKERS.get(kind)
    if marker and marker not in tail:
        raise util.CommandError(
            ["dump"], 1,
            "Dump '%s' looks truncated — completeness marker missing; NOT counted "
            "as a success." % display_path,
        )

    # Content warning only for SMALL dumps: a complete dump entirely WITHOUT a schema/data
    # statement is tiny (just SET header + footer, a few KB). A large dump necessarily has
    # content — do NOT warn there, or an extensive --clean-DROP/SET preamble would wrongly
    # report "empty" just because the first CREATE line is beyond the header window
    # (size ≤ HEAD ⇒ head is the whole file).
    content = _DUMP_CONTENT_MARKERS.get(kind) or ()
    if content and size <= _DUMP_HEAD_BYTES and not any(m in head for m in content):
        util.warn("Dump '%s' contains no schema/data statement — is the correct "
                  "database configured (e.g. POSTGRES_DB)?" % display_path)


def _run_to_file(
    argv: List[str], out_path: str, kind: str, *, parent_fd=None,
) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: " + util.fmt_argv(argv) + " > " + out_path)
        return
    filename = os.path.basename(out_path)
    if (not filename or filename in (".", "..") or "/" in filename
            or "\\" in filename or "\0" in filename):
        raise util.CommandError(["dump"], 1, "Unsafe dump filename: %r" % filename)
    owned_parent_fd = -1
    if parent_fd is None:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        owned_parent_fd = os.open(
            os.path.dirname(out_path),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_fd = owned_parent_fd
    # Remove the old partial by name (unlink never follows it), then create a
    # brand-new inode with O_EXCL. This makes MySQL's retry safe without ever
    # truncating a symlink/hardlink planted in staging.
    try:
        os.unlink(filename, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    fd = os.open(
        filename,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    os.fchmod(fd, 0o600)
    created_info = os.fstat(fd)
    # capture=True keeps the dump tool's stderr (stdout still goes to the file) so the
    # CommandError carries it — callers inspect it (e.g. the --events fallback in
    # _dump_mysql), and on failure we surface it explicitly since it no longer streams live.
    try:
        with os.fdopen(fd, "wb", closefd=False) as fh:
            util.run(argv, stdout=fh, text=False, capture=True, mutating=False)
            fh.flush()
            os.fsync(fd)
        _verify_dump_fd(fd, out_path, kind)
        current = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if ((current.st_dev, current.st_ino)
                != (created_info.st_dev, created_info.st_ino)):
            raise util.CommandError(
                ["dump"], 1, "Dump path changed while writing: %s" % out_path,
            )
    except util.CommandError as exc:
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        for line in (stderr or "").strip().splitlines():
            util.warn(line)
        raise
    finally:
        os.close(fd)
        if owned_parent_fd >= 0:
            os.close(owned_parent_fd)


# --- Import -----------------------------------------------------------------
def import_dump(
    db: Dict[str, Any], password: Optional[str],
    compose_file: str, project_dir: str, project_name: Optional[str], dumps_dir: str,
    *, dumps_fd=None,
) -> None:
    """Restores the files matching ``dump()`` from ``dumps_dir``."""
    if dumps_fd is not None:
        if not os.path.isdir("/proc/self/fd"):
            raise util.CommandError(
                ["import"], 1,
                "Descriptor-backed database import requires Linux /proc.",
            )
        dumps_dir = "/proc/%d/fd/%d" % (os.getpid(), dumps_fd)
    if db["engine"] == "mysql":
        _import_mysql(db, password, compose_file, project_dir, project_name,
                      _safe_dump_path(dumps_dir, db["service"] + ".sql"))
    elif db["engine"] == "postgres":
        _import_postgres(db, password, compose_file, project_dir, project_name, dumps_dir)
    else:
        raise util.CommandError(["import"], 1, "Unknown engine: %s" % db["engine"])


def _open_regular_dump(path: str):
    """Open a dump through no-follow parent descriptors and verify its inode."""
    parent_fd = fd = -1
    try:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("safe no-follow directory opens are unsupported")
        absolute = os.path.abspath(path)
        fd_prefix = "/proc/%d/fd/" % os.getpid()
        if absolute.startswith(fd_prefix):
            descriptor_and_path = absolute[len(fd_prefix):].split(os.path.sep)
            try:
                root_descriptor = int(descriptor_and_path[0])
            except (ValueError, IndexError):
                raise OSError("invalid descriptor-backed dump path")
            parent_fd = os.dup(root_descriptor)
            if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
                raise OSError("dump root descriptor is not a directory")
            parts = descriptor_and_path[1:]
            if any(part in ("", ".", "..") for part in parts):
                raise OSError("unsafe descriptor-backed dump path")
        else:
            parts = [part for part in absolute.split(os.path.sep) if part]
        if not parts:
            raise OSError("dump path has no filename")
        flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0))
        if parent_fd < 0:
            parent_fd = os.open(os.path.sep, flags)
        for part in parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        filename = parts[-1]
        before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError("dump is not a regular, non-symlink file")
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            current = os.fstat(fd)
            if (not stat.S_ISREG(current.st_mode)
                    or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                raise OSError("dump changed while it was opened")
            os.close(parent_fd)
            parent_fd = -1
            return os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            fd = -1
            raise
    except OSError as exc:
        raise util.CommandError(["import", path], 1, "Unsafe dump file: %s" % exc)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _import_file(argv: List[str], dump_path: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: " + util.fmt_argv(argv) + " < " + dump_path)
        return
    with _open_regular_dump(dump_path) as fh:
        util.run(argv, stdin=fh, text=False, capture=False, mutating=False)


def _import_globals(argv: List[str], globals_path: str) -> None:
    """Globals import WITHOUT ON_ERROR_STOP (see caller), but with error visibility.

    ``psql`` exits with rc=0 here even on individual statement errors, so ``check`` is not
    enough — stderr is scanned for unexpected errors (other than "already exists") even
    after the fact and warned about visibly.
    """
    if util.DRY_RUN:
        util.info("DRY-RUN: " + util.fmt_argv(argv) + " < " + globals_path)
        return
    with _open_regular_dump(globals_path) as fh:
        proc = util.run(argv, stdin=fh, text=True, capture=True, check=False, mutating=False)
    problems = _scan_import_errors(getattr(proc, "stderr", "") or "")
    if problems:
        util.warn("Globals import reported %d unexpected error(s) (check roles/privileges):"
                  % len(problems))
        for line in problems[:10]:
            util.warn("  " + line)


def _import_file_lenient(argv: List[str], dump_path: str) -> List[str]:
    """Imports ``dump_path`` leniently (no ON_ERROR_STOP, no transaction) and returns the
    non-harmless error lines from stderr.

    Used for the second attempt in :func:`_import_db_two_pass`: individual statement errors
    do NOT stop ``psql`` here, so that e.g. a stub view emitted too early by pg_dump
    (circular dependency) is skipped and the later ``CREATE OR REPLACE VIEW`` creates the
    view correctly. stdout is discarded, stderr collected for error visibility.
    """
    if util.DRY_RUN:
        util.info("DRY-RUN: " + util.fmt_argv(argv) + " < " + dump_path)
        return []
    with _open_regular_dump(dump_path) as fh:
        proc = util.run(argv, stdin=fh, text=True, capture=True, check=False, mutating=False)
    return _scan_import_errors(getattr(proc, "stderr", "") or "")


def _import_mysql(db, password, compose_file, project_dir, project_name, dump_path) -> None:
    tool = _probe_tool(
        compose_file, project_dir, db["service"], ["mariadb", "mysql"], project_name,
    ) or "mysql"
    cmd = [tool, "-h", "127.0.0.1", "-u", db["auth_user"]]
    env = {"MYSQL_PWD": password} if password else {}
    if password:
        util.register_secret(password)
    argv = compose.exec_args(compose_file, project_dir, db["service"], cmd,
                             env=env, tty=False, project_name=project_name)
    _import_file(argv, dump_path)


def _ensure_database(db, user, env, dbname, compose_file, project_dir, project_name) -> None:
    """Creates ``dbname`` if it does not exist. ``postgres`` always exists."""
    if dbname == _MAINT_DB:
        return
    argv = compose.exec_args(
        compose_file, project_dir, db["service"],
        build_create_database_cmd(user, dbname),
        env=env, tty=False, project_name=project_name,
    )
    # check=False: "already exists" is expected (init scripts may create _supabase).
    proc = util.run(argv, capture=True, check=False, mutating=True)
    # Do NOT swallow other errors (permission denied, disk full) — warn visibly;
    # the following strict import will then fail loudly anyway.
    if not util.DRY_RUN and getattr(proc, "returncode", 0) != 0:
        for line in _scan_import_errors(getattr(proc, "stderr", "") or ""):
            util.warn("CREATE DATABASE %s: %s" % (dbname, line))


# How many times the dump is retried at most in lenient mode when the strict run fails.
# TWO passes (not one): the FIRST lenient pass already restores ALL objects correctly —
# pg_dump places the actual, self-contained view reconstruction block AFTER ``CREATE TYPE``;
# only the redundant up-front stub view (before the ``DROP TYPE``) fails because the type is
# still missing there. That single harmless error makes the run look "not clean", though.
# The SECOND pass runs through error-free (the type now exists, so the up-front stub view
# also applies) and CONFIRMS the clean state. If errors remain even then, the dump is truly
# broken → fail loudly (see below). Verified empirically on PostgreSQL 14 and 17/Supabase
# (circular views, table columns, domains and composite types over the user type):
# convergence by the second pass at the latest.
_MAX_LENIENT_PASSES = 2


def _import_db_two_pass(
    db, user, env, dbname, dump_path, compose_file, project_dir, project_name
) -> None:
    """Imports ONE database — strict; if that fails, lenient until convergence.

    1) Strict (ON_ERROR_STOP=1 + --single-transaction): if all goes well the import stays
       atomic (all or nothing). Normal case, byte-identical to before.
    2) If the strict run fails (CommandError), --single-transaction rolled it back → clean
       starting state. We re-import the same dump leniently (no ON_ERROR_STOP/no transaction),
       up to ``_MAX_LENIENT_PASSES`` times, stopping as soon as a pass reports no more
       non-harmless errors. The first lenient pass already restores all objects (the pg_dump
       quirk only affects one harmless up-front stub view before ``CREATE TYPE``); the second
       pass confirms the clean state. ``--clean --if-exists`` makes each pass idempotent
       (DROP+CREATE per object → no duplicate data).
    3) If real errors remain after all lenient passes, the dump is not merely mis-ordered
       (corrupt, missing privileges, data constraint …). Then it is NOT counted as a success:
       warn visibly AND raise, so the restore aborts with a non-zero exit instead of silently
       reporting an incomplete database as "done".
    """
    service = db["service"]
    strict_argv = compose.exec_args(
        compose_file, project_dir, service, build_psql_import_cmd(user, dbname),
        env=env, tty=False, project_name=project_name,
    )
    try:
        _import_file(strict_argv, dump_path)
        return
    except util.CommandError as exc:
        util.warn("Strict import of %s/%s failed (rc=%s) — lenient re-import without "
                  "transaction." % (service, dbname, exc.returncode))

    lenient_argv = compose.exec_args(
        compose_file, project_dir, service,
        build_psql_import_cmd(user, dbname, single_transaction=False, on_error_stop=False),
        env=env, tty=False, project_name=project_name,
    )
    problems = []  # type: List[str]
    for attempt in range(1, _MAX_LENIENT_PASSES + 1):
        util.info("Lenient re-import of %s/%s (attempt %d/%d, output is being collected)…"
                  % (service, dbname, attempt, _MAX_LENIENT_PASSES))
        problems = _import_file_lenient(lenient_argv, dump_path)
        if not problems:
            util.info("Lenient re-import of %s/%s completed cleanly."
                      % (service, dbname))
            return

    # Real errors remain after all lenient passes → fail loudly instead of counting it as a
    # success. This makes the caller (restore) correctly abort with a non-zero exit.
    util.warn("Lenient re-import of %s/%s could not resolve %d error(s) after %d attempts "
              "— the database is probably incomplete:"
              % (service, dbname, len(problems), _MAX_LENIENT_PASSES))
    for line in problems[:10]:
        util.warn("  " + line)
    raise util.CommandError(
        lenient_argv, 1,
        "Import of %s/%s incomplete after %d lenient attempts."
        % (service, dbname, _MAX_LENIENT_PASSES),
    )


def _import_postgres(db, password, compose_file, project_dir, project_name, dumps_dir) -> None:
    user = db["auth_user"]
    env = {"PGPASSWORD": password} if password else {}
    if password:
        util.register_secret(password)
    globals_path, targets = postgres_dump_targets(db, dumps_dir)

    # 1) Globals first. CREATE ROLE "already exists" is expected (init scripts may have
    #    created roles already) → without ON_ERROR_STOP, so the following
    #    ALTER ROLE … (passwords/attributes) still take effect. Real errors (other than
    #    "already exists") are NOT swallowed, though — they are warned about visibly.
    if globals_path and (util.DRY_RUN or os.path.exists(globals_path)):
        argv = compose.exec_args(
            compose_file, project_dir, db["service"],
            ["psql", "-h", "127.0.0.1", "-U", user, "-d", _MAINT_DB],
            env=env, tty=False, project_name=project_name,
        )
        util.info("Importing globals (roles/passwords) for '%s'." % db["service"])
        _import_globals(argv, globals_path)

    # 2) per database: create if needed, then import atomically (ON_ERROR_STOP + transaction).
    imported = 0
    for dbname, path in targets:
        if not (util.DRY_RUN or os.path.exists(path)):
            util.warn("No dump for %s/%s at %s." % (db["service"], dbname, path))
            continue
        _ensure_database(db, user, env, dbname, compose_file, project_dir, project_name)
        _import_db_two_pass(db, user, env, dbname, path,
                            compose_file, project_dir, project_name)
        util.info("DB dump imported: %s/%s" % (db["service"], dbname))
        imported += 1

    # 3) Fallback: old single-file dump <service>.sql (snapshots from before this feature).
    if imported == 0 and not util.DRY_RUN:
        _import_legacy_single(db, user, env, compose_file, project_dir, project_name, dumps_dir)


def _import_legacy_single(db, user, env, compose_file, project_dir, project_name, dumps_dir) -> None:
    legacy = _safe_dump_path(dumps_dir, db["service"] + ".sql")
    if not os.path.exists(legacy):
        util.warn("No Postgres dumps found for '%s'." % db["service"])
        return
    target_db = (db.get("databases") or [_MAINT_DB])[0]
    util.info("Importing legacy dump %s → %s" % (legacy, target_db))
    _import_db_two_pass(db, user, env, target_db, legacy,
                        compose_file, project_dir, project_name)


# --- Readiness probe --------------------------------------------------------
def _ping(db, password, compose_file, project_dir, project_name) -> bool:
    if db["engine"] == "mysql":
        script = ("mariadb-admin ping -h 127.0.0.1 --silent 2>/dev/null || "
                  "mysqladmin ping -h 127.0.0.1 --silent 2>/dev/null")
        cmd = ["sh", "-c", script]
        env = {"MYSQL_PWD": password} if password else {}
    else:
        cmd = ["pg_isready", "-h", "127.0.0.1", "-U", db["auth_user"]]
        env = {"PGPASSWORD": password} if password else {}
    if password:
        util.register_secret(password)
    argv = compose.exec_args(compose_file, project_dir, db["service"], cmd,
                             env=env, tty=False, project_name=project_name)
    try:
        util.run(argv, capture=True, check=True)
        return True
    except util.CommandError:
        return False


def wait_ready(
    db, password, compose_file, project_dir, project_name, timeout: float = 120.0
) -> bool:
    if util.DRY_RUN:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ping(db, password, compose_file, project_dir, project_name):
            return True
        time.sleep(2)
    return False
