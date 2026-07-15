from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, manifest, util
from docker_backup.commands import restore as restore_cmd


_SNAPSHOT_ID = "a" * 64


def _v5_manifest():
    cfg = {
        "schema_version": config.SCHEMA_VERSION,
        "name": "app",
        "stack_path": "/opt/app",
        "compose_file": "/opt/app/docker-compose.yml",
        "project_name": "app",
        "db_services": [],
        "named_volumes": [],
        "exclude_paths": [],
        "extra_backup_paths": [],
        "exclude_patterns": [],
        "db_autodetect": True,
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        "template": None,
        "repo": "/mnt/backups/app",
    }
    return manifest.derive(cfg, _SNAPSHOT_ID, [])


def _args(**overrides):
    values = {
        "dest": "/opt/app-test",
        "from_name": None,
        "from_repo": "/mnt/backups/app",
        "key_file": "/root/app.key",
        "bootstrap_name": None,
        "save_config": False,
        "snapshot": "latest",
        "force": False,
        "restore_cmd": None,
        "no_custom_restore": False,
        "use_template_hooks": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ManifestSnapshotSafetyTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(True)

    def tearDown(self):
        util.set_dry_run(False)

    def test_plain_v5_bootstrap_uses_bound_snapshot_not_latest(self):
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read", return_value=_v5_manifest()), \
             mock.patch.object(restore_cmd.restic, "restore") as restore, \
             mock.patch.object(restore_cmd, "_restore_named_volumes"), \
             mock.patch.object(restore_cmd, "_import_databases"):
            rc = restore_cmd.cmd_restore(_args(no_custom_restore=True))

        self.assertEqual(rc, 0)
        self.assertEqual(restore.call_args.args[2], _SNAPSHOT_ID)

    def test_plain_v5_bootstrap_requires_an_explicit_restore_policy(self):
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read", return_value=_v5_manifest()), \
             mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd.cmd_restore(_args())

        self.assertEqual(rc, 1)
        restore.assert_not_called()

    def test_plain_v5_bootstrap_rejects_different_snapshot(self):
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read", return_value=_v5_manifest()), \
             mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd.cmd_restore(_args(snapshot="b" * 64))

        self.assertEqual(rc, 1)
        restore.assert_not_called()

    def test_database_name_cannot_escape_dump_staging(self):
        data = _v5_manifest()
        data["db_services"] = [{
            "service": "db",
            "engine": "postgres",
            "auth_user": "postgres",
            "databases": ["../../tmp/payload"],
            "password_source": "none",
        }]
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read", return_value=data), \
             mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd.cmd_restore(_args())

        self.assertEqual(rc, 1)
        restore.assert_not_called()

    def test_v103_manifest_accepts_rollback_safe_dynamic_scope_marker(self):
        data = _v5_manifest()
        data["manifest_schema_version"] = 3
        data["db_services"] = [{
            "service": "db", "engine": "mysql", "auth_user": "root",
            "all_databases": True, "databases": ["app"],
            "database_scope": "non-system",
            "password_source": "env:MYSQL_ROOT_PASSWORD",
        }]

        # Published v1.0.3 writes schema-v3 manifests and copies unknown DB
        # config fields. Current restore intentionally applies the strict field
        # allowlist only to v5, so a rollback-created manifest stays readable.
        restore_cmd._validate_bootstrap_manifest(data)


class ManifestDockerAuthorizationTest(unittest.TestCase):
    def test_detected_database_still_requires_default_no_confirmation(self):
        cfg = {
            "_manifest_schema_version": 5,
            "db_services": [{
                "service": "db", "engine": "postgres", "auth_user": "postgres",
                "databases": ["app"], "password_source": "env:POSTGRES_PASSWORD",
            }],
        }
        compose_json = {
            "services": {"db": {"image": "postgres:16", "environment": {}}},
        }
        with mock.patch.object(restore_cmd.wizard, "confirm", return_value=False) as confirm:
            self.assertFalse(
                restore_cmd._confirm_manifest_database_imports(cfg, compose_json)
            )
        self.assertFalse(confirm.call_args.kwargs["default"])

    def test_non_database_service_is_rejected_without_prompt(self):
        cfg = {
            "_manifest_schema_version": 5,
            "db_services": [{"service": "docker-api", "engine": "postgres"}],
        }
        compose_json = {
            "services": {"docker-api": {"image": "alpine", "environment": {}}},
        }
        with mock.patch.object(restore_cmd.wizard, "confirm") as confirm:
            self.assertFalse(
                restore_cmd._confirm_manifest_database_imports(cfg, compose_json)
            )
        confirm.assert_not_called()


class HostPathSafetyTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        util.set_dry_run(False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_external_target_root_symlink_is_never_followed(self):
        source = os.path.join(self.tmp, "source")
        scratch = os.path.join(self.tmp, "scratch")
        restored = scratch + source
        outside = os.path.join(self.tmp, "outside")
        target = os.path.join(self.tmp, "target-link")
        os.makedirs(restored)
        os.makedirs(outside)
        with open(os.path.join(restored, "pwn"), "w") as handle:
            handle.write("snapshot")
        os.symlink(outside, target)

        with self.assertRaises(util.CommandError):
            restore_cmd._restore_extra_paths(
                {"extra_backup_paths": [source]}, scratch, force=True,
                mappings=[(source, target)],
            )

        self.assertFalse(os.path.exists(os.path.join(outside, "pwn")))

    def test_external_target_symlinked_ancestor_is_rejected(self):
        outside = os.path.join(self.tmp, "outside")
        link = os.path.join(self.tmp, "link")
        os.makedirs(outside)
        os.symlink(outside, link)
        with self.assertRaises(ValueError):
            restore_cmd._assert_safe_host_target(
                os.path.join(link, "data"), "external", expected="directory"
            )

    def test_main_restore_target_symlink_is_rejected_before_restic(self):
        outside = os.path.join(self.tmp, "outside")
        target = os.path.join(self.tmp, "target")
        os.makedirs(outside)
        os.symlink(outside, target)
        cfg = {
            "repo": "/repo", "key_file": "/key", "stack_path": "/opt/app",
            "compose_file": "/opt/app/docker-compose.yml", "db_services": [],
            "named_volumes": [], "backend_env_file": None,
        }
        with mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd._run_restore(
                cfg, "app", target, "latest", force=True,
            )

        self.assertEqual(rc, 1)
        restore.assert_not_called()

    def test_existing_external_target_skipped_without_force_is_not_adopted(self):
        source = os.path.join(self.tmp, "source")
        scratch = os.path.join(self.tmp, "scratch")
        restored = scratch + source
        os.makedirs(restored)
        os.makedirs(source)

        result = restore_cmd._restore_extra_paths(
            {"extra_backup_paths": [source]}, scratch, force=False,
        )

        self.assertEqual(result, [])

    def test_double_slash_and_external_target_inside_stack_are_rejected(self):
        with self.assertRaises(ValueError):
            restore_cmd._canonical_absolute_path("//", "path")
        with self.assertRaises(util.CommandError):
            restore_cmd._validate_external_mapping_targets(
                [("/srv/source", "/opt/app/data")], "/opt/app"
            )


class TimerFailClosedTest(unittest.TestCase):
    def test_unknown_timer_state_is_not_accepted(self):
        with mock.patch.object(restore_cmd.systemd_units, "disable_timer"), \
             mock.patch.object(restore_cmd.systemd_units, "timer_active", return_value=None), \
             mock.patch.object(restore_cmd.systemd_units, "timer_enabled", return_value=None):
            with self.assertRaises(util.CommandError):
                restore_cmd._disable_and_verify_timer("gitlab")


if __name__ == "__main__":
    unittest.main()
