"""Consistent DB dumps and imports via ``docker compose exec``.

Passwords are passed into the exec environment via ``MYSQL_PWD`` / ``PGPASSWORD``
(not into the argv of the in-container process) and scrubbed before any logging.
"""

from __future__ import annotations

import os
import re
import stat
import time
from typing import Any, Dict, List, Optional, Tuple

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
        if any(not isinstance(d, str) or not d or d.startswith("-") for d in databases):
            raise util.CommandError(
                [tool, "--databases"], 2,
                "Invalid MySQL database name in config: %r" % (databases,),
            )
        cmd += ["--databases"] + list(databases)
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


# Objects that a ``--no-owner`` dump would silently hand over to the importing role.
# Schema ``public`` itself is skipped: pg_dump never recreates it, so its owner comes
# from the target's own initialization. Extension members follow their extension.
# Role names are hex-encoded so no name can break the ``hex|count`` line format.
_PG_FOREIGN_OWNER_SQL = """\
WITH user_ns AS (
  SELECT oid, nspname, nspowner FROM pg_catalog.pg_namespace
  WHERE nspname NOT IN ('pg_catalog', 'information_schema')
    AND nspname NOT LIKE 'pg\\_toast%' AND nspname NOT LIKE 'pg\\_temp\\_%'
), ext AS (
  SELECT classid, objid FROM pg_catalog.pg_depend WHERE deptype = 'e'
), owned AS (
  SELECT n.nspowner AS owner FROM user_ns n
  WHERE n.nspname <> 'public' AND NOT EXISTS (SELECT 1 FROM ext
    WHERE ext.classid = 'pg_catalog.pg_namespace'::regclass AND ext.objid = n.oid)
  UNION ALL
  SELECT c.relowner FROM pg_catalog.pg_class c JOIN user_ns n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND NOT EXISTS (SELECT 1 FROM ext
    WHERE ext.classid = 'pg_catalog.pg_class'::regclass AND ext.objid = c.oid)
  UNION ALL
  SELECT t.typowner FROM pg_catalog.pg_type t JOIN user_ns n ON n.oid = t.typnamespace
  WHERE t.typtype IN ('e', 'd', 'r', 'm') AND NOT EXISTS (SELECT 1 FROM ext
    WHERE ext.classid = 'pg_catalog.pg_type'::regclass AND ext.objid = t.oid)
  UNION ALL
  SELECT p.proowner FROM pg_catalog.pg_proc p JOIN user_ns n ON n.oid = p.pronamespace
  WHERE NOT EXISTS (SELECT 1 FROM ext
    WHERE ext.classid = 'pg_catalog.pg_proc'::regclass AND ext.objid = p.oid)
)
SELECT encode(convert_to(r.rolname, 'UTF8'), 'hex') || '|' || count(*)
FROM owned JOIN pg_catalog.pg_roles r ON r.oid = owned.owner
WHERE r.rolname <> current_user AND r.rolname <> 'pg_database_owner'
GROUP BY r.rolname ORDER BY r.rolname"""


def build_pg_owner_probe_cmd(user: str, dbname: str) -> List[str]:
    return ["psql", "-X", "-tAq", "-v", "ON_ERROR_STOP=1",
            "-h", "127.0.0.1", "-U", user, "-d", dbname, "-c", _PG_FOREIGN_OWNER_SQL]


def parse_pg_owner_rows(stdout: str) -> List[Tuple[str, int]]:
    """Decode ``hex(role)|count`` lines from the owner probe."""
    rows = []  # type: List[Tuple[str, int]]
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        encoded, sep, count = line.partition("|")
        try:
            if not sep or not encoded or not count.isdigit():
                raise ValueError("malformed row")
            role = bytes.fromhex(encoded).decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError):
            raise util.CommandError(
                ["psql", "owner-probe"], 1,
                "Postgres owner probe returned a malformed row: %r" % line[:80],
            )
        rows.append((role, int(count)))
    return rows


def postgres_foreign_owners(
    db: Dict[str, Any], password: Optional[str],
    compose_file: str, project_dir: str, project_name: Optional[str],
) -> List[Tuple[str, str, int]]:
    """``(database, role, object count)`` for objects NOT owned by the dump role.

    A dump with ``--no-owner`` restores all of these as the importing role, so an
    application that connects as one of those roles loses ownership of its own
    schema after a restore (e.g. ``permission denied for schema`` on migrations).
    """
    env = {"PGPASSWORD": password} if password else {}
    if password:
        util.register_secret(password)
    _globals_path, targets = postgres_dump_targets(db, "/")
    out = []  # type: List[Tuple[str, str, int]]
    for dbname, _path in targets:
        argv = compose.exec_args(
            compose_file, project_dir, db["service"],
            build_pg_owner_probe_cmd(db["auth_user"], dbname),
            env=env, tty=False, project_name=project_name,
        )
        proc = util.run(argv, capture=True, check=True)
        out.extend((dbname, role, count)
                   for role, count in parse_pg_owner_rows(proc.stdout or ""))
    return out


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
    problems = _problems_of(proc)
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
    return _problems_of(proc)


