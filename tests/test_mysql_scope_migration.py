from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import run as run_cmd


def _compose(database="snipeit"):
    environment = {"MYSQL_ROOT_PASSWORD": "rootpw"}
    if database is not None:
        environment["MYSQL_DATABASE"] = database
    return {
        "services": {
            "snipeit-mysql": {
                "image": "mariadb:10.7",
                "environment": environment,
            },
        },
    }


def _legacy_cfg(**updates):
    cfg = {
        "name": "snipeit",
        "stack_path": "/opt/snipeit",
        "compose_file": "/opt/snipeit/docker-compose.yml",
        "project_name": "snipeit",
        "repo": "/mnt/backups/snipeit",
        "key_file": "/etc/docker-backup/keys/snipeit.key",
        "db_autodetect": True,
        "db_services": [{
            "service": "snipeit-mysql",
            "engine": "mysql",
            "auth_user": "root",
            "all_databases": True,
            "databases": [],
            "password_source": "env:MYSQL_ROOT_PASSWORD",
        }],
    }
    cfg.update(updates)
    return cfg


class MysqlScopeGuardTest(unittest.TestCase):
    def test_legacy_all_database_scope_is_rejected_when_app_db_is_declared(self):
        with self.assertRaises(util.CommandError) as caught:
            run_cmd._verify_mysql_db_scope(_legacy_cfg(), _compose())
        self.assertIn("docker-backup set snipeit --refresh-db-detection",
                      caught.exception.stderr)

    def test_legacy_scope_is_rejected_after_dump_user_override(self):
        cfg = _legacy_cfg()
        cfg["db_services"][0]["auth_user"] = "backup-admin"
        with self.assertRaises(util.CommandError):
            run_cmd._verify_mysql_db_scope(cfg, _compose())

    def test_current_scope_version_allows_explicit_cluster_wide_dump(self):
        cfg = _legacy_cfg(db_scope_version=config.DB_SCOPE_VERSION)
        run_cmd._verify_mysql_db_scope(cfg, _compose())

    def test_legacy_all_database_scope_without_seed_is_also_rejected(self):
        with self.assertRaises(util.CommandError):
            run_cmd._verify_mysql_db_scope(_legacy_cfg(), _compose(database=None))

    def test_legacy_empty_scoped_database_list_is_rejected(self):
        cfg = _legacy_cfg()
        cfg["db_services"][0].update(all_databases=False, databases=[])
        with self.assertRaises(util.CommandError):
            run_cmd._verify_mysql_db_scope(cfg, _compose(database=None))

    def test_contradictory_all_database_flag_is_still_rejected(self):
        cfg = _legacy_cfg()
        cfg["db_services"][0]["databases"] = ["snipeit"]
        with self.assertRaises(util.CommandError):
            run_cmd._verify_mysql_db_scope(cfg, _compose())

    def test_refreshed_non_system_scope_without_seed_is_allowed(self):
        cfg = _legacy_cfg(db_scope_version=config.DB_SCOPE_VERSION)
        cfg["db_services"][0].update(
            all_databases=True, databases=[], database_scope="non-system",
        )
        run_cmd._verify_mysql_db_scope(cfg, _compose(database=None))

    def test_static_legacy_entry_requires_refresh_for_additional_databases(self):
        cfg = _legacy_cfg()
        cfg["db_services"][0].update(
            all_databases=False, databases=["snipeit"],
        )
        with self.assertRaises(util.CommandError):
            run_cmd._verify_mysql_db_scope(cfg, _compose())

    def test_backported_dynamic_marker_is_safe_without_top_level_stamp(self):
        cfg = _legacy_cfg()
        cfg["db_services"][0]["database_scope"] = "non-system"
        run_cmd._verify_mysql_db_scope(cfg, _compose())

    def test_disabled_autodetection_is_not_reinterpreted(self):
        run_cmd._verify_mysql_db_scope(
            _legacy_cfg(db_autodetect=False), _compose(),
        )

    def test_guard_fails_before_pre_hook_and_staging(self):
        cfg = _legacy_cfg(
            hooks={"pre_backup": [{"cmd": "must-not-run"}],
                   "post_backup": [], "restore": []},
        )
        with mock.patch.object(run_cmd.hooks, "ensure_allowed"), \
                mock.patch.object(run_cmd.runtime, "load_backend_env"), \
                mock.patch.object(run_cmd.util, "assert_mounted"), \
                mock.patch.object(run_cmd.compose, "config_json", return_value=_compose()), \
                mock.patch.object(run_cmd.hooks, "run_hooks") as run_hooks, \
                mock.patch.object(run_cmd, "_prepare_staging") as prepare_staging:
            with self.assertRaises(util.CommandError):
                run_cmd._do_run(cfg)
        run_hooks.assert_not_called()
        prepare_staging.assert_not_called()


if __name__ == "__main__":
    unittest.main()
