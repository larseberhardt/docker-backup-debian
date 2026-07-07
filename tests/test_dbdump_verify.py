from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import dbdump, util


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

    def test_retries_without_events_on_scheduler_error(self):
        calls = []

        def fake_run_to_file(argv, out_path, kind):
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
        def fake_run_to_file(argv, out_path, kind):
            raise util.CommandError(argv, 1, "Access denied for user 'root'")

        db = {"service": "db", "auth_user": "root", "all_databases": True}
        with mock.patch.object(dbdump, "_probe_tool", return_value="mariadb-dump"), \
                mock.patch.object(dbdump, "_run_to_file", side_effect=fake_run_to_file), \
                mock.patch.object(dbdump.compose, "exec_args", side_effect=lambda *a, **k: a[3]):
            with self.assertRaises(util.CommandError):
                dbdump._dump_mysql(db, None, "c.yml", "/p", "proj", "/out/db.sql")


if __name__ == "__main__":
    unittest.main()
