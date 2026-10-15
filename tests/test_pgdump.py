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


# Tail of a Supabase pg_dump (plain format, PG 17) as seen on a real production source:
# trigger issue_pg_cron_access owned by supabase_admin with its function owned by postgres,
# issue_pg_net_access owned by postgres. supautils refuses both CREATEs as written.
_EVT_DUMP = '''\
--
-- Name: secrets; Type: TABLE DATA; Schema: app; Owner: postgres
--

COPY app.secrets (id, note) FROM stdin;
1\tlooks like a header
-- Name: evil; Type: EVENT TRIGGER; Schema: -; Owner: postgres
CREATE EVENT TRIGGER evil ON ddl_command_end
   EXECUTE FUNCTION public.evil();
ALTER EVENT TRIGGER evil OWNER TO postgres;
\\.


--
-- Name: issue_pg_cron_access; Type: EVENT TRIGGER; Schema: -; Owner: supabase_admin
--

CREATE EVENT TRIGGER issue_pg_cron_access ON ddl_command_end
         WHEN TAG IN ('CREATE EXTENSION')
   EXECUTE FUNCTION extensions.grant_pg_cron_access();


ALTER EVENT TRIGGER issue_pg_cron_access OWNER TO supabase_admin;

--
-- Name: issue_pg_net_access; Type: EVENT TRIGGER; Schema: -; Owner: postgres
--

CREATE EVENT TRIGGER issue_pg_net_access ON ddl_command_end
         WHEN TAG IN ('CREATE EXTENSION')
   EXECUTE FUNCTION extensions.grant_pg_net_access();


ALTER EVENT TRIGGER issue_pg_net_access OWNER TO postgres;

--
-- Name: Odd "Name"; Type: EVENT TRIGGER; Schema: -; Owner: Ops Admin
--

CREATE EVENT TRIGGER "Odd ""Name""" ON sql_drop
   EXECUTE FUNCTION "My Schema".watch();

ALTER EVENT TRIGGER "Odd ""Name""" DISABLE;


ALTER EVENT TRIGGER "Odd ""Name""" OWNER TO "Ops Admin";

--
-- Name: EVENT TRIGGER "Odd ""Name"""; Type: COMMENT; Schema: -; Owner: Ops Admin
--

COMMENT ON EVENT TRIGGER "Odd ""Name""" IS 'it''s
multi-line';


--
-- PostgreSQL database dump complete
--

'''

_SUPAUTILS_ERRORS = (
    "ERROR:  Superuser owned event trigger must execute a superuser owned function\n"
    'ERROR:  event trigger "issue_pg_cron_access" does not exist\n'
)


