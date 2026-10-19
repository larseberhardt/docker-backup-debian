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

    def test_no_custom_restore_warns_before_and_after(self):
        with mock.patch.object(restore_cmd.util, "warn") as warn:
            rc = restore_cmd._run_restore(_cfg(), "gitlab", "/opt/gitlab-test", "latest",
                                          True, no_custom_restore=True)
        self.assertEqual(rc, 0)
        messages = [c.args[0] for c in warn.call_args_list]
        skipped = [m for m in messages if "skips the application restore step" in m]
        self.assertEqual(len(skipped), 2)
        self.assertIn("WITHOUT the application restore step", skipped[-1])
        self.assertIs(messages[-1], skipped[-1])  # last thing the operator sees

    def test_bootstrap_manifest_warning_names_template(self):
        # --from-repo: the manifest carries no shell, only the requirement marker.
        cfg = _cfg()
        cfg["hooks"]["restore"] = []
        cfg["custom_restore_required"] = True
        cfg["template"] = {"name": "gitlab", "source": "builtin", "version": "1"}
        with mock.patch.object(restore_cmd.util, "warn") as warn:
            rc = restore_cmd._run_restore(cfg, "gitlab", "/opt/gitlab-drill", "latest",
                                          True, no_custom_restore=True)
        self.assertEqual(rc, 0)
        skipped = [c.args[0] for c in warn.call_args_list
                   if "skips the application restore step" in c.args[0]]
        self.assertEqual(len(skipped), 2)
        self.assertIn("template 'gitlab'", skipped[0])
        self.assertIn("--use-template-hooks", skipped[0])

    def test_no_skipped_warning_without_restore_step(self):
        cfg = _cfg()
        cfg["hooks"]["restore"] = []
        with mock.patch.object(restore_cmd.util, "warn") as warn:
            rc = restore_cmd._run_restore(cfg, "gitlab", "/opt/gitlab-test", "latest",
                                          True, no_custom_restore=True)
        self.assertEqual(rc, 0)
        self.assertFalse(any("skips the application restore step" in c.args[0]
                             for c in warn.call_args_list))

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

    def test_scoped_runtime_model_disables_all_restart_policies_without_mutating_source(self):
        source = {
            "services": {
                "gitlab": {
                    "ports": [{"target": 80, "published": "30080"}],
                    "restart": "always",
                    "healthcheck": {
                        "test": ["CMD", "/opt/gitlab/bin/gitlab-healthcheck"],
                        "interval": "60s",
                    },
                    "deploy": {
                        "resources": {"limits": {"memory": "12G"}},
                        "restart_policy": {
                            "condition": "any",
                            "delay": "5s",
                        },
                    },
                },
                "runner": {
                    "ports": [{"target": 9000, "published": "39000"}],
                    "restart": "unless-stopped",
                    "healthcheck": {"test": ["CMD", "runner-healthcheck"]},
                },
            }
        }

        runtime_model = restore_cmd._custom_restore_runtime_model(
            source, ["gitlab"],
        )

        self.assertNotIn("ports", runtime_model["services"]["gitlab"])
        self.assertNotIn("ports", runtime_model["services"]["runner"])
        self.assertEqual(runtime_model["services"]["gitlab"]["restart"], "no")
        self.assertEqual(runtime_model["services"]["runner"]["restart"], "no")
        self.assertEqual(
            runtime_model["services"]["gitlab"]["healthcheck"],
            {"disable": True},
        )
        self.assertEqual(
            runtime_model["services"]["runner"]["healthcheck"],
            {"disable": True},
        )
        self.assertNotIn(
            "restart_policy", runtime_model["services"]["gitlab"]["deploy"],
        )
        self.assertEqual(
            runtime_model["services"]["gitlab"]["deploy"]["resources"],
            {"limits": {"memory": "12G"}},
        )
        self.assertIn("ports", source["services"]["gitlab"])
        self.assertEqual(source["services"]["gitlab"]["restart"], "always")
        self.assertEqual(
            source["services"]["gitlab"]["healthcheck"],
            {
                "test": ["CMD", "/opt/gitlab/bin/gitlab-healthcheck"],
                "interval": "60s",
            },
        )
        self.assertEqual(
            source["services"]["gitlab"]["deploy"]["restart_policy"],
            {"condition": "any", "delay": "5s"},
        )

    def test_unscoped_runtime_model_is_not_rewritten(self):
        source = {
            "services": {
                "app": {
                    "ports": [{"target": 80, "published": "8080"}],
                    "restart": "always",
                    "healthcheck": {"test": ["CMD", "true"]},
                    "deploy": {"restart_policy": {"condition": "any"}},
                },
            },
        }

        runtime_model = restore_cmd._custom_restore_runtime_model(source, None)

        self.assertIs(runtime_model, source)
        self.assertEqual(runtime_model["services"]["app"]["restart"], "always")
        self.assertEqual(
            runtime_model["services"]["app"]["healthcheck"],
            {"test": ["CMD", "true"]},
        )
        self.assertIn("restart_policy", runtime_model["services"]["app"]["deploy"])

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
