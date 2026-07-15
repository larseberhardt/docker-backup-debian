from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import hooks, util
from docker_backup.commands import restore as restore_cmd


def _cfg(allowed=True):
    cfg = {
        "name": "gitlab",
        "stack_path": "/opt/gitlab",
        "compose_file": "/opt/gitlab/docker-compose.yml",
        "repo": "/mnt/backups/gitlab",
        "key_file": "/etc/docker-backup/keys/gitlab.key",
        "named_volumes": [],
        "db_services": [],
        "backend_env_file": None,
        "hooks": {"pre_backup": [], "post_backup": [],
                  "restore": [hooks.make_hook("docker exec gitlab true", phase="restore")]},
        "hooks_allowed": False,
        "hooks_fingerprint": None,
    }
    if allowed:
        hooks.approve(cfg)
    return cfg


class RestoreCustomTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(restore_cmd.util, "require_root"),
            mock.patch.object(restore_cmd, "restic"),
            mock.patch.object(restore_cmd, "_restore_named_volumes"),
            mock.patch.object(restore_cmd, "_import_databases"),
            mock.patch.object(restore_cmd.compose, "up_all"),
            mock.patch.object(restore_cmd.compose, "down_all"),
            mock.patch.object(restore_cmd.hooks, "run_hooks"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        util.set_dry_run(False)

    def test_custom_path_runs_and_skips_db_import(self):
        rc = restore_cmd._run_restore(_cfg(), "gitlab", "/opt/gitlab-test", "latest", True)
        self.assertEqual(rc, 0)
        self.assertFalse(restore_cmd._import_databases.called)  # built-in import skipped
        self.assertTrue(restore_cmd.compose.up_all.called)      # stack brought up before the hook
        self.assertTrue(restore_cmd.hooks.run_hooks.called)
        self.assertEqual(restore_cmd.hooks.run_hooks.call_args.args[1], "restore")
        self.assertTrue(restore_cmd.compose.down_all.called)  # transient bind paths removed

    def test_no_custom_restore_forces_builtin(self):
        rc = restore_cmd._run_restore(_cfg(), "gitlab", "/opt/gitlab-test", "latest", True,
                                      no_custom_restore=True)
        self.assertEqual(rc, 0)
        self.assertTrue(restore_cmd._import_databases.called)
        self.assertFalse(restore_cmd.compose.up_all.called)
        self.assertFalse(restore_cmd.compose.down_all.called)

    def test_no_restore_hook_uses_builtin(self):
        cfg = _cfg()
        cfg["hooks"]["restore"] = []  # no restore hook -> built-in path
        rc = restore_cmd._run_restore(cfg, "gitlab", "/opt/gitlab-test", "latest", True)
        self.assertEqual(rc, 0)
        self.assertTrue(restore_cmd._import_databases.called)

    def test_partial_stack_start_failure_still_tears_project_down(self):
        restore_cmd.compose.up_all.side_effect = RuntimeError("partial up failed")

        with self.assertRaisesRegex(RuntimeError, "partial up failed"):
            restore_cmd._custom_restore(
                _cfg(), "/runtime-compose", "/stable/project", "gitlab",
                already_confirmed=True,
            )

        restore_cmd.compose.down_all.assert_called_once_with(
            "/runtime-compose", "/stable/project", "gitlab",
        )
        restore_cmd.hooks.run_hooks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
