"""A Postgres --no-owner dump must not silently lose object ownership on restore."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, dbdump, util
from docker_backup.commands import create, run as run_cmd, setcfg


def _hex(role):
    return role.encode("utf-8").hex()


def _pg_db(**kw):
    db = {"service": "db", "engine": "postgres", "auth_user": "admin",
          "databases": ["canteen"], "dump_globals": False,
          "password_source": "env:POSTGRES_PASSWORD"}
    db.update(kw)
    return db


def _cfg(**kw):
    cfg = {"name": "canteen-app", "stack_path": "/opt/canteen-app",
           "compose_file": "/opt/canteen-app/docker-compose.yml",
           "project_name": "canteen-app"}
    cfg.update(kw)
    return cfg


class OwnerProbeCmdTest(unittest.TestCase):
    def test_probe_is_read_only_psql_against_the_database(self):
        argv = dbdump.build_pg_owner_probe_cmd("admin", "canteen")
        self.assertEqual(argv[0], "psql")
        self.assertIn("-X", argv)
        self.assertEqual(argv[argv.index("-U") + 1], "admin")
        self.assertEqual(argv[argv.index("-d") + 1], "canteen")
        sql = argv[argv.index("-c") + 1]
        self.assertIn("current_user", sql)
        self.assertIn("deptype = 'e'", sql)
        self.assertIn("nspname <> 'public'", sql)
        for keyword in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE"):
            self.assertNotIn(keyword, sql)


class ParseOwnerRowsTest(unittest.TestCase):
    def test_decodes_hex_role_names(self):
        out = "%s|8\n%s|1\n" % (_hex("canteen_app"), _hex('we|ird"role'))
        self.assertEqual(dbdump.parse_pg_owner_rows(out),
                         [("canteen_app", 8), ('we|ird"role', 1)])

    def test_empty_output_means_no_foreign_owners(self):
        self.assertEqual(dbdump.parse_pg_owner_rows("\n"), [])

    def test_malformed_rows_fail_loudly(self):
        for bad in ("canteen_app|8", "%s|x" % _hex("a"), "%s" % _hex("a"), "zz|1"):
            with self.subTest(bad=bad), self.assertRaises(util.CommandError):
                dbdump.parse_pg_owner_rows(bad)


class PostgresForeignOwnersTest(unittest.TestCase):
    def test_probes_every_configured_database_with_password_env(self):
        db = _pg_db(databases=["postgres", "_supabase"])
        outputs = ["%s|2\n" % _hex("authenticator"), ""]
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, outputs[len(calls) - 1], "")

        with mock.patch.object(dbdump.util, "run", side_effect=fake_run):
            out = dbdump.postgres_foreign_owners(
                db, "secret", "/s/docker-compose.yml", "/s", "proj")
        self.assertEqual(out, [("postgres", "authenticator", 2)])
        self.assertEqual(len(calls), 2)
        self.assertIn("PGPASSWORD=secret", calls[0])
        self.assertEqual(calls[1][calls[1].index("-d") + 1], "_supabase")


class VerifyOwnershipTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)

    def tearDown(self):
        util.set_dry_run(False)

    def _verify(self, cfg, db, foreign, started=False):
        with mock.patch.object(run_cmd.dbdump, "postgres_foreign_owners",
                               return_value=foreign) as probe:
            run_cmd._verify_postgres_ownership(cfg, db, "pw", started)
        return probe

    def test_foreign_owners_abort_with_the_fix(self):
        with self.assertRaises(util.CommandError) as ctx:
            self._verify(_cfg(), _pg_db(), [("canteen", "canteen_app", 8)])
        message = ctx.exception.stderr
        self.assertIn("canteen: canteen_app (8 objects)", message)
        self.assertIn("docker-backup set canteen-app --dump-globals", message)
        self.assertIn("docker-backup set canteen-app --no-owner-check", message)

    def test_single_role_database_passes(self):
        self._verify(_cfg(), _pg_db(), [])

    def test_ownership_preserving_dump_is_not_probed(self):
        probe = self._verify(_cfg(), _pg_db(dump_globals=True), [("canteen", "x", 1)])
        self.assertFalse(probe.called)

    def test_opt_out_is_not_probed(self):
        probe = self._verify(_cfg(pg_owner_check=False), _pg_db(), [("canteen", "x", 1)])
        self.assertFalse(probe.called)

    def test_mysql_is_not_probed(self):
        probe = self._verify(_cfg(), {"service": "db", "engine": "mysql"},
                             [("canteen", "x", 1)])
        self.assertFalse(probe.called)

    def test_dry_run_warns_instead_of_failing(self):
        util.set_dry_run(True)
        with mock.patch.object(run_cmd.util, "warn") as warn:
            self._verify(_cfg(), _pg_db(), [("canteen", "canteen_app", 1)])
        self.assertIn("1 object)", warn.call_args[0][0])

    def test_dry_run_does_not_probe_a_db_it_did_not_really_start(self):
        util.set_dry_run(True)
        probe = self._verify(_cfg(), _pg_db(), [("canteen", "x", 1)], started=True)
        self.assertFalse(probe.called)


class DumpOneOrderTest(unittest.TestCase):
    def test_no_dump_is_written_when_ownership_would_be_lost(self):
        util.set_dry_run(False)
        cfg = _cfg()
        with mock.patch.object(run_cmd.runtime, "resolve_password", return_value="pw"), \
             mock.patch.object(run_cmd.compose, "service_running", return_value=True), \
             mock.patch.object(run_cmd.dbdump, "wait_ready", return_value=True), \
             mock.patch.object(run_cmd.dbdump, "postgres_foreign_owners",
                               return_value=[("canteen", "canteen_app", 3)]), \
             mock.patch.object(run_cmd.dbdump, "dump") as dump:
            with self.assertRaises(util.CommandError):
                run_cmd._dump_one(cfg, _pg_db(), {}, "/stage/dumps")
        self.assertFalse(dump.called)


class CreatePreservesOwnershipTest(unittest.TestCase):
    def _run(self, db, *, running=True, foreign=None, probe_error=None):
        with mock.patch.object(create.compose, "service_running", return_value=running), \
             mock.patch.object(create.runtime, "resolve_password", return_value="pw"), \
             mock.patch.object(create.dbdump, "postgres_foreign_owners",
                               return_value=foreign or [],
                               side_effect=probe_error) as probe, \
             mock.patch.object(create.util, "warn"):
            create._preserve_pg_ownership_if_needed(
                [db], {}, "canteen-app", "/s/docker-compose.yml", "/s", "canteen-app")
        return probe

    def test_running_db_with_app_role_enables_dump_globals(self):
        db = _pg_db()
        self._run(db, foreign=[("canteen", "canteen_app", 8)])
        self.assertTrue(db["dump_globals"])

    def test_single_role_db_stays_portable(self):
        db = _pg_db()
        self._run(db, foreign=[])
        self.assertFalse(db["dump_globals"])

    def test_stopped_db_is_left_for_the_run_check(self):
        db = _pg_db()
        probe = self._run(db, running=False, foreign=[("canteen", "x", 1)])
        self.assertFalse(probe.called)
        self.assertFalse(db["dump_globals"])

    def test_probe_failure_does_not_break_create(self):
        db = _pg_db()
        self._run(db, probe_error=util.CommandError(["psql"], 2, "no connection"))
        self.assertFalse(db["dump_globals"])

    def test_explicit_dump_globals_flag_skips_the_probe(self):
        with mock.patch.object(create, "_preserve_pg_ownership_if_needed") as helper, \
             mock.patch.object(create.compose, "find_compose_file",
                               return_value="/s/docker-compose.yml"), \
             mock.patch.object(create.compose, "config_json", side_effect=RuntimeError("stop")), \
             mock.patch.object(create.os.path, "isdir", return_value=True), \
             mock.patch.object(create.config, "exists", return_value=False):
            with self.assertRaises(RuntimeError):
                create.create_one("/s", target_base="/b", schedule_input=None, offsite=None,
                                  name="s", force=False, interactive=False,
                                  dump_globals=False)
        self.assertFalse(helper.called)


class SetOwnerCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        config.save({"schema_version": config.SCHEMA_VERSION, "name": "canteen-app",
                     "repo": "/mnt/backups/canteen-app", "offsite": None,
                     "retention": dict(config.DEFAULT_RETENTION)})
        self.root = mock.patch.object(setcfg.util, "require_root")
        self.root.start()

    def tearDown(self):
        self.root.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _set(self, value):
        args = argparse.Namespace(name="canteen-app", schedule=None, retention=None,
                                  offsite=None, target=None, ack_dangerous=False,
                                  refresh_db_detection=False, pg_owner_check=value)
        return setcfg.cmd_set(args)

    def test_opt_out_and_back_in(self):
        self.assertEqual(self._set(False), 0)
        self.assertFalse(config.load("canteen-app")["pg_owner_check"])
        self.assertEqual(self._set(True), 0)
        self.assertTrue(config.load("canteen-app")["pg_owner_check"])


if __name__ == "__main__":
    unittest.main()
