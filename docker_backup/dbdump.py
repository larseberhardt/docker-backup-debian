"""Consistent DB dumps and imports via ``docker compose exec``.

Passwords are passed into the exec environment via ``MYSQL_PWD`` / ``PGPASSWORD``
(not into the argv of the in-container process) and scrubbed before any logging.
"""

from __future__ import annotations

import os
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
) -> None:
    """Writes one or more dump files to ``dumps_dir``.

    MySQL: a single file ``<service>.sql``. Postgres: optionally ``<service>.globals.sql``
    (roles/passwords via ``pg_dumpall --globals-only``) plus one ``<service>.<db>.sql``
    per database (e.g. Supabase: ``db.postgres.sql`` + ``db._supabase.sql``).
    """
    if db["engine"] == "mysql":
        out_path = os.path.join(dumps_dir, db["service"] + ".sql")
        _dump_mysql(db, password, compose_file, project_dir, project_name, out_path)
    elif db["engine"] == "postgres":
        _dump_postgres(db, password, compose_file, project_dir, project_name, dumps_dir)
    else:
        raise util.CommandError(["dump"], 1, "Unknown engine: %s" % db["engine"])


def _dump_mysql(db, password, compose_file, project_dir, project_name, out_path) -> None:
    tool = _probe_tool(
        compose_file, project_dir, db["service"],
        ["mariadb-dump", "mysqldump"], project_name,
    ) or "mysqldump"
    # CAUTION: do NOT add --compact/--skip-comments/--skip-dump-date — they strip the
    # "-- Dump completed" footer that _verify_dump_file uses to detect completeness;
    # without it every valid MySQL dump would be wrongly treated as truncated and abort.
    cmd = [
        tool, "--single-transaction", "--quick", "--routines", "--triggers", "--events",
        "--default-character-set=utf8mb4", "-h", "127.0.0.1", "-u", db["auth_user"],
    ]
    if db.get("all_databases"):
        cmd += ["--all-databases"]
    else:
        dbs = db.get("databases") or []
        cmd += ["--databases"] + dbs
    env = {}  # type: Dict[str, str]
    if password:
        env["MYSQL_PWD"] = password
        util.register_secret(password)
    argv = compose.exec_args(compose_file, project_dir, db["service"], cmd,
                             env=env, tty=False, project_name=project_name)
    _run_to_file(argv, out_path, "mysql")


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
    globals_path = (os.path.join(dumps_dir, globals_filename(service))
                    if db.get("dump_globals") else None)
    targets = [(name, os.path.join(dumps_dir, db_filename(service, name)))
               for name in databases]
    return globals_path, targets


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


def _dump_postgres(db, password, compose_file, project_dir, project_name, dumps_dir) -> None:
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
        _run_to_file(argv, globals_path, "postgres-globals")
        util.info("DB globals (roles/passwords): %s → %s" % (db["service"], globals_path))
    for dbname, path in targets:
        argv = compose.exec_args(compose_file, project_dir, db["service"],
                                 build_pg_dump_cmd(user, dbname, preserve), env=env, tty=False,
                                 project_name=project_name)
        _run_to_file(argv, path, "postgres")
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
        size = os.path.getsize(out_path)
    except OSError:
        raise util.CommandError(["dump"], 1, "Dump file missing: %s" % out_path)
    if size == 0:
        raise util.CommandError(["dump"], 1, "Dump file is empty (0 bytes): %s" % out_path)

    with open(out_path, "rb") as fh:
        head = fh.read(_DUMP_HEAD_BYTES)
        if size > _DUMP_HEAD_BYTES:
            fh.seek(-_DUMP_TAIL_BYTES, os.SEEK_END)
            tail = fh.read()
        else:
            tail = head

    marker = _DUMP_COMPLETE_MARKERS.get(kind)
    if marker and marker not in tail:
        raise util.CommandError(
            ["dump"], 1,
            "Dump '%s' looks truncated — completeness marker missing; NOT counted "
            "as a success." % out_path,
        )

    # Content warning only for SMALL dumps: a complete dump entirely WITHOUT a schema/data
    # statement is tiny (just SET header + footer, a few KB). A large dump necessarily has
    # content — do NOT warn there, or an extensive --clean-DROP/SET preamble would wrongly
    # report "empty" just because the first CREATE line is beyond the header window
    # (size ≤ HEAD ⇒ head is the whole file).
    content = _DUMP_CONTENT_MARKERS.get(kind) or ()
    if content and size <= _DUMP_HEAD_BYTES and not any(m in head for m in content):
        util.warn("Dump '%s' contains no schema/data statement — is the correct "
                  "database configured (e.g. POSTGRES_DB)?" % out_path)


def _run_to_file(argv: List[str], out_path: str, kind: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: " + util.fmt_argv(argv) + " > " + out_path)
        return
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # 0600: the dump is the complete plaintext database — never world-readable
    # (default umask would give 0644).
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        util.run(argv, stdout=fh, text=False, capture=False, mutating=False)
    # rc=0 does NOT mean "complete": a truncated/empty dump must not end up in the snapshot
    # as a success (the only previous safeguard was the exit code).
    _verify_dump_file(out_path, kind)


# --- Import -----------------------------------------------------------------
def import_dump(
    db: Dict[str, Any], password: Optional[str],
    compose_file: str, project_dir: str, project_name: Optional[str], dumps_dir: str,
) -> None:
    """Restores the files matching ``dump()`` from ``dumps_dir``."""
    if db["engine"] == "mysql":
        _import_mysql(db, password, compose_file, project_dir, project_name,
                      os.path.join(dumps_dir, db["service"] + ".sql"))
    elif db["engine"] == "postgres":
        _import_postgres(db, password, compose_file, project_dir, project_name, dumps_dir)
    else:
        raise util.CommandError(["import"], 1, "Unknown engine: %s" % db["engine"])


def _import_file(argv: List[str], dump_path: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: " + util.fmt_argv(argv) + " < " + dump_path)
        return
    with open(dump_path, "rb") as fh:
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
    with open(globals_path, "rb") as fh:
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
    with open(dump_path, "rb") as fh:
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
    legacy = os.path.join(dumps_dir, db["service"] + ".sql")
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