def _problems_of(proc) -> List[str]:
    """Unexpected error lines of an import run — PLUS the exit code.

    stderr scanning alone is blind to failures whose message lacks the word
    "error" (Compose: "service is not running"; a psql killed by the OOM
    killer prints nothing at all). Such a run must never count as clean."""
    problems = _scan_import_errors(getattr(proc, "stderr", "") or "")
    rc = getattr(proc, "returncode", 0)
    if rc != 0:
        tail = (getattr(proc, "stderr", "") or "").strip().splitlines()
        detail = tail[-1].strip() if tail else "(no stderr)"
        problems.insert(0, "psql exited with rc=%s: %s" % (rc, detail[:200]))
    return problems


# --- Default privileges of the restore target --------------------------------
# The target's default privileges apply to every object the dump creates (the
# supabase/postgres image grants ALL on new public tables/functions/sequences to anon and
# authenticated). pg_dump writes GRANT/REVOKE only relative to Postgres' built-in
# defaults, so those extra privileges would never be revoked and a restore would
# silently widen access (seen on Supabase: anon gained ALL on ~100 public tables the
# source had locked down). Privilege-preserving dumps therefore reset the target's
# default privileges to the built-in state before EVERY import pass, which is the state
# pg_dump expects; the dump re-creates the source's own ALTER DEFAULT PRIVILEGES at its end.
_RESET_DEFAULT_PRIVILEGES_SQL = """\
DO $docker_backup$
DECLARE
  d record;
  grantee_id oid;
  kind text;
  scope text;
BEGIN
  FOR d IN SELECT a.defaclrole, a.defaclnamespace, a.defaclobjtype, a.defaclacl
           FROM pg_catalog.pg_default_acl a LOOP
    kind := CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
      WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' WHEN 'n' THEN 'SCHEMAS'
      WHEN 'L' THEN 'LARGE OBJECTS' END;
    scope := pg_catalog.format('FOR ROLE %s', d.defaclrole::pg_catalog.regrole);
    IF d.defaclnamespace <> 0 THEN
      scope := scope || pg_catalog.format(' IN SCHEMA %s',
        d.defaclnamespace::pg_catalog.regnamespace);
    END IF;
    FOR grantee_id IN SELECT DISTINCT x.grantee FROM pg_catalog.aclexplode(d.defaclacl) x LOOP
      EXECUTE pg_catalog.format('ALTER DEFAULT PRIVILEGES %s REVOKE ALL ON %s FROM %s',
        scope, kind,
        CASE WHEN grantee_id = 0 THEN 'PUBLIC' ELSE grantee_id::pg_catalog.regrole::text END);
    END LOOP;
    -- A global entry goes back to the built-in default (owner, plus PUBLIC for
    -- functions/types); Postgres drops the row once it equals that default.
    IF d.defaclnamespace = 0 THEN
      EXECUTE pg_catalog.format('ALTER DEFAULT PRIVILEGES %s GRANT ALL ON %s TO %s',
        scope, kind, d.defaclrole::pg_catalog.regrole);
      IF d.defaclobjtype IN ('f', 'T') THEN
        EXECUTE pg_catalog.format('ALTER DEFAULT PRIVILEGES %s GRANT ALL ON %s TO PUBLIC',
          scope, kind);
      END IF;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_default_acl) THEN
    RAISE EXCEPTION 'docker-backup: default privileges could not be reset';
  END IF;
END
$docker_backup$;
"""


def _reset_default_privileges(
    db, user, env, dbname, compose_file, project_dir, project_name
) -> None:
    """Resets the target's default privileges (see above); raises if that fails, since
    the following import would otherwise grant more than the source did."""
    service = db["service"]
    argv = compose.exec_args(
        compose_file, project_dir, service, build_psql_import_cmd(user, dbname),
        env=env, tty=False, project_name=project_name,
    )
    if util.DRY_RUN:
        util.info("DRY-RUN: reset default privileges: " + util.fmt_argv(argv))
        return
    proc = util.run(argv, input=_RESET_DEFAULT_PRIVILEGES_SQL, text=True, capture=True,
                    check=False, mutating=False)
    if getattr(proc, "returncode", 0) != 0:
        problems = _problems_of(proc)
        for line in problems[:10]:
            util.warn("  " + line)
        raise util.CommandError(
            argv, proc.returncode,
            "Could not reset the default privileges of %s/%s before the import — "
            "the restored privileges would differ from the source." % (service, dbname),
        )


