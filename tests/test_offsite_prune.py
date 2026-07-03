"""Offsite lifecycle in the run flow: unlock, copy → prune order, opt-out."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util
from docker_backup.commands import run as run_cmd


def _cfg(**extra):
    cfg = {
        "schema_version": 2,
        "name": "app",
        "stack_path": "/opt/app",
        "compose_file": "/opt/app/docker-compose.yml",
        "project_name": "app",
        "key_file": "/etc/docker-backup/keys/app.key",
        "repo": "/mnt/backups/app",
        "offsite": "/mnt/offsite/app",
        "mount_check": None,
        "db_services": [],
        "named_volumes": [],
        "exclude_paths": [],
        "exclude_patterns": [],
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        "hooks_allowed": False,
    }
    cfg.update(extra)
    return cfg


class OffsitePruneFlowTest(unittest.TestCase):
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
        run_cmd.restic.last_snapshot.return_value = None

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_offsite_is_pruned_after_copy_with_primary_retention(self):
        calls = []
        run_cmd.restic.copy.side_effect = lambda *a, **k: calls.append("copy")
        run_cmd.restic.forget_prune.side_effect = (
            lambda repo, key, ret, tags: calls.append(("forget", repo, ret))
        )
        cfg = _cfg()
        run_cmd._do_run(cfg)
        self.assertEqual(calls[0], ("forget", "/mnt/backups/app", cfg["retention"]))
        self.assertEqual(calls[1], "copy")
        self.assertEqual(calls[2], ("forget", "/mnt/offsite/app", cfg["retention"]))

    def test_offsite_retention_override_wins(self):
        offsite_ret = {"daily": 30, "weekly": 12, "monthly": 24}
        run_cmd._do_run(_cfg(offsite_retention=offsite_ret))
        offsite_call = [
            c for c in run_cmd.restic.forget_prune.call_args_list
            if c.args[0] == "/mnt/offsite/app"
        ]
        self.assertEqual(len(offsite_call), 1)
        self.assertEqual(offsite_call[0].args[2], offsite_ret)

    def test_offsite_prune_can_be_disabled(self):
        run_cmd._do_run(_cfg(offsite_prune=False))
        repos = [c.args[0] for c in run_cmd.restic.forget_prune.call_args_list]
        self.assertEqual(repos, ["/mnt/backups/app"])  # primary only

    def test_no_offsite_means_single_prune(self):
        run_cmd._do_run(_cfg(offsite=None))
        repos = [c.args[0] for c in run_cmd.restic.forget_prune.call_args_list]
        self.assertEqual(repos, ["/mnt/backups/app"])
        self.assertFalse(run_cmd.restic.copy.called)

    def test_stale_locks_cleared_before_backup_and_copy(self):
        run_cmd._do_run(_cfg())
        unlocked = [c.args[0] for c in run_cmd.restic.unlock.call_args_list]
        self.assertIn("/mnt/backups/app", unlocked)
        self.assertIn("/mnt/offsite/app", unlocked)


if __name__ == "__main__":
    unittest.main()
