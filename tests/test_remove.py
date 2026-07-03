from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, keys, status, util
from docker_backup.commands import remove


def _args(name="xibo", purge_keys=False, yes=True):
    return argparse.Namespace(name=name, purge_keys=purge_keys, yes=yes)


class RemoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sysd = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        os.environ["DOCKER_BACKUP_SYSTEMD_DIR"] = self.sysd
        util.set_dry_run(False)
        config.save({"schema_version": config.SCHEMA_VERSION, "name": "xibo",
                     "repo": "/mnt/backups/xibo"})
        keys.ensure_key("xibo")
        config.save_secret("xibo", "cms-db", "pw")
        self.backend = os.path.join(config.backends_dir(), "xibo.env")
        with open(self.backend, "w") as f:
            f.write("AWS_ACCESS_KEY_ID=x\n")
        status.write_status("xibo", result="success", started_at="a", finished_at="b",
                            duration_sec=1.0)
        self._patches = [
            mock.patch.object(remove.util, "require_root"),
            mock.patch.object(remove.systemd_units, "disable_timer"),
            mock.patch.object(remove.systemd_units, "daemon_reload"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        os.environ.pop("DOCKER_BACKUP_SYSTEMD_DIR", None)
        util.set_dry_run(False)

    def test_default_keeps_key_and_secrets(self):
        rc = remove.cmd_remove(_args())
        self.assertEqual(rc, 0)
        self.assertFalse(config.exists("xibo"))
        self.assertIsNone(status.read_status("xibo"))
        self.assertTrue(remove.systemd_units.disable_timer.called)
        # Key/secrets/backend stay:
        self.assertTrue(os.path.exists(keys.key_path("xibo")))
        self.assertEqual(config.read_secret("xibo", "cms-db"), "pw")
        self.assertTrue(os.path.exists(self.backend))

    def test_purge_keys_removes_secrets(self):
        rc = remove.cmd_remove(_args(purge_keys=True))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(keys.key_path("xibo")))
        self.assertIsNone(config.read_secret("xibo", "cms-db"))
        self.assertFalse(os.path.exists(self.backend))

    def test_confirm_gate_aborts(self):
        with mock.patch.object(remove.wizard, "confirm", return_value=False):
            rc = remove.cmd_remove(_args(yes=False))
        self.assertEqual(rc, 0)
        self.assertTrue(config.exists("xibo"))  # nothing deleted

    def test_unknown_name(self):
        self.assertEqual(remove.cmd_remove(_args(name="nope")), 1)


if __name__ == "__main__":
    unittest.main()
