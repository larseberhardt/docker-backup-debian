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
            mock.patch.object(restore_cmd.compose, "up_services"),
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

    def test_scoped_restore_starts_only_listed_service_without_dependencies(self):
        cfg = _cfg()
        cfg["restore_services"] = ["gitlab"]

        self.assertTrue(restore_cmd._custom_restore(
            cfg, "/runtime-compose", "/stable/project", "gitlab",
            already_confirmed=True,
        ))

        restore_cmd.compose.up_services.assert_called_once_with(
            "/runtime-compose", "/stable/project", ["gitlab"], "gitlab",
            no_deps=True,
        )
        restore_cmd.compose.up_all.assert_not_called()
        restore_cmd.compose.down_all.assert_called_once_with(
            "/runtime-compose", "/stable/project", "gitlab",
        )

    def test_scoped_start_failure_still_tears_entire_project_down(self):
        cfg = _cfg()
        cfg["restore_services"] = ["gitlab"]
        restore_cmd.compose.up_services.side_effect = RuntimeError("partial scoped up")

        with self.assertRaisesRegex(RuntimeError, "partial scoped up"):
            restore_cmd._custom_restore(
                cfg, "/runtime-compose", "/stable/project", "gitlab",
                already_confirmed=True,
            )

        restore_cmd.compose.down_all.assert_called_once_with(
            "/runtime-compose", "/stable/project", "gitlab",
        )
        restore_cmd.hooks.run_hooks.assert_not_called()

    def test_scoped_hook_failure_still_tears_entire_project_down(self):
        cfg = _cfg()
        cfg["restore_services"] = ["gitlab"]
        restore_cmd.hooks.run_hooks.side_effect = RuntimeError("restore hook failed")

        with self.assertRaisesRegex(RuntimeError, "restore hook failed"):
            restore_cmd._custom_restore(
                cfg, "/runtime-compose", "/stable/project", "gitlab",
                already_confirmed=True,
            )

        restore_cmd.compose.down_all.assert_called_once_with(
            "/runtime-compose", "/stable/project", "gitlab",
        )

    def test_scoped_runtime_model_strips_all_published_ports_without_mutating_source(self):
        source = {
            "services": {
                "gitlab": {"ports": [{"target": 80, "published": "30080"}]},
                "runner": {"ports": [{"target": 9000, "published": "39000"}]},
            }
        }

        runtime_model = restore_cmd._custom_restore_runtime_model(
            source, ["gitlab"],
        )

        self.assertNotIn("ports", runtime_model["services"]["gitlab"])
        self.assertNotIn("ports", runtime_model["services"]["runner"])
        self.assertIn("ports", source["services"]["gitlab"])

    def test_scope_rejects_unknown_service_and_host_network(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            restore_cmd._authenticate_restore_services(
                ["gitlab"], {"services": {"runner": {}}},
            )
        with self.assertRaisesRegex(ValueError, "network_mode=host"):
            restore_cmd._authenticate_restore_services(
                ["gitlab"], {"services": {"gitlab": {"network_mode": "host"}}},
            )

    def test_malformed_scope_fails_before_restic_restore(self):
        cfg = _cfg()
        cfg["restore_services"] = []

        rc = restore_cmd._run_restore(
            cfg, "gitlab", "/opt/gitlab-test", "latest", True,
        )

        self.assertEqual(rc, 1)
        restore_cmd.restic.restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
