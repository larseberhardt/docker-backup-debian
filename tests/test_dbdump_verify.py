from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import dbdump, detect, util


# Real modern pg_dump output (14+/17): \restrict prolog AFTER the header and a
# \unrestrict line AFTER the completeness footer. Verifies that the marker still lands in
# the 8192-byte tail despite the trailer, and the content markers in the head despite the prolog.
_PG_COMPLETE = (
    "--\n-- PostgreSQL database dump\n--\n\n"
    "\\restrict aBc123DeF456\n\n"
    "SET statement_timeout = 0;\nSET client_encoding = 'UTF8';\n\n"
    "CREATE TABLE public.users (id integer NOT NULL);\n"
    "COPY public.users (id) FROM stdin;\n1\n\\.\n\n"
    "--\n-- PostgreSQL database dump complete\n--\n\n"
    "\\unrestrict aBc123DeF456\n"
)
# Complete, but without schema/data (e.g. wrong/empty POSTGRES_DB).
_PG_EMPTY = (
    "--\n-- PostgreSQL database dump\n--\n\n"
    "SET statement_timeout = 0;\nSET client_encoding = 'UTF8';\n\n"
    "--\n-- PostgreSQL database dump complete\n--\n"
)
# Truncated: marker missing (container killed / disk full at rc=0).
_PG_TRUNCATED = (
    "--\n-- PostgreSQL database dump\n--\n\n"
    "SET statement_timeout = 0;\n\nCREATE TABLE public.users (id integer);\n"
    "COPY public.users (id) FROM stdin;\n1\n2\n"  # cuts off in the middle of the COPY
)
_PG_GLOBALS = (
    "--\n-- PostgreSQL database cluster dump\n--\n\n"
    "ALTER ROLE postgres WITH SUPERUSER;\n\n"
    "--\n-- PostgreSQL database cluster dump complete\n--\n"
)


def _mysql_hex_names(*names):
    return "\n".join(name.encode("utf-8").hex() for name in names) + "\n"
_MYSQL_COMPLETE = (
    "-- MySQL dump\n\nCREATE TABLE `t` (`id` int);\nINSERT INTO `t` VALUES (1);\n"
    "-- Dump completed on 2026-07-01 03:00:00\n"
)
# Complete (footer present) but table-less → only the non-fatal content warning.
_MYSQL_EMPTY = "-- MySQL dump\n\n-- Dump completed on 2026-07-01 03:00:00\n"
# Truncated: "Dump completed" footer missing (rc=0 despite a half dump).
_MYSQL_TRUNCATED = "-- MySQL dump\n\nCREATE TABLE `t` (`id` int);\nINSERT INTO `t` VALUES (1),(2),"
_PG_GLOBALS_TRUNCATED = (
    "--\n-- PostgreSQL database cluster dump\n--\n\nALTER ROLE postgres WITH SUPERUSER;\n"
)


class VerifyDumpFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        util.set_dry_run(False)

    def _write(self, text: str, name: str = "db.appdb.sql") -> str:
        p = os.path.join(self.dir, name)
        with open(p, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return p

    def test_complete_postgres_dump_passes(self):
        dbdump._verify_dump_file(self._write(_PG_COMPLETE), "postgres")  # no raise

    def test_globals_dump_passes(self):
        dbdump._verify_dump_file(self._write(_PG_GLOBALS), "postgres-globals")

    def test_mysql_dump_passes(self):
        dbdump._verify_dump_file(self._write(_MYSQL_COMPLETE), "mysql")

    def test_missing_file_raises(self):
        with self.assertRaises(util.CommandError):
            dbdump._verify_dump_file(os.path.join(self.dir, "nope.sql"), "postgres")

    def test_zero_byte_raises(self):
        with self.assertRaises(util.CommandError):
            dbdump._verify_dump_file(self._write(""), "postgres")

    def test_truncated_dump_raises(self):
        # rc=0, but completeness marker missing → abort hard.
        with self.assertRaises(util.CommandError):
            dbdump._verify_dump_file(self._write(_PG_TRUNCATED), "postgres")

    def test_truncated_mysql_raises(self):
        with self.assertRaises(util.CommandError):
            dbdump._verify_dump_file(self._write(_MYSQL_TRUNCATED), "mysql")

    def test_truncated_globals_raises(self):
        with self.assertRaises(util.CommandError):
            dbdump._verify_dump_file(self._write(_PG_GLOBALS_TRUNCATED), "postgres-globals")

    def test_empty_but_complete_warns_not_raises(self):
        with mock.patch.object(dbdump.util, "warn") as warn:
            dbdump._verify_dump_file(self._write(_PG_EMPTY), "postgres")  # no raise
        self.assertTrue(warn.called)

    def test_empty_mysql_warns_not_raises(self):
        with mock.patch.object(dbdump.util, "warn") as warn:
            dbdump._verify_dump_file(self._write(_MYSQL_EMPTY), "mysql")  # no raise
        self.assertTrue(warn.called)

    def test_large_dump_with_late_content_does_not_warn(self):
        # Huge --clean preamble (only DROP/SET) → first CREATE line beyond 64 KiB.
        # The content warning is limited to small dumps, so it must NOT fire here.
        preamble = "DROP TABLE IF EXISTS public.t;\n" * 4000  # > 64 KiB, no content marker
        big = ("--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n"
               + preamble
               + "CREATE TABLE public.t (id int);\nCOPY public.t (id) FROM stdin;\n1\n\\.\n\n"
               + "--\n-- PostgreSQL database dump complete\n--\n")
        self.assertGreater(len(big.encode()), dbdump._DUMP_HEAD_BYTES)
        with mock.patch.object(dbdump.util, "warn") as warn:
            dbdump._verify_dump_file(self._write(big), "postgres")  # no raise (footer present)
        self.assertFalse(warn.called)

    def test_marker_found_in_tail_of_large_dump(self):
        # Large dump (> HEAD): marker lies beyond the first 64 KiB, in the tail.
        big = ("--\n-- PostgreSQL database dump\n--\nCREATE TABLE public.t (id int);\n"
               + "-- filler filler filler\n" * 6000
               + "--\n-- PostgreSQL database dump complete\n--\n")
        self.assertGreater(len(big.encode()), dbdump._DUMP_HEAD_BYTES)
        dbdump._verify_dump_file(self._write(big), "postgres")  # no raise


class RunToFileVerifiesTest(unittest.TestCase):
    """_run_to_file must verify the written dump after the (mocked) run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        util.set_dry_run(False)

    def test_truncated_output_makes_run_to_file_raise(self):
        out = os.path.join(self.dir, "db.appdb.sql")

        def fake_run(argv, stdout=None, **kw):
            stdout.write(_PG_TRUNCATED.encode("utf-8"))  # rc=0, but incomplete
            return mock.Mock(returncode=0)

        with mock.patch.object(dbdump.util, "run", side_effect=fake_run), \
                mock.patch.object(dbdump.util, "info"):
            with self.assertRaises(util.CommandError):
                dbdump._run_to_file(["pg_dump"], out, "postgres")

    def test_complete_output_passes(self):
        out = os.path.join(self.dir, "db.appdb.sql")

        def fake_run(argv, stdout=None, **kw):
            stdout.write(_PG_COMPLETE.encode("utf-8"))
            return mock.Mock(returncode=0)

        with mock.patch.object(dbdump.util, "run", side_effect=fake_run), \
                mock.patch.object(dbdump.util, "info"):
            dbdump._run_to_file(["pg_dump"], out, "postgres")  # no raise
        self.assertTrue(os.path.getsize(out) > 0)


class MysqlEventsFallbackTest(unittest.TestCase):
    """--events must be dropped and the dump retried when the event scheduler is disabled."""

    def setUp(self):
        util.set_dry_run(False)

    def test_events_error_string_is_recognized(self):
        err = ("mariadb-dump: Couldn't execute 'show events': Cannot proceed, because "
               "event scheduler is disabled (1577)")
        self.assertTrue(dbdump._is_event_scheduler_error(err))
        self.assertTrue(dbdump._is_event_scheduler_error(err.encode("utf-8")))
        self.assertFalse(dbdump._is_event_scheduler_error("some other failure"))

    def test_cmd_toggles_events_flag(self):
        with_events = dbdump._mysql_dump_cmd("mariadb-dump", "root", True, None, True)
        without = dbdump._mysql_dump_cmd("mariadb-dump", "root", True, None, False)
        self.assertIn("--events", with_events)
        self.assertNotIn("--events", without)
        self.assertEqual(with_events[-1], "--all-databases")

    def test_scoped_root_dump_names_only_the_application_database(self):
        cmd = dbdump._mysql_dump_cmd(
            "mariadb-dump", "root", False, ["snipeit"], True,
        )
        self.assertNotIn("--all-databases", cmd)
        self.assertEqual(cmd[-2:], ["--databases", "snipeit"])

    def test_persisted_dynamic_scope_is_complete_if_v103_ignores_marker(self):
        creds = detect.extract_credentials({
            "MYSQL_ROOT_PASSWORD": "secret", "MYSQL_DATABASE": "snipeit",
        }, "mysql")

        # v1.0.3 does not understand database_scope and feeds only the legacy
        # fields into this builder. It must over-include system schemas rather
        # than silently omit additional user databases created after setup.
        cmd = dbdump._mysql_dump_cmd(
            "mariadb-dump", creds["user"], creds["all_databases"],
            creds["databases"], True,
        )

        self.assertEqual(creds["database_scope"], "non-system")
        self.assertIn("--all-databases", cmd)
        self.assertNotIn("--databases", cmd)

    def test_show_databases_parser_keeps_all_user_databases(self):
        names = dbdump._parse_mysql_database_names(
            _mysql_hex_names(
                "information_schema", "mysql", "ndbinfo", "performance_schema",
                "snipeit", "audit", "sys",
            )
        )
        self.assertEqual(names, ["audit", "snipeit"])

    def test_show_databases_parser_rejects_empty_user_scope(self):
        with self.assertRaises(util.CommandError):
            dbdump._parse_mysql_database_names(
                _mysql_hex_names("mysql", "sys", "performance_schema", "ndbinfo")
            )

    def test_show_databases_parser_fails_closed_on_unsafe_names(self):
        for output in (
            "not-hex\n",
            _mysql_hex_names("bad\nname"),
            _mysql_hex_names("-option"),
            _mysql_hex_names("path/name"),
            _mysql_hex_names("path\\name"),
        ):
            with self.subTest(output=output):
                with self.assertRaises(util.CommandError):
                    dbdump._parse_mysql_database_names(output)

    def test_system_filter_is_exact_and_user_names_are_deduplicated(self):
        names = dbdump._parse_mysql_database_names(
            _mysql_hex_names("mysql", "MYSQL", "app", "app")
        )
        self.assertEqual(names, ["MYSQL", "app"])

    def test_show_databases_parser_rejects_more_than_manifest_limit(self):
        with self.assertRaises(util.CommandError):
            dbdump._parse_mysql_database_names(
                _mysql_hex_names(*("db%d" % i for i in range(65)))
            )

    def test_runtime_enumeration_uses_password_env_and_requires_seed(self):
        db = {"service": "db", "auth_user": "root", "databases": ["snipeit"]}
        with mock.patch.object(dbdump, "_probe_tool", return_value="mariadb"), \
                mock.patch.object(
                    dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3],
                ) as exec_args, mock.patch.object(
                    dbdump.util, "run",
                    return_value=mock.Mock(
                        stdout=_mysql_hex_names("mysql", "snipeit", "audit", "sys")
                    ),
                ):
            names = dbdump._mysql_non_system_databases(
                db, "secret", "c.yml", "/p", "proj",
            )
        self.assertEqual(names, ["audit", "snipeit"])
        self.assertEqual(exec_args.call_args.kwargs["env"], {"MYSQL_PWD": "secret"})
        query = exec_args.call_args.args[3]
        self.assertNotIn("--raw", query)
        self.assertIn(
            "SELECT HEX(SCHEMA_NAME) FROM information_schema.SCHEMATA", query[-1],
        )
        self.assertIn("ORDER BY SCHEMA_NAME", query[-1])

        with mock.patch.object(dbdump, "_probe_tool", return_value="mysql"), \
                mock.patch.object(dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3]), \
                mock.patch.object(
                    dbdump.util, "run",
                    return_value=mock.Mock(stdout=_mysql_hex_names("mysql", "other")),
                ):
            with self.assertRaises(util.CommandError):
                dbdump._mysql_non_system_databases(
                    db, "secret", "c.yml", "/p", "proj",
                )

    def test_dynamic_non_system_scope_is_exactly_bound_to_dump_plan(self):
        calls = []

        def fake_run_to_file(argv, out_path, kind, **kwargs):
            calls.append(argv)

        db = {
            "service": "db", "auth_user": "root", "all_databases": True,
            "databases": ["snipeit"], "database_scope": "non-system",
        }
        with mock.patch.object(
                    dbdump, "_mysql_non_system_databases",
                    return_value=["snipeit", "audit"],
                ), mock.patch.object(dbdump, "_probe_tool", return_value="mariadb-dump"), \
                mock.patch.object(dbdump, "_run_to_file", side_effect=fake_run_to_file), \
                mock.patch.object(dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3]), \
                mock.patch.object(dbdump.util, "info"):
            dbdump._dump_mysql(db, "pw", "c.yml", "/p", "proj", "/out/db.sql")

        self.assertEqual(len(calls), 1)
        self.assertNotIn("--all-databases", calls[0])
        index = calls[0].index("--databases")
        self.assertEqual(calls[0][index + 1:], ["snipeit", "audit"])
        self.assertEqual(db["databases"], ["snipeit", "audit"])
        self.assertNotIn("database_scope", db)

    def test_dynamic_scope_is_enumerated_once_across_events_retry(self):
        calls = []

        def fake_run_to_file(argv, out_path, kind, **kwargs):
            calls.append(argv)
            if "--events" in argv:
                raise util.CommandError(argv, 2, "event scheduler is disabled (1577)")

        db = {
            "service": "db", "auth_user": "root", "all_databases": True,
            "databases": [], "database_scope": "non-system",
        }
        with mock.patch.object(
                    dbdump, "_mysql_non_system_databases",
                    return_value=["app", "audit"],
                ) as enumerate_dbs, mock.patch.object(
                    dbdump, "_probe_tool", return_value="mariadb-dump",
                ), mock.patch.object(
                    dbdump, "_run_to_file", side_effect=fake_run_to_file,
                ), mock.patch.object(
                    dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3],
                ), mock.patch.object(dbdump.util, "warn"), \
                mock.patch.object(dbdump.util, "info"):
            dbdump._dump_mysql(db, None, "c.yml", "/p", "proj", "/out/db.sql")
        enumerate_dbs.assert_called_once()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call[call.index("--databases") + 1:], ["app", "audit"])

    def test_unknown_dynamic_scope_is_rejected_before_dump(self):
        db = {
            "service": "db", "auth_user": "root", "all_databases": False,
            "databases": ["app"], "database_scope": "mystery",
        }
        with mock.patch.object(dbdump, "_run_to_file") as run_to_file:
            with self.assertRaises(util.CommandError):
                dbdump._dump_mysql(db, None, "c.yml", "/p", "proj", "/out/db.sql")
        run_to_file.assert_not_called()

    def test_retries_without_events_on_scheduler_error(self):
        calls = []

        def fake_run_to_file(argv, out_path, kind, **kwargs):
            calls.append(argv)
            if "--events" in argv:
                raise util.CommandError(
                    argv, 2,
                    "mariadb-dump: Couldn't execute 'show events': Cannot proceed, "
                    "because event scheduler is disabled (1577)")

        db = {"service": "db", "auth_user": "root", "all_databases": True}
        with mock.patch.object(dbdump, "_probe_tool", return_value="mariadb-dump"), \
                mock.patch.object(dbdump, "_run_to_file", side_effect=fake_run_to_file), \
                mock.patch.object(dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3]), \
                mock.patch.object(dbdump.util, "warn"):
            dbdump._dump_mysql(db, None, "c.yml", "/p", "proj", "/out/db.sql")

        self.assertEqual(len(calls), 2)
        self.assertIn("--events", calls[0])
        self.assertNotIn("--events", calls[1])

    def test_other_errors_are_not_retried(self):
        def fake_run_to_file(argv, out_path, kind, **kwargs):
            raise util.CommandError(argv, 1, "Access denied for user 'root'")

        db = {"service": "db", "auth_user": "root", "all_databases": True}
        with mock.patch.object(dbdump, "_probe_tool", return_value="mariadb-dump"), \
                mock.patch.object(dbdump, "_run_to_file", side_effect=fake_run_to_file), \
                mock.patch.object(dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3]):
            with self.assertRaises(util.CommandError):
                dbdump._dump_mysql(db, None, "c.yml", "/p", "proj", "/out/db.sql")


class CommandErrorStderrTest(unittest.TestCase):
    """stderr captured from a binary (text=False) run arrives as bytes; CommandError must
    normalize it to str so the error path (util.error(exc.stderr.strip())) does not crash."""

    def test_bytes_stderr_is_decoded(self):
        exc = util.CommandError(["mariadb-dump"], 2, b"proc corrupted (1728)")
        self.assertEqual(exc.stderr, "proc corrupted (1728)")
        # must be concatenable like in util.error("[ERROR] " + msg)
        self.assertEqual("[ERROR] " + exc.stderr.strip(), "[ERROR] proc corrupted (1728)")

    def test_str_stderr_is_preserved(self):
        exc = util.CommandError(["x"], 1, "plain text")
        self.assertEqual(exc.stderr, "plain text")


if __name__ == "__main__":
    unittest.main()
