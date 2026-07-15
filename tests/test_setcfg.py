from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import setcfg


def _args(name="xibo", **kw):
    base = dict(name=name, schedule=None, retention=None, offsite=None,
                target=None, ack_dangerous=False, refresh_db_detection=False)
    base.update(kw)
    return argparse.Namespace(**base)


class ParseRetentionTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(setcfg._parse_retention("7/4/6"),
                         {"daily": 7, "weekly": 4, "monthly": 6})

    def test_rejects_wrong_count(self):
        with self.assertRaises(util.CommandError):
            setcfg._parse_retention("7/4")

    def test_rejects_non_int(self):
        with self.assertRaises(util.CommandError):
            setcfg._parse_retention("a/b/c")

    def test_rejects_negative(self):
        with self.assertRaises(util.CommandError):
            setcfg._parse_retention("7/-1/6")


class SetCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        config.save({
            "schema_version": config.SCHEMA_VERSION, "name": "xibo",
            "repo": "/mnt/backups/xibo", "offsite": None,
            "schedule": {"input": "daily 03:00", "oncalendar": "*-*-* 03:00:00",
                         "randomized_delay_sec": 300},
            "retention": dict(config.DEFAULT_RETENTION),
        })
        self._patches = [
            mock.patch.object(setcfg.util, "require_root"),
            mock.patch.object(setcfg.systemd_units, "validate_oncalendar", return_value=True),
            mock.patch.object(setcfg.systemd_units, "write_schedule_dropin"),
            mock.patch.object(setcfg.systemd_units, "daemon_reload"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_schedule_updates_oncalendar(self):
        rc = setcfg.cmd_set(_args(schedule="weekly Mon 04:00"))
        self.assertEqual(rc, 0)
        cfg = config.load("xibo")
        self.assertEqual(cfg["schedule"]["input"], "weekly Mon 04:00")
        self.assertEqual(cfg["schedule"]["oncalendar"], "Mon *-*-* 04:00:00")
        self.assertTrue(setcfg.systemd_units.write_schedule_dropin.called)

    def test_retention_updates(self):
        rc = setcfg.cmd_set(_args(retention="10/8/12"))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["retention"],
                         {"daily": 10, "weekly": 8, "monthly": 12, "keep_within": None})

    def test_offsite_set_and_clear(self):
        setcfg.cmd_set(_args(offsite="s3:bucket"))
        self.assertEqual(config.load("xibo")["offsite"], "s3:bucket/xibo")
        setcfg.cmd_set(_args(offsite=""))
        self.assertIsNone(config.load("xibo")["offsite"])

    def test_target_without_ack_refused(self):
        rc = setcfg.cmd_set(_args(target="/mnt/other"))
        self.assertEqual(rc, 2)
        self.assertEqual(config.load("xibo")["repo"], "/mnt/backups/xibo")  # unchanged

    def test_target_with_ack(self):
        rc = setcfg.cmd_set(_args(target="/mnt/other", ack_dangerous=True))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["repo"], "/mnt/other/xibo")

    def test_nothing_to_change(self):
        self.assertEqual(setcfg.cmd_set(_args()), 0)

    def test_add_exclude_patterns(self):
        rc = setcfg.cmd_set(_args(exclude=["gitlab/logs", "gitlab/data/postgresql"]))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["exclude_patterns"],
                         ["gitlab/logs", "gitlab/data/postgresql"])

    def test_exclude_dedup(self):
        setcfg.cmd_set(_args(exclude=["gitlab/logs"]))
        setcfg.cmd_set(_args(exclude=["gitlab/logs", "tmp"]))
        self.assertEqual(config.load("xibo")["exclude_patterns"], ["gitlab/logs", "tmp"])

    def test_exclude_clear(self):
        setcfg.cmd_set(_args(exclude=["gitlab/logs"]))
        setcfg.cmd_set(_args(exclude_clear=True))
        self.assertEqual(config.load("xibo")["exclude_patterns"], [])

    def test_invalid_exclude_rejected(self):
        with self.assertRaises(util.CommandError):
            setcfg.cmd_set(_args(exclude=["../etc"]))

    def test_keep_within_set_and_clear(self):
        setcfg.cmd_set(_args(keep_within="30d"))
        self.assertEqual(config.load("xibo")["retention"]["keep_within"], "30d")
        setcfg.cmd_set(_args(no_keep_within=True))
        self.assertIsNone(config.load("xibo")["retention"]["keep_within"])

    def test_retention_preserves_keep_within(self):
        setcfg.cmd_set(_args(keep_within="14d"))
        setcfg.cmd_set(_args(retention="5/3/2"))
        ret = config.load("xibo")["retention"]
        self.assertEqual(ret["daily"], 5)
        self.assertEqual(ret["keep_within"], "14d")

    def test_offsite_retention_set(self):
        rc = setcfg.cmd_set(_args(offsite_retention="30/12/24"))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["offsite_retention"],
                         {"daily": 30, "weekly": 12, "monthly": 24})

    def test_offsite_retention_invalid_rejected(self):
        with self.assertRaises(util.CommandError):
            setcfg.cmd_set(_args(offsite_retention="30/12"))

    def test_offsite_prune_toggle(self):
        setcfg.cmd_set(_args(offsite_prune=False))
        self.assertFalse(config.load("xibo")["offsite_prune"])
        setcfg.cmd_set(_args(offsite_prune=True))
        self.assertTrue(config.load("xibo")["offsite_prune"])

    def test_refresh_db_detection_replaces_only_db_and_volume_plan(self):
        cfg = config.load("xibo")
        cfg.update({
            "stack_path": "/opt/snipeit",
            "compose_file": "/opt/snipeit/docker-compose.yml",
            "project_name": "snipeit",
            "db_autodetect": True,
            "db_services": [{
                "service": "snipeit-mysql", "engine": "mysql",
                "auth_user": "root", "all_databases": True, "databases": [],
                "password_source": "env:MYSQL_ROOT_PASSWORD",
            }],
            "exclude_paths": ["/opt/snipeit/db-old"],
            "named_volumes": [{"key": "keep-old"}],
            "hooks": {"pre_backup": [], "post_backup": [], "restore": [{"cmd": "restore"}]},
            "hooks_allowed": True,
            "hooks_fingerprint": "approved",
            "key_file": "/etc/docker-backup/keys/xibo.key",
            "offsite": "s3:bucket/xibo",
            "exclude_patterns": ["cache/**"],
            "extra_backup_paths": ["/srv/xibo-data"],
            "template": {"name": "snipeit", "version": "1", "source": "builtin"},
        })
        config.save(cfg)
        unchanged = {
            key: cfg[key] for key in (
                "repo", "key_file", "schedule", "retention", "offsite", "hooks",
                "hooks_allowed", "hooks_fingerprint", "exclude_patterns",
                "extra_backup_paths", "template",
            )
        }
        compose_json = {"services": {"snipeit-mysql": {"image": "mariadb:10.7"}}}
        detected = [{
            "service": "snipeit-mysql", "engine": "mysql", "auth_user": "root",
            "all_databases": True, "databases": ["snipeit"],
            "database_scope": "non-system",
            "password_source": "env:MYSQL_ROOT_PASSWORD", "raw_data_exclude": None,
        }]
        with mock.patch.object(setcfg.compose, "config_json", return_value=compose_json) as config_json, \
                mock.patch.object(setcfg.create_cmd, "_build_db_services", return_value=detected), \
                mock.patch.object(
                    setcfg.compose, "collect_volume_backup_plan",
                    return_value=(["/opt/snipeit/db"], [{"key": "uploads"}]),
                ):
            rc = setcfg.cmd_set(_args(refresh_db_detection=True))

        self.assertEqual(rc, 0)
        config_json.assert_called_once_with(
            "/opt/snipeit/docker-compose.yml", "/opt/snipeit", "snipeit"
        )
        refreshed = config.load("xibo")
        self.assertTrue(refreshed["db_services"][0]["all_databases"])
        self.assertEqual(refreshed["db_services"][0]["databases"], ["snipeit"])
        self.assertEqual(refreshed["db_services"][0]["database_scope"], "non-system")
        self.assertEqual(refreshed["exclude_paths"], ["/opt/snipeit/db"])
        self.assertEqual(refreshed["named_volumes"], [{"key": "uploads"}])
        self.assertEqual(refreshed["db_scope_version"], config.DB_SCOPE_VERSION)
        for key, value in unchanged.items():
            self.assertEqual(refreshed[key], value, key)

    def test_refresh_db_detection_does_not_erase_config_when_nothing_detected(self):
        cfg = config.load("xibo")
        cfg.update({
            "stack_path": "/opt/app",
            "compose_file": "/opt/app/docker-compose.yml",
            "db_services": [{"service": "db", "engine": "mysql"}],
        })
        config.save(cfg)
        with mock.patch.object(setcfg.compose, "config_json", return_value={}), \
                mock.patch.object(setcfg.create_cmd, "_build_db_services", return_value=[]):
            with self.assertRaises(util.CommandError):
                setcfg.cmd_set(_args(refresh_db_detection=True))
        self.assertEqual(config.load("xibo")["db_services"],
                         [{"service": "db", "engine": "mysql"}])

    def test_refresh_db_detection_dry_run_does_not_persist(self):
        cfg = config.load("xibo")
        cfg.update({
            "stack_path": "/opt/snipeit",
            "compose_file": "/opt/snipeit/docker-compose.yml",
            "db_services": [{
                "service": "db", "engine": "mysql", "auth_user": "root",
                "all_databases": True, "databases": [],
            }],
        })
        config.save(cfg)
        before = config.load("xibo")
        detected = [{
            "service": "db", "engine": "mysql", "auth_user": "root",
            "all_databases": True, "databases": ["snipeit"],
            "password_source": "env:MYSQL_ROOT_PASSWORD",
        }]
        util.set_dry_run(True)
        with mock.patch.object(setcfg.compose, "config_json", return_value={}), \
                mock.patch.object(setcfg.create_cmd, "_build_db_services", return_value=detected), \
                mock.patch.object(setcfg.compose, "collect_volume_backup_plan",
                                  return_value=([], [])):
            self.assertEqual(setcfg.cmd_set(_args(refresh_db_detection=True)), 0)
        self.assertEqual(config.load("xibo"), before)

    def test_refresh_keeps_stored_password_user_together(self):
        cfg = config.load("xibo")
        cfg.update({
            "stack_path": "/opt/app",
            "compose_file": "/opt/app/docker-compose.yml",
            "db_services": [{
                "service": "db", "engine": "mysql", "auth_user": "legacy-user",
                "password_source": "stored", "all_databases": False,
                "databases": ["app"],
            }],
        })
        config.save(cfg)
        detected = [{
            "service": "db", "engine": "mysql", "auth_user": "root",
            "password_source": "none", "all_databases": False,
            "databases": ["app"],
        }]
        with mock.patch.object(setcfg.compose, "config_json", return_value={}), \
                mock.patch.object(setcfg.create_cmd, "_build_db_services", return_value=detected), \
                mock.patch.object(setcfg.compose, "collect_volume_backup_plan",
                                  return_value=([], [])):
            self.assertEqual(setcfg.cmd_set(_args(refresh_db_detection=True)), 0)
        db = config.load("xibo")["db_services"][0]
        self.assertEqual(db["password_source"], "stored")
        self.assertEqual(db["auth_user"], "legacy-user")

    def test_refresh_preserves_dump_user_and_postgres_globals_overrides(self):
        refreshed = [{
            "service": "mysql-db", "engine": "mysql", "auth_user": "root",
            "password_source": "env:MYSQL_ROOT_PASSWORD",
            "all_databases": True, "databases": ["app"],
            "database_scope": "non-system",
        }, {
            "service": "pg-db", "engine": "postgres", "auth_user": "postgres",
            "password_source": "env:POSTGRES_PASSWORD",
            "all_databases": False, "databases": ["app"],
            "dump_globals": False,
        }]
        old_by_service = {
            "mysql-db": {
                "service": "mysql-db", "engine": "mysql",
                "auth_user": "backup_admin",
                "password_source": "env:MYSQL_ROOT_PASSWORD",
            },
            "pg-db": {
                "service": "pg-db", "engine": "postgres",
                "auth_user": "supabase_admin",
                "password_source": "env:POSTGRES_PASSWORD",
                "dump_globals": True,
            },
        }

        setcfg._preserve_db_operator_overrides(refreshed, old_by_service)

        self.assertEqual(refreshed[0]["auth_user"], "backup_admin")
        self.assertEqual(refreshed[0]["password_source"],
                         "env:MYSQL_ROOT_PASSWORD")
        self.assertEqual(refreshed[1]["auth_user"], "supabase_admin")
        self.assertTrue(refreshed[1]["dump_globals"])


if __name__ == "__main__":
    unittest.main()
