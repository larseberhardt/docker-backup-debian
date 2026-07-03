from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import status, util


class StatusRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_write_read_roundtrip_and_mode(self):
        path = status.write_status(
            "xibo", result="success", started_at="2026-06-03T01:00:00+00:00",
            finished_at="2026-06-03T01:04:00+00:00", duration_sec=240.0, snapshot="abcd1234",
        )
        self.assertTrue(path and os.path.exists(path))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o640)
        st = status.read_status("xibo")
        self.assertEqual(st["result"], "success")
        self.assertEqual(st["snapshot"], "abcd1234")
        self.assertEqual(st["duration_sec"], 240.0)

    def test_read_missing_returns_none(self):
        self.assertIsNone(status.read_status("does-not-exist"))

    def test_read_corrupt_returns_none(self):
        os.makedirs(status.status_dir(), exist_ok=True)
        with open(status.status_path("broken"), "w") as f:
            f.write("{ not json")
        self.assertIsNone(status.read_status("broken"))

    def test_write_is_noop_under_dry_run(self):
        util.set_dry_run(True)
        path = status.write_status(
            "xibo", result="success", started_at="x", finished_at="y", duration_sec=1.0)
        self.assertIsNone(path)
        self.assertIsNone(status.read_status("xibo"))

    def test_delete_status_idempotent(self):
        status.write_status("xibo", result="failure", started_at="x", finished_at="y",
                            duration_sec=1.0, error="boom")
        self.assertIsNotNone(status.read_status("xibo"))
        status.delete_status("xibo")
        self.assertIsNone(status.read_status("xibo"))
        status.delete_status("xibo")  # again: no error


class CheckCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_roundtrip_and_failed_checks(self):
        status.write_check_cache("2026-06-03T04:00:00+00:00", {
            "xibo": {"status": "failed", "checked_at": "2026-06-03T04:00:00+00:00", "error": "x"},
            "wp": {"status": "ok", "checked_at": "2026-06-03T04:00:00+00:00", "error": None},
            "db": {"status": "error", "checked_at": "2026-06-03T04:00:00+00:00", "error": "unreach"},
        })
        self.assertEqual(status.failed_checks(), ["xibo"])

    def test_failed_checks_on_missing_cache(self):
        self.assertEqual(status.failed_checks(), [])

    def test_failed_checks_on_corrupt_cache(self):
        with open(status.check_cache_path(), "w") as f:
            f.write("nonsense")
        self.assertEqual(status.failed_checks(), [])


class CheckNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        os.environ.pop("DOCKER_BACKUP_NO_CHECK_NOTICE", None)
        util.set_dry_run(False)
        status.write_check_cache("t", {"xibo": {"status": "failed", "checked_at": "t", "error": "x"}})

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        os.environ.pop("DOCKER_BACKUP_NO_CHECK_NOTICE", None)
        util.set_dry_run(False)

    def _run(self, command):
        fake = mock.MagicMock()
        fake.isatty.return_value = True
        with mock.patch("docker_backup.status.sys.stderr", fake), \
             mock.patch.object(status.util, "warn") as warn:
            status.maybe_print_check_notice(command)
        return warn

    def test_prints_when_failed_and_tty(self):
        warn = self._run("ls")
        self.assertTrue(warn.called)

    def test_silent_for_run_and_check(self):
        self.assertFalse(self._run("run").called)
        self.assertFalse(self._run("check").called)

    def test_silent_without_tty(self):
        fake = mock.MagicMock()
        fake.isatty.return_value = False
        with mock.patch("docker_backup.status.sys.stderr", fake), \
             mock.patch.object(status.util, "warn") as warn:
            status.maybe_print_check_notice("ls")
        self.assertFalse(warn.called)

    def test_env_opt_out(self):
        os.environ["DOCKER_BACKUP_NO_CHECK_NOTICE"] = "1"
        self.assertFalse(self._run("ls").called)

    def test_never_raises(self):
        with mock.patch.object(status, "failed_checks", side_effect=RuntimeError("boom")):
            status.maybe_print_check_notice("ls")  # must not raise


if __name__ == "__main__":
    unittest.main()
