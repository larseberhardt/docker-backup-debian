"""Backup runs must not trust stale raw-database restic excludes."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import run as run_cmd


def _compose(source="/opt/app/mysql"):
    return {
        "name": "app",
        "services": {
            "db": {
                "image": "mariadb:10.11",
                "volumes": [{
                    "type": "bind",
                    "source": source,
                    "target": "/var/lib/mysql",
                }],
            },
        },
        "volumes": {},
    }


def _db(raw_data_exclude="/opt/app/mysql"):
    return {
        "service": "db",
        "engine": "mysql",
        "raw_data_exclude": raw_data_exclude,
        "data_dir_target": "/var/lib/mysql",
    }


def _cfg():
    return {
        "name": "app",
        "db_scope_version": config.DB_SCOPE_VERSION,
        "stack_path": "/opt/app",
        "compose_file": "/opt/app/docker-compose.yml",
        "project_name": "app",
        "key_file": "/keys/app.key",
        "repo": "/backup/app",
        "offsite": None,
        "mount_check": None,
        "db_services": [_db()],
        "named_volumes": [],
        "exclude_paths": ["/opt/app/mysql"],
        "exclude_patterns": [],
        "retention": {"daily": 7},
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
    }


class VerifyDatabaseExcludesTest(unittest.TestCase):
    def test_exact_current_bind_matches_without_mutating_cached_db(self):
        dbs = [_db()]
        before = copy.deepcopy(dbs)

        run_cmd._verify_backup_db_excludes(
            ["/opt/app/mysql"], dbs, _compose(),
        )

        self.assertEqual(dbs, before)

    def test_moved_bind_is_rejected(self):
        with self.assertRaises(util.CommandError) as raised:
            run_cmd._verify_backup_db_excludes(
                ["/opt/app/mysql"], [_db()], _compose("/srv/app/mysql"),
            )
        self.assertIn("no longer exactly match", raised.exception.stderr)

    def test_aggregate_exclude_must_contain_current_raw_bind(self):
        with self.assertRaises(util.CommandError) as raised:
            run_cmd._verify_backup_db_excludes([], [_db()], _compose())
        self.assertIn("no longer exactly match", raised.exception.stderr)

    def test_per_service_annotation_must_match_even_if_aggregate_does(self):
        with self.assertRaises(util.CommandError) as raised:
            run_cmd._verify_backup_db_excludes(
                ["/opt/app/mysql"], [_db("/stale/mysql")], _compose(),
            )
        self.assertIn("no longer exactly match", raised.exception.stderr)

    def test_removed_bind_does_not_inherit_cached_annotation(self):
        current = _compose()
        current["services"]["db"]["volumes"] = [{
            "type": "volume", "source": "dbdata", "target": "/var/lib/mysql",
        }]
        current["volumes"] = {"dbdata": {}}

        with self.assertRaises(util.CommandError) as raised:
            run_cmd._verify_backup_db_excludes(
                ["/opt/app/mysql"], [_db()], current,
            )
        self.assertIn("no longer exactly match", raised.exception.stderr)


class RunDatabaseExcludeOrderingTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(True)

    def tearDown(self):
        util.set_dry_run(False)

    def test_drift_fails_before_staging_hooks_and_restic(self):
        cfg = _cfg()
        with mock.patch.object(run_cmd.hooks, "ensure_allowed"), \
                mock.patch.object(run_cmd.hooks, "run_hooks") as run_hooks, \
                mock.patch.object(run_cmd.runtime, "load_backend_env"), \
                mock.patch.object(run_cmd.util, "assert_mounted"), \
                mock.patch.object(
                    run_cmd.compose, "config_json", return_value=_compose("/srv/app/mysql"),
                ) as config_json, \
                mock.patch.object(run_cmd, "_prepare_staging") as prepare_staging, \
                mock.patch.object(run_cmd, "restic") as restic:
            with self.assertRaises(util.CommandError) as raised:
                run_cmd._do_run(cfg)

        self.assertIn("no longer exactly match", raised.exception.stderr)
        config_json.assert_called_once_with(
            cfg["compose_file"], cfg["stack_path"], cfg["project_name"],
        )
        prepare_staging.assert_not_called()
        run_hooks.assert_not_called()
        restic.ensure_init.assert_not_called()
        restic.backup.assert_not_called()

    def test_even_run_without_databases_parses_current_compose_first(self):
        cfg = _cfg()
        cfg["db_services"] = []
        cfg["exclude_paths"] = []
        events = []

        def load_compose(*_args):
            events.append("compose")
            return {"name": "app", "services": {}, "volumes": {}}

        def run_hook(_cfg, phase):
            events.append("hook:%s" % phase)
            if phase == "pre_backup":
                raise RuntimeError("stop after ordering assertion")

        with mock.patch.object(run_cmd.hooks, "ensure_allowed"), \
                mock.patch.object(run_cmd.hooks, "run_hooks", side_effect=run_hook), \
                mock.patch.object(run_cmd.runtime, "load_backend_env"), \
                mock.patch.object(run_cmd.util, "assert_mounted"), \
                mock.patch.object(run_cmd.compose, "config_json", side_effect=load_compose), \
                mock.patch.object(run_cmd, "_prepare_staging", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "ordering assertion"):
                run_cmd._do_run(cfg)

        self.assertEqual(events[:2], ["compose", "hook:pre_backup"])


if __name__ == "__main__":
    unittest.main()