# --- Event triggers under supautils (Supabase) --------------------------------
# The supabase/postgres image preloads supautils, which refuses CREATE EVENT TRIGGER
# unless the creating role and the trigger function's owner are both superusers or both
# not ("Superuser owned event trigger must execute a superuser owned function"). pg_dump
# gives every function its final owner early, but creates all event triggers at the very
# end as the importing superuser and only then hands them to their owner. A source whose
# trigger owner and function owner differ in that respect (seen on Supabase: trigger
# issue_pg_cron_access owned by supabase_admin, its function by postgres; trigger
# issue_pg_net_access owned by postgres) is valid there but cannot be replayed verbatim:
# the CREATE fails and the following ALTER … OWNER TO reports "does not exist".
# When those are the ONLY errors left after a lenient pass, the dumped event triggers are
# recreated with their original owners and verified against the dump (see
# build_event_trigger_repair_sql), so the restore reproduces the source state exactly.
_EVENT_TRIGGER_ERROR_MARKERS = ("event trigger", "ereignistrigger")  # DE-locale equivalent

# pg_dump's fmtId(): lower-case simple identifiers stay bare, everything else is quoted.
_PG_IDENT = r'(?:"(?:[^"]|"")+"|[a-z_][a-z0-9_]*)'
_EVT_CREATE_RE = re.compile(
    r"^(CREATE EVENT TRIGGER (?P<name>%s) ON [a-z_]+\n"
    r"(?: +WHEN TAG IN \([^\n]*\)\n)?"
    r" +EXECUTE (?:FUNCTION|PROCEDURE) (?P<func>[^\n]+)\(\);)$" % _PG_IDENT, re.M)
_EVT_STATE_RE = re.compile(
    r"^ALTER EVENT TRIGGER (?P<name>%s) "
    r"(?P<state>DISABLE|ENABLE(?: ALWAYS| REPLICA)?);$" % _PG_IDENT, re.M)
_EVT_OWNER_RE = re.compile(
    r"^ALTER EVENT TRIGGER (?P<name>%s) OWNER TO (?P<owner>%s);$"
    % (_PG_IDENT, _PG_IDENT), re.M)
_EVT_DEPENDENT_RE = re.compile(
    r"^((?:COMMENT ON|SECURITY LABEL FOR %s ON) EVENT TRIGGER (?P<name>%s) IS "
    r"(?:'(?:[^']|'')*'|NULL);)$" % (_PG_IDENT, _PG_IDENT), re.M)
_EVT_STATES = {"ENABLE": "O", "DISABLE": "D", "ENABLE ALWAYS": "A", "ENABLE REPLICA": "R"}
_REPAIR_DOLLAR_TAG = "$docker_backup$"

# One block per dumped event trigger, all run in ONE transaction with ON_ERROR_STOP.
# A missing trigger is created AS its owner (SET ROLE). Only when owner and function
# owner differ in superuser status — the combination supautils refuses — the function is
# handed to the trigger owner for the CREATE and back right after; its ACL must come out
# unchanged. Every trigger (recreated or not) is then checked against the dump.
_EVT_REPAIR_BLOCK = """\
DO $docker_backup$
DECLARE
  trg name := %(name)s;
  fn oid := pg_catalog.to_regprocedure(%(func)s || '()');
  trg_owner oid := pg_catalog.to_regrole(%(owner)s);
  fn_owner oid;
  fn_acl pg_catalog.aclitem[];
  swap boolean;
BEGIN
  IF fn IS NULL OR trg_owner IS NULL THEN
    RAISE EXCEPTION 'docker-backup: event trigger %%: function or owner role is missing', trg;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger e WHERE e.evtname = trg) THEN
    SELECT p.proowner, p.proacl INTO fn_owner, fn_acl
      FROM pg_catalog.pg_proc p WHERE p.oid = fn;
    swap := (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.oid = trg_owner)
      IS DISTINCT FROM (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.oid = fn_owner);
    IF swap THEN
      EXECUTE pg_catalog.format('ALTER FUNCTION %%s OWNER TO %%s',
        fn::pg_catalog.regprocedure, trg_owner::pg_catalog.regrole);
    END IF;
    EXECUTE pg_catalog.format('SET ROLE %%s', trg_owner::pg_catalog.regrole);
    EXECUTE %(create)s;
    RESET ROLE;
    IF swap THEN
      EXECUTE pg_catalog.format('ALTER FUNCTION %%s OWNER TO %%s',
        fn::pg_catalog.regprocedure, fn_owner::pg_catalog.regrole);
      IF (SELECT p.proacl FROM pg_catalog.pg_proc p WHERE p.oid = fn)
          IS DISTINCT FROM fn_acl THEN
        RAISE EXCEPTION 'docker-backup: privileges of %% changed while recreating '
          'event trigger %%', fn::pg_catalog.regprocedure, trg;
      END IF;
      IF (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.oid = trg_owner) THEN
        RAISE WARNING 'docker-backup: event trigger %% (owner %%, superuser) runs %% owned '
          'by non-superuser %% - restored as in the source; supautils refuses to create '
          'this combination because that role can run code as superuser through it',
          trg, trg_owner::pg_catalog.regrole, fn::pg_catalog.regprocedure,
          fn_owner::pg_catalog.regrole;
      END IF;
    END IF;
%(extra)s  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_event_trigger e
      WHERE e.evtname = trg AND e.evtowner = trg_owner AND e.evtfoid = fn
        AND e.evtenabled = %(state)s) THEN
    RAISE EXCEPTION 'docker-backup: event trigger %% does not match the dump', trg;
  END IF;
END
$docker_backup$;
"""