class EventTriggerRepairTest(unittest.TestCase):
    """Event triggers that supautils refuses to replay are recreated with original owners."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.dump = os.path.join(self.tmp, "db.postgres.sql")
        with open(self.dump, "w") as f:
            f.write(_EVT_DUMP)
        self.db = {"service": "db"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        util.set_dry_run(False)

    def test_parse_keeps_owner_function_state_and_comment(self):
        triggers = dbdump.parse_event_triggers(self.dump)
        self.assertEqual([t["name"] for t in triggers],
                         ["issue_pg_cron_access", "issue_pg_net_access", 'Odd "Name"'])
        cron, net, odd = triggers
        self.assertEqual((cron["owner"], cron["function"], cron["state"]),
                         ("supabase_admin", "extensions.grant_pg_cron_access", "O"))
        self.assertTrue(cron["create"].startswith("CREATE EVENT TRIGGER issue_pg_cron_access"))
        self.assertTrue(cron["create"].endswith("grant_pg_cron_access();"))
        self.assertEqual(cron["extra"], [])
        self.assertEqual(net["owner"], "postgres")
        self.assertEqual((odd["owner"], odd["function"], odd["state"]),
                         ('"Ops Admin"', '"My Schema".watch', "D"))
        self.assertEqual(odd["extra"], [
            'ALTER EVENT TRIGGER "Odd ""Name""" DISABLE;',
            "COMMENT ON EVENT TRIGGER \"Odd \"\"Name\"\"\" IS 'it''s\nmulti-line';",
        ])

    def test_copy_data_is_never_parsed_as_toc(self):
        names = [t["name"] for t in dbdump.parse_event_triggers(self.dump)]
        self.assertNotIn("evil", names)

    def test_no_owner_dump_is_not_repaired(self):
        with open(self.dump, "w") as f:
            f.write(_EVT_DUMP.replace(
                "ALTER EVENT TRIGGER issue_pg_net_access OWNER TO postgres;\n", ""))
        self.assertIsNone(dbdump.parse_event_triggers(self.dump))

    def test_repair_sql_creates_as_owner_and_verifies(self):
        sql = dbdump.build_event_trigger_repair_sql(dbdump.parse_event_triggers(self.dump))
        self.assertEqual(sql.count("DO $docker_backup$"), 3)
        self.assertIn("SET standard_conforming_strings = on;", sql)
        self.assertIn("pg_catalog.to_regrole('supabase_admin')", sql)
        self.assertIn("pg_catalog.to_regprocedure('extensions.grant_pg_cron_access' || '()')",
                      sql)
        self.assertIn("'SET ROLE %s'", sql)
        # The CREATE is embedded verbatim as a literal (quotes doubled).
        self.assertIn("EXECUTE 'CREATE EVENT TRIGGER issue_pg_cron_access ON ddl_command_end\n"
                      "         WHEN TAG IN (''CREATE EXTENSION'')", sql)
        self.assertIn("pg_catalog.to_regrole('\"Ops Admin\"')", sql)
        self.assertIn("EXECUTE 'COMMENT ON EVENT TRIGGER \"Odd \"\"Name\"\"\" IS ''it''''s\n"
                      "multi-line'';';", sql)
        self.assertIn("AND e.evtenabled = 'D'", sql)

    def test_repair_sql_refuses_dollar_tag_collision(self):
        triggers = dbdump.parse_event_triggers(self.dump)
        triggers[0]["create"] += " -- $docker_backup$"
        with self.assertRaises(ValueError):
            dbdump.build_event_trigger_repair_sql(triggers)

    def test_only_event_trigger_problems(self):
        lines = dbdump._scan_import_errors(_SUPAUTILS_ERRORS)
        self.assertTrue(dbdump._only_event_trigger_problems(lines))
        self.assertFalse(dbdump._only_event_trigger_problems(
            lines + ["ERROR:  permission denied for schema public"]))
        self.assertFalse(dbdump._only_event_trigger_problems([]))

    def _two_pass(self, side_effect):
        calls = []

        def record(argv, **kw):
            calls.append((argv, kw))
            return side_effect(len(calls), argv, kw)

        with mock.patch.object(dbdump.util, "run", side_effect=record), \
                mock.patch.object(dbdump.util, "warn") as warn, \
                mock.patch.object(dbdump.util, "info"):
            try:
                dbdump._import_db_two_pass(
                    self.db, "supabase_admin", {}, "postgres", self.dump,
                    "/c/docker-compose.yml", "/c", "proj",
                )
                raised = False
            except util.CommandError:
                raised = True
        return calls, raised, "\n".join(c.args[0] for c in warn.call_args_list)

    def test_event_trigger_only_errors_are_repaired(self):
        def side_effect(n, argv, kw):
            if n == 1:
                raise util.CommandError(argv, 3, "")           # strict fails
            if n == 2:
                return subprocess.CompletedProcess(argv, 0, "", _SUPAUTILS_ERRORS)
            return subprocess.CompletedProcess(                 # repair succeeds
                argv, 0, "",
                "WARNING:  docker-backup: event trigger issue_pg_cron_access (owner "
                "supabase_admin, superuser) runs … owned by non-superuser postgres\n")

        calls, raised, warned = self._two_pass(side_effect)
        self.assertFalse(raised)
        self.assertEqual(len(calls), 3)  # strict + ONE lenient + repair
        repair_argv, repair_kw = calls[2]
        self.assertIn("ON_ERROR_STOP=1", repair_argv)          # atomic repair
        self.assertIn("--single-transaction", repair_argv)
        self.assertIn("CREATE EVENT TRIGGER issue_pg_net_access", repair_kw["input"])
        self.assertIs(repair_kw["check"], False)
        self.assertIn("non-superuser postgres", warned)        # escalation path surfaced
        self.assertNotIn("could not resolve", warned)

    def test_failed_repair_still_raises(self):
        def side_effect(n, argv, kw):
            if n == 1:
                raise util.CommandError(argv, 3, "")
            if "input" in kw:                                   # repair attempts fail
                return subprocess.CompletedProcess(
                    argv, 3, "", "ERROR:  docker-backup: event trigger x does not match\n")
            return subprocess.CompletedProcess(argv, 0, "", _SUPAUTILS_ERRORS)

        calls, raised, warned = self._two_pass(side_effect)
        self.assertTrue(raised)
        self.assertEqual(len(calls), 5)  # strict + 2 × (lenient + repair)
        self.assertIn("Recreating the event triggers", warned)
        self.assertIn("could not resolve", warned)

    def test_mixed_errors_are_not_repaired(self):
        def side_effect(n, argv, kw):
            if n == 1:
                raise util.CommandError(argv, 3, "")
            return subprocess.CompletedProcess(
                argv, 0, "", _SUPAUTILS_ERRORS + "ERROR:  permission denied for table x\n")

        calls, raised, _warned = self._two_pass(side_effect)
        self.assertTrue(raised)
        self.assertEqual(len(calls), 3)  # strict + 2 lenient, never a repair
        self.assertFalse(any("input" in kw for _argv, kw in calls))


