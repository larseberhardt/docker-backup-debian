from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, status, util
from docker_backup.commands import run as run_cmd


class _DummyLock:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _save(name):
    config.save({"schema_version": config.SCHEMA_VERSION, "name": name,
                 "repo": "/mnt/backups/%s" % name, "key_file": "/etc/x/%s.key" % name})


class RunStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        self._patches = [
            mock.patch.object(run_cmd.util, "require_root"),
            mock.patch.object(run_cmd.util, "FileLock", _DummyLock),
            mock.patch.object(run_cmd.notify, "notify_success"),
            mock.patch.object(run_cmd.restic, "last_snapshot",
                              return_value={"short_id": "abcd1234"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_success_writes_status_and_notifies(self):
        _save("xibo")
        with mock.patch.object(run_cmd, "_do_run", return_value="summary"):
            rc = run_cmd.cmd_run(argparse.Namespace(name="xibo", all=False))
        self.assertEqual(rc, 0)
        st = status.read_status("xibo")
        self.assertEqual(st["result"], "success")
        self.assertEqual(st["snapshot"], "abcd1234")
        self.assertTrue(run_cmd.notify.notify_success.called)

    def test_failure_writes_status_and_reraises(self):
        _save("xibo")
        with mock.patch.object(run_cmd, "_do_run", side_effect=RuntimeError("kaputt")):
            with self.assertRaises(RuntimeError):
                run_cmd.cmd_run(argparse.Namespace(name="xibo", all=False))
        st = status.read_status("xibo")
        self.assertEqual(st["result"], "failure")
        self.assertIn("kaputt", st["error"])

    def test_run_all_mixed(self):
        _save("alpha")
        _save("beta")

        def fake_do_run(cfg):
            if cfg["name"] == "beta":
                raise RuntimeError("boom")
            return "ok"

        with mock.patch.object(run_cmd, "_do_run", side_effect=fake_do_run):
            rc = run_cmd.cmd_run(argparse.Namespace(name=None, all=True))
        self.assertEqual(rc, 1)
        self.assertEqual(status.read_status("alpha")["result"], "success")
        self.assertEqual(status.read_status("beta")["result"], "failure")

    def test_run_requires_name_or_all(self):
        rc = run_cmd.cmd_run(argparse.Namespace(name=None, all=False))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