def _sql_literal(value: str) -> str:
    """Standard-conforming SQL string literal (the repair script sets the GUC)."""
    return "'%s'" % value.replace("'", "''")


def _unquote_ident(ident: str) -> str:
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident


def _event_trigger_sql(dump_path: str) -> Tuple[str, int]:
    """The plain-format TOC entries of all event triggers (plus comments/security labels
    on them) and the number of EVENT TRIGGER entries. COPY data is skipped, so table
    contents can never be mistaken for a TOC header."""
    chunks = []  # type: List[bytes]
    entries = 0
    capture = in_copy = False
    with _open_regular_dump(dump_path) as fh:
        for line in fh:
            if in_copy:
                if line.rstrip(b"\r\n") == b"\\.":
                    in_copy = False
                continue
            if line.startswith(b"-- Name: ") or line.startswith(b"-- Data for Name: "):
                is_trigger = b"; Type: EVENT TRIGGER; " in line
                if is_trigger:
                    entries += 1
                capture = is_trigger or (
                    line.startswith(b"-- Name: EVENT TRIGGER ")
                    and (b"; Type: COMMENT; " in line
                         or b"; Type: SECURITY LABEL; " in line))
                continue
            if line.startswith(b"COPY ") and line.rstrip(b"\r\n").endswith(b" FROM stdin;"):
                in_copy = True
                capture = False
                continue
            if capture:
                chunks.append(line)
    return b"".join(chunks).decode("utf-8", "replace"), entries


def parse_event_triggers(dump_path: str) -> Optional[List[Dict[str, Any]]]:
    """The dumped event triggers as dicts (name, owner, function, state, create, extra).

    ``None`` when the dump cannot be mapped unambiguously — an unexpected format, or no
    ``OWNER TO`` (dump taken with --no-owner) — so the caller keeps the original errors
    instead of guessing."""
    sql, entries = _event_trigger_sql(dump_path)
    owners = {m.group("name"): m.group("owner") for m in _EVT_OWNER_RE.finditer(sql)}
    states = {m.group("name"): m.group("state") for m in _EVT_STATE_RE.finditer(sql)}
    dependents = {}  # type: Dict[str, List[str]]
    for m in _EVT_DEPENDENT_RE.finditer(sql):
        dependents.setdefault(m.group("name"), []).append(m.group(1))
    triggers = []  # type: List[Dict[str, Any]]
    for m in _EVT_CREATE_RE.finditer(sql):
        qname = m.group("name")
        if qname not in owners:
            return None
        extra = []  # type: List[str]
        if qname in states:
            extra.append("ALTER EVENT TRIGGER %s %s;" % (qname, states[qname]))
        triggers.append({
            "name": _unquote_ident(qname),
            "owner": owners[qname],
            "function": m.group("func"),
            "state": _EVT_STATES[states.get(qname, "ENABLE")],
            "create": m.group(1),
            "extra": extra + dependents.get(qname, []),
        })
    if not triggers or len(triggers) != entries:
        return None
    return triggers


