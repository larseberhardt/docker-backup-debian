from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import dbdump, util


class PgDumpCmdTest(unittest.TestCase):
    def test_generic_dump_strips_owner_and_privileges(self):
        argv = dbdump.build_pg_dump_cmd("postgres", "appdb", preserve_ownership=False)
        self.assertEqual(argv[0], "pg_dump")
        self.assertIn("--clean", argv)
        self.assertIn("--if-exists", argv)
        self.assertIn("--no-owner", argv)
        self.assertIn("--no-privileges", argv)
        self.assertEqual(argv[argv.index("-U") + 1], "postgres")
        self.assertEqual(argv[argv.index("-d") + 1], "appdb")

    def test_preserve_ownership_keeps_owner_and_privileges(self):
        argv = dbdump.build_pg_dump_cmd("supabase_admin", "postgres", preserve_ownership=True)
        self.assertNotIn("--no-owner", argv)
        self.assertNotIn("--no-privileges", argv)
        self.assertEqual(argv[argv.index("-U") + 1], "supabase_admin")

    def test_globals_cmd_is_globals_only(self):
        argv = dbdump.build_pg_dumpall_globals_cmd("supabase_admin")
        self.assertEqual(argv[0], "pg_dumpall")
        self.assertIn("--globals-only", argv)
        self.assertEqual(argv[argv.index("-U") + 1], "supabase_admin")


