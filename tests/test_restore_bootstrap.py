from __future__ import annotations

import argparse
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util
from docker_backup.commands import restore as restore_cmd


def _args(**kw):
    base = dict(dest="/opt/xibo-test", from_name=None, from_repo=None, key_file=None,
                bootstrap_name=None, save_config=False, snapshot="latest", force=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _sample_manifest(password_source="env:POSTGRES_PASSWORD"):
    return {
        "manifest_schema_version": 1,
        "config_schema_version": 1,
        "name": "xibo",
        "stack_path": "/opt/xibo",
        "compose_file": "docker-compose.yml",
        "project_name": "xibo",
        "db_services": [{"service": "db", "engine": "postgres",
                         "password_source": password_source}],
        "named_volumes": [],
        "exclude_paths": [],
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
        "source_repo": "/mnt/backups/xibo",
    }


class RestoreBootstrapTest(unittest.TestCase):
    def setUp(self):
        # DRY_RUN: no real restic/docker calls, FS checks skipped.
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(restore_cmd.util, "require_root"),
            mock.patch.object(restore_cmd, "restic"),
            mock.patch.object(restore_cmd, "_restore_named_volumes"),
            mock.patch.object(restore_cmd, "_import_databases"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        util.set_dry_run(False)

    def test_from_repo_builds_cfg_and_restores(self):
        with mock.patch.object(restore_cmd.manifest, "read",
                               return_value=_sample_manifest()) as rd:
            rc = restore_cmd.cmd_restore(
                _args(from_repo="/mnt/backups/xibo", key_file="/root/xibo.key"))
        self.assertEqual(rc, 0)
        self.assertTrue(rd.called)
        # restic.restore(repo, key_file, snapshot, scratch, paths=[stack_path])
        call = restore_cmd.restic.restore.call_args
        self.assertEqual(call.args[0], "/mnt/backups/xibo")     # repo == --from-repo
        self.assertEqual(call.args[1], "/root/xibo.key")        # key_file == --key-file
        self.assertEqual(call.kwargs["paths"], ["/opt/xibo"])   # in-snapshot stack_path

    def test_missing_manifest_errors(self):
        with mock.patch.object(restore_cmd.manifest, "read", return_value=None):
            rc = restore_cmd.cmd_restore(
                _args(from_repo="/mnt/backups/xibo", key_file="/root/xibo.key"))
        self.assertEqual(rc, 1)
        self.assertFalse(restore_cmd.restic.restore.called)

    def test_missing_key_errors(self):
        with mock.patch.object(restore_cmd.manifest, "read",
                               return_value=_sample_manifest()):
            rc = restore_cmd.cmd_restore(_args(from_repo="/mnt/backups/xibo", key_file=None))
        self.assertEqual(rc, 1)
        self.assertFalse(restore_cmd.restic.restore.called)

    def test_stored_password_warns(self):
        with mock.patch.object(restore_cmd.manifest, "read",
                               return_value=_sample_manifest(password_source="stored")), \
             mock.patch.object(restore_cmd.util, "warn") as warn:
            rc = restore_cmd.cmd_restore(
                _args(from_repo="/mnt/backups/xibo", key_file="/root/xibo.key"))
        self.assertEqual(rc, 0)
        self.assertTrue(any("stored" in str(c.args[0]) for c in warn.call_args_list))

    def test_name_override_used_for_scratch(self):
        with mock.patch.object(restore_cmd.manifest, "read",
                               return_value=_sample_manifest()):
            rc = restore_cmd.cmd_restore(
                _args(from_repo="/mnt/backups/xibo", key_file="/root/xibo.key",
                      bootstrap_name="xibo-test"))
        self.assertEqual(rc, 0)


class RestoreLegacyRegressionTest(unittest.TestCase):
    """The classic --from path stays unchanged and reads NO manifest."""

    def setUp(self):
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(restore_cmd.util, "require_root"),
            mock.patch.object(restore_cmd, "restic"),
            mock.patch.object(restore_cmd, "_restore_named_volumes"),
            mock.patch.object(restore_cmd, "_import_databases"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        util.set_dry_run(False)

    def test_from_name_path_unchanged(self):
        cfg = {
            "name": "xibo", "stack_path": "/opt/xibo",
            "compose_file": "/opt/xibo/docker-compose.yml",
            "repo": "/mnt/backups/xibo", "key_file": "/etc/docker-backup/keys/xibo.key",
            "named_volumes": [], "db_services": [], "backend_env_file": None,
        }
        with mock.patch.object(restore_cmd.config, "exists", return_value=True), \
             mock.patch.object(restore_cmd.config, "load", return_value=cfg), \
             mock.patch.object(restore_cmd.manifest, "read") as rd:
            rc = restore_cmd.cmd_restore(_args(from_name="xibo"))
        self.assertEqual(rc, 0)
        self.assertFalse(rd.called)                       # manifest never touched
        self.assertEqual(restore_cmd.restic.restore.call_args.args[0], "/mnt/backups/xibo")

    def test_missing_config_errors(self):
        with mock.patch.object(restore_cmd.config, "exists", return_value=False):
            rc = restore_cmd.cmd_restore(_args(from_name="ghost"))
        self.assertEqual(rc, 1)
        self.assertFalse(restore_cmd.restic.restore.called)


if __name__ == "__main__":
    unittest.main()
