from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import hooks, util
from docker_backup.commands import run as run_cmd


def _cfg(**extra):
    cfg = {
        "schema_version": 2,
        "name": "gitlab",
        "stack_path": "/opt/gitlab",
        "compose_file": "/opt/gitlab/docker-compose.yml",
        "project_name": "gitlab",
        "key_file": "/etc/docker-backup/keys/gitlab.key",
        "repo": "/mnt/backups/gitlab",
        "offsite": None,
        "mount_check": None,
        "db_services": [],
        "named_volumes": [],
        "exclude_paths": [],
        "exclude_patterns": [],
        "retention": {"daily": 7, "weekly": 0, "monthly": 0, "keep_within": "30d"},
        "hooks": {"pre_backup": [{"cmd": "echo pre"}], "post_backup": [{"cmd": "echo post"}],
                  "restore": []},
        "hooks_allowed": True,
    }
    hooks_block = cfg["hooks"]
    cfg["hooks_fingerprint"] = hooks.compute_fingerprint(hooks_block)
    cfg.update(extra)
    return cfg


class HookOrderingTest(unittest.TestCase):
    """hooks mocked -> we check ordering/finally semantics in the _do_run flow."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(run_cmd, "manifest"),
            mock.patch.object(run_cmd, "hooks"),
            mock.patch.object(
                run_cmd.compose, "config_json",
                return_value={"name": "gitlab", "services": {}, "volumes": {}},
            ),
        ]
        for p in self._patches:
            p.start()
        run_cmd.restic.last_snapshot.return_value = {"short_id": "abcd1234", "time": "t"}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_pre_then_backup_then_post(self):
        calls = []
        run_cmd.hooks.run_hooks.side_effect = lambda cfg, phase: calls.append("hook:%s" % phase)
        run_cmd.restic.backup.side_effect = lambda *a, **k: calls.append("backup")
        run_cmd._do_run(_cfg())
        self.assertEqual(calls, ["hook:pre_backup", "backup", "hook:post_backup"])
        self.assertTrue(run_cmd.hooks.ensure_allowed.called)

    def test_post_runs_even_when_backup_fails(self):
        run_cmd.restic.backup.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            run_cmd._do_run(_cfg())
        phases = [c.args[1] for c in run_cmd.hooks.run_hooks.call_args_list]
        self.assertIn("pre_backup", phases)
        self.assertIn("post_backup", phases)  # finally ran despite the backup error

    def test_pre_failure_skips_backup_but_runs_cleanup_post(self):
        def se(cfg, phase):
            if phase == "pre_backup":
                raise util.CommandError(["pre"], 1, "fail")
        run_cmd.hooks.run_hooks.side_effect = se
        with self.assertRaises(util.CommandError):
            run_cmd._do_run(_cfg())
        self.assertFalse(run_cmd.restic.backup.called)
        phases = [c.args[1] for c in run_cmd.hooks.run_hooks.call_args_list]
        self.assertIn("post_backup", phases)


class HookGateInFlowTest(unittest.TestCase):
    """Real hooks: unapproved hooks abort the run BEFORE any restic call."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(run_cmd, "manifest"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_unapproved_hooks_abort_before_backup(self):
        cfg = _cfg(hooks_allowed=False, hooks_fingerprint=None)
        with self.assertRaises(util.CommandError):
            run_cmd._do_run(cfg)
        self.assertFalse(run_cmd.restic.backup.called)

    def test_malformed_template_restore_scope_aborts_before_hook_and_backup(self):
        cfg = _cfg(
            template={"name": "gitlab", "version": "1", "source": "builtin"},
            restore_services=[],
        )
        with mock.patch.object(run_cmd.hooks, "run_hooks") as run_hooks, \
                self.assertRaises(util.CommandError):
            run_cmd._do_run(cfg)

        run_hooks.assert_not_called()
        run_cmd.restic.backup.assert_not_called()

    def test_template_restore_scope_requires_service_in_current_compose(self):
        cfg = _cfg(
            template={"name": "gitlab", "version": "1", "source": "builtin"},
            restore_services=["gitlab"],
        )
        with mock.patch.object(
                run_cmd.compose, "config_json",
                return_value={"services": {"runner": {}}, "volumes": {}},
        ), mock.patch.object(run_cmd.hooks, "run_hooks") as run_hooks:
            with self.assertRaises(util.CommandError) as raised:
                run_cmd._do_run(cfg)

        self.assertIn("not found", raised.exception.stderr)
        run_hooks.assert_not_called()
        run_cmd.restic.backup.assert_not_called()


class DbSkipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(run_cmd, "manifest"),
            mock.patch.object(run_cmd, "hooks"),
            mock.patch.object(run_cmd, "compose"),
        ]
        for p in self._patches:
            p.start()
        run_cmd.restic.last_snapshot.return_value = {"short_id": "x", "time": "t"}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_no_db_services_still_checks_current_compose_model(self):
        run_cmd.compose.config_json.return_value = {
            "name": "gitlab", "services": {}, "volumes": {},
        }
        run_cmd.compose.collect_volume_backup_plan.return_value = ([], [])
        run_cmd._do_run(_cfg(db_services=[]))
        self.assertTrue(run_cmd.compose.config_json.called)


if __name__ == "__main__":
    unittest.main()
