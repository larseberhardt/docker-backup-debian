from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import create


def _args(**kw):
    base = dict(path=None, all=False, auto=False, target=None, offsite=None,
                schedule="daily 03:00", name=None, force=False, non_interactive=False)
    base.update(kw)
    return argparse.Namespace(**base)


_STACKS = [
    {"Name": "alpha", "ConfigFiles": "/srv/alpha/docker-compose.yml"},
    {"Name": "beta", "ConfigFiles": "/srv/beta/docker-compose.yml"},
]


class CreateValidationTest(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(create.util, "require_root")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_no_path_no_all(self):
        self.assertEqual(create.cmd_create(_args()), 2)

    def test_path_and_all(self):
        self.assertEqual(create.cmd_create(_args(path="/srv/x", all=True)), 2)

    def test_auto_without_all(self):
        self.assertEqual(create.cmd_create(_args(auto=True)), 2)

    def test_auto_non_interactive_without_target(self):
        rc = create.cmd_create(_args(all=True, auto=True, non_interactive=True))
        self.assertEqual(rc, 2)


class CreateAllTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(create.util, "require_root"),
            mock.patch.object(create.compose, "ls_json", return_value=list(_STACKS)),
            mock.patch("docker_backup.commands.create.os.path.exists", return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_auto_sets_up_all_without_confirm(self):
        with mock.patch.object(create, "create_one", return_value=0) as co, \
             mock.patch.object(create.wizard, "confirm") as confirm:
            rc = create.cmd_create(_args(all=True, auto=True, target="/mnt/b"))
        self.assertEqual(rc, 0)
        self.assertFalse(confirm.called)
        self.assertEqual(co.call_count, 2)
        for c in co.call_args_list:
            self.assertEqual(c.kwargs["interactive"], False)
            self.assertEqual(c.kwargs["target_base"], "/mnt/b")

    def test_all_wizard_confirm_yes_runs_full_wizard(self):
        with mock.patch.object(create, "create_one", return_value=0) as co, \
             mock.patch.object(create.wizard, "confirm", return_value=True):
            rc = create.cmd_create(_args(all=True))
        self.assertEqual(rc, 0)
        self.assertEqual(co.call_count, 2)
        for c in co.call_args_list:
            self.assertEqual(c.kwargs["interactive"], True)
            self.assertIsNone(c.kwargs["target_base"])

    def test_all_wizard_confirm_no_skips(self):
        with mock.patch.object(create, "create_one", return_value=0) as co, \
             mock.patch.object(create.wizard, "confirm", return_value=False):
            rc = create.cmd_create(_args(all=True))
        self.assertEqual(rc, 0)
        self.assertFalse(co.called)


_SUPABASE_CJ = {
    "name": "supabase-production",
    "services": {
        "db": {
            "image": "supabase/postgres:15.8.1.085",
            "environment": {"POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "postgres"},
        },
    },
}

_GENERIC_PG_CJ = {
    "services": {
        "db": {
            "image": "postgres:16",
            "environment": {"POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "app"},
        },
    },
}

_SNIPEIT_CJ = {
    "services": {
        "snipeit-mysql": {
            "image": "mariadb:10.7",
            "environment": {
                "MYSQL_ROOT_PASSWORD": "rootpw",
                "MYSQL_DATABASE": "snipeit",
                "MYSQL_USER": "snipeit",
                "MYSQL_PASSWORD": "apppw",
            },
        },
    },
}


class BuildDbServicesTest(unittest.TestCase):
    def test_snipeit_root_dump_excludes_mariadb_system_databases(self):
        out = create._build_db_services(_SNIPEIT_CJ, "snipeit", interactive=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["auth_user"], "root")
        self.assertEqual(out[0]["password_source"], "env:MYSQL_ROOT_PASSWORD")
        self.assertTrue(out[0]["all_databases"])
        self.assertEqual(out[0]["databases"], ["snipeit"])
        self.assertEqual(out[0]["database_scope"], "non-system")

    def test_supabase_defaults(self):
        out = create._build_db_services(_SUPABASE_CJ, "supa", interactive=False)
        self.assertEqual(len(out), 1)
        db = out[0]
        self.assertEqual(db["auth_user"], "supabase_admin")
        self.assertEqual(db["databases"], ["postgres", "_supabase"])
        self.assertTrue(db["dump_globals"])
        self.assertEqual(db["password_source"], "env:POSTGRES_PASSWORD")

    def test_dump_user_override(self):
        out = create._build_db_services(_SUPABASE_CJ, "supa", interactive=False,
                                        dump_user="myadmin")
        self.assertEqual(out[0]["auth_user"], "myadmin")
        self.assertTrue(out[0]["dump_globals"])  # Supabase default is kept

    def test_dump_globals_override_off(self):
        out = create._build_db_services(_SUPABASE_CJ, "supa", interactive=False,
                                        dump_globals=False)
        self.assertFalse(out[0]["dump_globals"])

    def test_generic_postgres_unchanged(self):
        out = create._build_db_services(_GENERIC_PG_CJ, "g", interactive=False)
        self.assertEqual(out[0]["auth_user"], "postgres")
        self.assertEqual(out[0]["databases"], ["app"])
        self.assertFalse(out[0]["dump_globals"])


class DbAutodetectSkipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = os.path.join(self.tmp, "etc")
        self.stack = os.path.join(self.tmp, "gitlab")
        os.makedirs(self.stack)
        util.set_dry_run(False)
        self.patches = [
            mock.patch.object(create.util, "require_root"),
            mock.patch.object(create.compose, "find_compose_file",
                              return_value=os.path.join(self.stack, "docker-compose.yml")),
            mock.patch.object(create.compose, "config_json", return_value={"name": "gitlab"}),
            mock.patch.object(create.compose, "collect_volume_backup_plan", return_value=([], [])),
            mock.patch.object(create.compose, "find_env_files", return_value=[]),
            mock.patch.object(create.systemd_units, "validate_oncalendar", return_value=True),
            mock.patch.object(create, "_install_timer"),
            mock.patch.object(create.keys, "ensure_key",
                              return_value=os.path.join(self.tmp, "k.key")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_skip_db_autodetect_produces_empty_db_services(self):
        with mock.patch.object(create, "_build_db_services") as bds:
            rc = create.create_one(
                self.stack, target_base="/mnt/backups", schedule_input="daily 04:00",
                offsite=None, name="gitlab", force=True, interactive=False,
                db_autodetect=False, exclude_patterns=["gitlab/logs"], keep_within="30d",
            )
        self.assertEqual(rc, 0)
        self.assertFalse(bds.called)  # autodetect NOT called
        cfg = config.load("gitlab")
        self.assertEqual(cfg["db_services"], [])
        self.assertFalse(cfg["db_autodetect"])
        self.assertEqual(cfg["db_scope_version"], config.DB_SCOPE_VERSION)
        self.assertEqual(cfg["exclude_patterns"], ["gitlab/logs"])
        self.assertEqual(cfg["retention"]["keep_within"], "30d")

    def test_autodetect_default_calls_build(self):
        with mock.patch.object(create, "_build_db_services", return_value=[]) as bds:
            create.create_one(
                self.stack, target_base="/mnt/backups", schedule_input="daily 03:00",
                offsite=None, name="gitlab2", force=True, interactive=False,
            )
        self.assertTrue(bds.called)


if __name__ == "__main__":
    unittest.main()