def build_event_trigger_repair_sql(triggers: List[Dict[str, Any]]) -> str:
    """SQL that recreates missing ``triggers`` with their original owners and verifies all
    of them (see _EVT_REPAIR_BLOCK). Run it with ON_ERROR_STOP in one transaction."""
    parts = ["SET standard_conforming_strings = on;\n",
             "SELECT pg_catalog.set_config('search_path', '', false);\n"]
    for trg in triggers:
        embedded = [trg["name"], trg["function"], trg["owner"], trg["create"]] + trg["extra"]
        if any(_REPAIR_DOLLAR_TAG in text for text in embedded):
            raise ValueError("event trigger definition contains %s" % _REPAIR_DOLLAR_TAG)
        parts.append(_EVT_REPAIR_BLOCK % {
            "name": _sql_literal(trg["name"]),
            "func": _sql_literal(trg["function"]),
            "owner": _sql_literal(trg["owner"]),
            "create": _sql_literal(trg["create"]),
            "state": _sql_literal(trg["state"]),
            "extra": "".join("    EXECUTE %s;\n" % _sql_literal(stmt) for stmt in trg["extra"]),
        })
    return "".join(parts)


def _only_event_trigger_problems(problems: List[str]) -> bool:
    return bool(problems) and all(
        any(marker in line.lower() for marker in _EVENT_TRIGGER_ERROR_MARKERS)
        for line in problems)


def _recreate_event_triggers(
    db, user, env, dbname, dump_path, compose_file, project_dir, project_name
) -> bool:
    """Recreates the dumped event triggers with their original owners; True if all of
    them now match the dump. Atomic: on any failure nothing is changed."""
    service = db["service"]
    triggers = parse_event_triggers(dump_path)
    try:
        sql = build_event_trigger_repair_sql(triggers) if triggers else ""
    except ValueError:
        sql = ""
    if not sql:
        util.warn("The event-trigger errors of %s/%s cannot be mapped to the dump — "
                  "not repaired." % (service, dbname))
        return False
    util.info("Only event-trigger errors remain in %s/%s (supautils rejects pg_dump's "
              "statement order) — recreating the %d dumped event trigger(s) with their "
              "original owners…" % (service, dbname, len(triggers)))
    argv = compose.exec_args(
        compose_file, project_dir, service, build_psql_import_cmd(user, dbname),
        env=env, tty=False, project_name=project_name,
    )
    proc = util.run(argv, input=sql, text=True, capture=True, check=False, mutating=False)
    if getattr(proc, "returncode", 0) != 0:
        util.warn("Recreating the event triggers of %s/%s failed (nothing changed):"
                  % (service, dbname))
        for line in _problems_of(proc)[:10]:
            util.warn("  " + line)
        return False
    for line in (getattr(proc, "stderr", "") or "").splitlines():
        if "docker-backup:" in line:
            util.warn(line.split("docker-backup:", 1)[1].strip())
    util.info("Event triggers of %s/%s match the dump (owners, functions and state "
              "verified)." % (service, dbname))
    return True


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
        for line in _problems_of(proc):
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
    db, user, env, dbname, dump_path, compose_file, project_dir, project_name,
    reset_default_privileges: bool = False,
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
       (DROP+CREATE per object → no duplicate data). If a pass leaves ONLY event-trigger
       errors (supautils on Supabase, see above), the dumped event triggers are recreated
       with their original owners and verified; the pass then counts as clean.
    3) If real errors remain after all lenient passes, the dump is not merely mis-ordered
       (corrupt, missing privileges, data constraint …). Then it is NOT counted as a success:
       warn visibly AND raise, so the restore aborts with a non-zero exit instead of silently
       reporting an incomplete database as "done".

    With ``reset_default_privileges`` (privilege-preserving dumps) the target's default
    privileges are reset before the strict run and before every lenient pass — see
    _reset_default_privileges.
    """
    service = db["service"]
    strict_argv = compose.exec_args(
        compose_file, project_dir, service, build_psql_import_cmd(user, dbname),
        env=env, tty=False, project_name=project_name,
    )
    if reset_default_privileges:
        _reset_default_privileges(
            db, user, env, dbname, compose_file, project_dir, project_name)
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
        if reset_default_privileges:
            _reset_default_privileges(
                db, user, env, dbname, compose_file, project_dir, project_name)
        problems = _import_file_lenient(lenient_argv, dump_path)
        if _only_event_trigger_problems(problems) and _recreate_event_triggers(
                db, user, env, dbname, dump_path,
                compose_file, project_dir, project_name):
            problems = []
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
        # Only dumps that carry privileges (dump_globals → no --no-privileges) can
        # re-create the source's default privileges themselves.
        _import_db_two_pass(db, user, env, dbname, path,
                            compose_file, project_dir, project_name,
                            reset_default_privileges=bool(db.get("dump_globals")))
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