class PgDumpTargetsTest(unittest.TestCase):
    def test_supabase_plan_has_globals_and_two_dbs(self):
        db = {"service": "db", "auth_user": "supabase_admin",
              "databases": ["postgres", "_supabase"], "dump_globals": True}
        globals_path, targets = dbdump.postgres_dump_targets(db, "/stage/dumps")
        self.assertEqual(globals_path, "/stage/dumps/db.globals.sql")
        self.assertEqual(targets, [
            ("postgres", "/stage/dumps/db.postgres.sql"),
            ("_supabase", "/stage/dumps/db._supabase.sql"),
        ])

    def test_generic_plan_has_no_globals_and_one_db(self):
        db = {"service": "db", "auth_user": "postgres",
              "databases": ["appdb"], "dump_globals": False}
        globals_path, targets = dbdump.postgres_dump_targets(db, "/stage/dumps")
        self.assertIsNone(globals_path)
        self.assertEqual(targets, [("appdb", "/stage/dumps/db.appdb.sql")])

    def test_defaults_to_postgres_db(self):
        db = {"service": "db", "auth_user": "postgres"}
        globals_path, targets = dbdump.postgres_dump_targets(db, "/d")
        self.assertIsNone(globals_path)
        self.assertEqual(targets, [("postgres", "/d/db.postgres.sql")])

    def test_filenames(self):
        self.assertEqual(dbdump.globals_filename("db"), "db.globals.sql")
        self.assertEqual(dbdump.db_filename("db", "_supabase"), "db._supabase.sql")

    def test_database_name_cannot_escape_dump_directory(self):
        db = {"service": "db", "databases": ["../../tmp/payload"]}
        with self.assertRaises(util.CommandError):
            dbdump.postgres_dump_targets(db, "/stage/dumps")

    def test_dump_open_does_not_follow_racing_parent_symlink(self):
        root = os.path.realpath(tempfile.mkdtemp())
        try:
            dumps = os.path.join(root, "dumps")
            parked = os.path.join(root, "dumps-before-race")
            outside = os.path.join(root, "outside")
            os.makedirs(dumps)
            os.makedirs(outside)
            dump_path = os.path.join(dumps, "db.sql")
            with open(dump_path, "wb") as fh:
                fh.write(b"trusted dump")
            with open(os.path.join(outside, "db.sql"), "wb") as fh:
                fh.write(b"attacker dump")
            real_open = os.open
            fired = {"value": False}

            def racing_open(path, flags, *args, **kwargs):
                if path == "db.sql" and not fired["value"]:
                    fired["value"] = True
                    os.rename(dumps, parked)
                    os.symlink(outside, dumps)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(dbdump.os, "open", side_effect=racing_open):
                with dbdump._open_regular_dump(dump_path) as fh:
                    self.assertEqual(fh.read(), b"trusted dump")
            self.assertTrue(fired["value"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_dump_open_can_stay_on_retained_staging_descriptor(self):
        root = os.path.realpath(tempfile.mkdtemp())
        staging_fd = -1
        try:
            staging = os.path.join(root, "dumps")
            parked = os.path.join(root, "dumps-original")
            os.makedirs(staging)
            with open(os.path.join(staging, "db.sql"), "wb") as fh:
                fh.write(b"trusted dump")
            staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
            os.rename(staging, parked)
            os.makedirs(staging)
            with open(os.path.join(staging, "db.sql"), "wb") as fh:
                fh.write(b"replacement dump")
            descriptor_path = "/proc/%d/fd/%d/db.sql" % (os.getpid(), staging_fd)

            with dbdump._open_regular_dump(descriptor_path) as fh:
                self.assertEqual(fh.read(), b"trusted dump")
        finally:
            if staging_fd >= 0:
                os.close(staging_fd)
            shutil.rmtree(root, ignore_errors=True)


class PgImportCmdTest(unittest.TestCase):
    def test_import_is_atomic_and_strict(self):
        argv = dbdump.build_psql_import_cmd("supabase_admin", "_supabase")
        self.assertEqual(argv[0], "psql")
        self.assertIn("--single-transaction", argv)
        self.assertEqual(argv[argv.index("-v") + 1], "ON_ERROR_STOP=1")
        self.assertEqual(argv[argv.index("-d") + 1], "_supabase")

    def test_import_single_transaction_can_be_disabled(self):
        argv = dbdump.build_psql_import_cmd("postgres", "appdb", single_transaction=False)
        self.assertNotIn("--single-transaction", argv)
        self.assertIn("ON_ERROR_STOP=1", argv)

    def test_import_lenient_disables_both_guards(self):
        argv = dbdump.build_psql_import_cmd(
            "postgres", "appdb", single_transaction=False, on_error_stop=False)
        self.assertEqual(argv[0], "psql")
        self.assertNotIn("--single-transaction", argv)
        self.assertNotIn("ON_ERROR_STOP=1", argv)
        self.assertNotIn("-v", argv)
        self.assertEqual(argv[argv.index("-d") + 1], "appdb")

    def test_create_database_quotes_identifier(self):
        argv = dbdump.build_create_database_cmd("supabase_admin", "_supabase")
        self.assertIn('CREATE DATABASE "_supabase"', argv)

    def test_quote_ident_escapes_embedded_quote(self):
        self.assertEqual(dbdump.quote_ident('weird"name'), '"weird""name"')


class ScanImportErrorsTest(unittest.TestCase):
    def test_benign_already_exists_is_ignored(self):
        stderr = 'ERROR:  role "anon" already exists\n'
        self.assertEqual(dbdump._scan_import_errors(stderr), [])

    def test_real_error_is_surfaced(self):
        stderr = ('ERROR:  role "anon" already exists\n'
                  'ERROR:  permission denied to create role\n')
        out = dbdump._scan_import_errors(stderr)
        self.assertEqual(len(out), 1)
        self.assertIn("permission denied", out[0])

    def test_non_error_lines_ignored(self):
        self.assertEqual(dbdump._scan_import_errors("NOTICE: something\nSET\n"), [])

    def test_german_real_error_is_surfaced(self):
        # psql in a German locale: 'FEHLER' instead of 'ERROR' (pins the 'fehler' keyword).
        # The German literals below are real German-locale psql output being matched.
        out = dbdump._scan_import_errors('FEHLER:  Typ »public.x[]« existiert nicht\n')
        self.assertEqual(len(out), 1)
        self.assertIn("existiert nicht", out[0])

    def test_german_benign_already_exists_is_ignored(self):
        # German-locale psql output matched by _BENIGN_IMPORT_ERRORS — do not translate.
        stderr = ('FEHLER:  Rolle „anon" existiert bereits\n'
                  'FEHLER:  Schema „public" ist bereits vorhanden\n')
        self.assertEqual(dbdump._scan_import_errors(stderr), [])

    def test_benign_failed_drops_are_ignored(self):
        # --clean against a freshly initialized supabase/postgres container hits the
        # bootstrap-owned objects: inherited partition PKs (realtime.messages_*) and the
        # 'extensions' schema along with grant_pg_*_access(). Failed DROPs are
        # non-destructive → not an error.
        stderr = (
            'ERROR:  cannot drop inherited constraint "messages_2026_07_03_pkey" '
            'of relation "messages_2026_07_03"\n'
            'ERROR:  cannot drop function extensions.grant_pg_net_access() '
            'because other objects depend on it\n'
            'ERROR:  cannot drop schema extensions because other objects depend on it\n'
        )
        self.assertEqual(dbdump._scan_import_errors(stderr), [])

    def test_german_benign_failed_drop_is_ignored(self):
        # German locale: "… kann nicht gelöscht werden …" — real psql output being matched.
        stderr = ('FEHLER:  geerbter Constraint „messages_2026_07_03_pkey" '
                  'kann nicht gelöscht werden\n')
        self.assertEqual(dbdump._scan_import_errors(stderr), [])

    def test_failed_drop_does_not_mask_real_create_error(self):
        # A real CREATE/COPY error stays visible despite the tolerated DROPs.
        stderr = (
            'ERROR:  cannot drop schema extensions because other objects depend on it\n'
            'ERROR:  permission denied for schema public\n'
        )
        out = dbdump._scan_import_errors(stderr)
        self.assertEqual(len(out), 1)
        self.assertIn("permission denied", out[0])


class TwoPassImportTest(unittest.TestCase):
    """Strict import first; if it fails, lenient until convergence — otherwise raise."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.dump = os.path.join(self.tmp, "db.appdb.sql")
        with open(self.dump, "w") as f:
            f.write("SELECT 1;\n")
        self.db = {"service": "db"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        util.set_dry_run(False)

    def _two_pass(self):
        dbdump._import_db_two_pass(
            self.db, "postgres", {"PGPASSWORD": "S3CRET"}, "appdb", self.dump,
            "/c/docker-compose.yml", "/c", "proj",
        )

    @staticmethod
    def _argv(call):
        # side_effect stores (argv, kwargs) tuples
        return call[0]

    def test_strict_success_skips_lenient(self):
        with mock.patch.object(dbdump.util, "run") as run, \
                mock.patch.object(dbdump.util, "warn") as warn, \
                mock.patch.object(dbdump.util, "info"):
            run.return_value = subprocess.CompletedProcess([], 0)
            self._two_pass()
        self.assertEqual(run.call_count, 1)  # only the strict run
        # strict run relies on util.run's default check=True (no check kwarg)
        self.assertTrue(run.call_args.kwargs.get("check", True))
        warn.assert_not_called()

    def test_lenient_converges_on_second_pass(self):
        # Real case of the pg_dump quirk: 1st lenient pass still dirty (type missing at the
        # stub), 2nd pass clean (type now exists) → NO raise.
        calls = []

        def side_effect(argv, **kw):
            calls.append((argv, kw))
            if len(calls) == 1:
                raise util.CommandError(argv, 3, "")            # strict fails
            if len(calls) == 2:
                return subprocess.CompletedProcess(             # lenient pass 1: dirty
                    argv, 0, "", 'ERROR:  type "public.x[]" does not exist\n')
            return subprocess.CompletedProcess(                 # lenient pass 2: clean
                argv, 0, "", 'ERROR:  role "anon" already exists\n')

        with mock.patch.object(dbdump.util, "run", side_effect=side_effect), \
                mock.patch.object(dbdump.util, "warn") as warn, \
                mock.patch.object(dbdump.util, "info"):
            self._two_pass()  # must NOT raise

        self.assertEqual(len(calls), 3)  # strict + 2 lenient runs
        # strict run: both guards on
        self.assertIn("ON_ERROR_STOP=1", self._argv(calls[0]))
        self.assertIn("--single-transaction", self._argv(calls[0]))
        # lenient runs: both off, and explicitly check=False
        for c in calls[1:]:
            self.assertNotIn("ON_ERROR_STOP=1", self._argv(c))
            self.assertNotIn("--single-transaction", self._argv(c))
            self.assertIs(c[1].get("check"), False)
        # no "could not resolve" alarm, since clean at the end
        self.assertFalse(any("could not resolve" in c.args[0] for c in warn.call_args_list))

    def test_lenient_first_pass_clean_returns_early(self):
        calls = []

        def side_effect(argv, **kw):
            calls.append((argv, kw))
            if len(calls) == 1:
                raise util.CommandError(argv, 1, "")
            return subprocess.CompletedProcess(argv, 0, "", "NOTICE: all good\n")

        with mock.patch.object(dbdump.util, "run", side_effect=side_effect), \
                mock.patch.object(dbdump.util, "warn"), \
                mock.patch.object(dbdump.util, "info"):
            self._two_pass()

        self.assertEqual(len(calls), 2)  # strict + ONE lenient run (early exit)

    def test_unrecovered_errors_raise(self):
        # If the errors persist across all lenient runs → raise loudly (restore exit ≠ 0).
        calls = []

        def side_effect(argv, **kw):
            calls.append((argv, kw))
            if len(calls) == 1:
                raise util.CommandError(argv, 3, "")
            return subprocess.CompletedProcess(
                argv, 0, "", "ERROR:  permission denied for schema public\n")

        with mock.patch.object(dbdump.util, "run", side_effect=side_effect), \
                mock.patch.object(dbdump.util, "warn") as warn, \
                mock.patch.object(dbdump.util, "info"):
            with self.assertRaises(util.CommandError):
                self._two_pass()

        self.assertEqual(len(calls), 3)  # strict + 2 lenient runs, then raise
        warned = "\n".join(c.args[0] for c in warn.call_args_list)
        self.assertIn("could not resolve", warned)
        self.assertIn("permission denied", warned)

    def test_benign_failed_drops_do_not_raise(self):
        # Real Supabase case: strict fails (circular stub view), the lenient run only reports
        # non-destructive --clean DROPs on bootstrap-owned objects → NO raise, the restore
        # counts as successful.
        calls = []

        def side_effect(argv, **kw):
            calls.append((argv, kw))
            if len(calls) == 1:
                raise util.CommandError(argv, 3, "")            # strict fails
            return subprocess.CompletedProcess(
                argv, 0, "",
                'ERROR:  cannot drop inherited constraint "messages_2026_07_03_pkey" '
                'of relation "messages_2026_07_03"\n'
                'ERROR:  cannot drop schema extensions because other objects depend on it\n')

        with mock.patch.object(dbdump.util, "run", side_effect=side_effect), \
                mock.patch.object(dbdump.util, "warn") as warn, \
                mock.patch.object(dbdump.util, "info"):
            self._two_pass()  # must NOT raise

        self.assertEqual(len(calls), 2)  # strict + ONE lenient run (clean early)
        self.assertFalse(any("could not resolve" in c.args[0] for c in warn.call_args_list))

    def test_dry_run_only_logs_strict(self):
        util.set_dry_run(True)
        with mock.patch.object(dbdump.util, "run") as run, \
                mock.patch.object(dbdump.util, "warn") as warn:
            self._two_pass()
        run.assert_not_called()       # DRY-RUN executes nothing
        warn.assert_not_called()      # no fallback in DRY-RUN


class ImportPostgresFlowTest(unittest.TestCase):
    """Integration test for _import_postgres: create-before-import, skip, legacy fallback."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.common = ("/c/docker-compose.yml", "/c", "proj")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        util.set_dry_run(False)

    def _write(self, name):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write("SELECT 1;\n")

    def test_ensure_database_runs_before_import_for_non_maint_db(self):
        self._write("db.appdb.sql")
        db = {"service": "db", "auth_user": "postgres",
              "databases": ["appdb"], "dump_globals": False}
        calls = []
        with mock.patch.object(dbdump.util, "run",
                               side_effect=lambda a, **k: calls.append(a)
                               or subprocess.CompletedProcess(a, 0, "", "")), \
                mock.patch.object(dbdump.util, "info"), \
                mock.patch.object(dbdump.util, "warn"):
            dbdump._import_postgres(db, None, *self.common, self.tmp)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("CREATE DATABASE" in str(a) for a in calls[0]))  # create first
        self.assertIn("ON_ERROR_STOP=1", calls[1])                          # then (strict) import

    def test_maintenance_db_is_not_created(self):
        self._write("db.postgres.sql")
        db = {"service": "db", "auth_user": "postgres",
              "databases": ["postgres"], "dump_globals": False}
        calls = []
        with mock.patch.object(dbdump.util, "run",
                               side_effect=lambda a, **k: calls.append(a)
                               or subprocess.CompletedProcess(a, 0, "", "")), \
                mock.patch.object(dbdump.util, "info"), \
                mock.patch.object(dbdump.util, "warn"):
            dbdump._import_postgres(db, None, *self.common, self.tmp)
        self.assertEqual(len(calls), 1)  # 'postgres' always exists → only the import
        self.assertFalse(any("CREATE DATABASE" in str(a) for a in calls[0]))

    def test_missing_dump_warns_and_falls_back_to_legacy(self):
        # No db.appdb.sql and no legacy db.sql → nothing imported, two warnings.
        db = {"service": "db", "auth_user": "postgres",
              "databases": ["appdb"], "dump_globals": False}
        with mock.patch.object(dbdump.util, "run") as run, \
                mock.patch.object(dbdump.util, "info"), \
                mock.patch.object(dbdump.util, "warn") as warn:
            dbdump._import_postgres(db, None, *self.common, self.tmp)
        run.assert_not_called()
        warned = "\n".join(c.args[0] for c in warn.call_args_list)
        self.assertIn("No dump", warned)             # per-DB skip
        self.assertIn("No Postgres dumps", warned)   # legacy fallback empty


if __name__ == "__main__":
    unittest.main()