class DefaultPrivilegesResetTest(unittest.TestCase):
    """Privilege-preserving imports must not inherit the target's default privileges."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.common = ("/c/docker-compose.yml", "/c", "proj")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        util.set_dry_run(False)

    @staticmethod
    def _is_reset(call):
        return "pg_default_acl" in (call[1].get("input") or "")

    def _import(self, dump_globals, side_effect=None):
        with open(os.path.join(self.tmp, "db.postgres.sql"), "w") as f:
            f.write("SELECT 1;\n")
        db = {"service": "db", "auth_user": "supabase_admin",
              "databases": ["postgres"], "dump_globals": dump_globals}
        calls = []

        def record(argv, **kw):
            calls.append((argv, kw))
            if side_effect:
                return side_effect(len(calls), argv, kw)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(dbdump.util, "run", side_effect=record), \
                mock.patch.object(dbdump.util, "info"), \
                mock.patch.object(dbdump.util, "warn"):
            dbdump._import_postgres(db, None, *self.common, self.tmp)
        return calls

    def test_reset_runs_before_strict_import(self):
        calls = self._import(dump_globals=True)
        self.assertEqual([self._is_reset(c) for c in calls], [True, False])
        reset_argv = calls[0][0]
        self.assertIn("ON_ERROR_STOP=1", reset_argv)
        self.assertIn("--single-transaction", reset_argv)
        self.assertEqual(reset_argv[reset_argv.index("-d") + 1], "postgres")

    def test_reset_runs_before_every_lenient_pass(self):
        # Pass 1 recreates the source's default privileges at its end; pass 2 must not
        # create its objects under them.
        def side_effect(n, argv, kw):
            if n == 2:
                raise util.CommandError(argv, 3, "")             # strict fails
            if n == 4:
                return subprocess.CompletedProcess(               # lenient 1: dirty
                    argv, 0, "", 'ERROR:  type "public.x[]" does not exist\n')
            return subprocess.CompletedProcess(argv, 0, "", "")

        calls = self._import(dump_globals=True, side_effect=side_effect)
        self.assertEqual([self._is_reset(c) for c in calls],
                         [True, False, True, False, True, False])

    def test_no_reset_for_dumps_without_privileges(self):
        # --no-privileges dumps rely on the target's default privileges.
        calls = self._import(dump_globals=False)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self._is_reset(calls[0]))

    def test_failed_reset_aborts_before_import(self):
        def side_effect(n, argv, kw):
            return subprocess.CompletedProcess(argv, 3, "", "ERROR:  boom\n")

        with self.assertRaises(util.CommandError):
            self._import(dump_globals=True, side_effect=side_effect)

    def test_dry_run_runs_nothing(self):
        util.set_dry_run(True)
        calls = self._import(dump_globals=True)
        self.assertEqual(calls, [])


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
