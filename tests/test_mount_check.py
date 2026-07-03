from __future__ import annotations

import os
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util


class _Stat:
    def __init__(self, dev):
        self.st_dev = dev


class AssertMountedTest(unittest.TestCase):
    def test_none_path_is_noop(self):
        # No mount_check configured -> no check, no error.
        util.assert_mounted(None)
        util.assert_mounted("")

    def test_subdir_of_mount_is_allowed(self):
        # Target is a subdirectory of a network share: it sits on a different
        # device than '/', so allowed -- even if it is not itself a mountpoint.
        def fake_stat(p):
            return _Stat(1) if p == os.sep else _Stat(42)

        with mock.patch.object(util.os, "stat", side_effect=fake_stat), \
                mock.patch.object(util.os.path, "exists", return_value=True):
            util.assert_mounted("/mnt/backup/test-140-157/keycloak")

    def test_same_device_as_root_raises(self):
        # Share not mounted -> target sits on the root partition -> abort.
        with mock.patch.object(util.os, "stat", return_value=_Stat(1)), \
                mock.patch.object(util.os.path, "exists", return_value=True):
            with self.assertRaises(util.CommandError):
                util.assert_mounted("/mnt/backup/test-140-157/keycloak")

    def test_missing_target_walks_up_to_existing_ancestor(self):
        # Target directory does not exist yet; the mount ancestor '/mnt/backup' does.
        existing = "/mnt/backup"

        def fake_exists(p):
            return p == existing

        def fake_stat(p):
            return _Stat(42) if p == existing else _Stat(1)

        with mock.patch.object(util.os.path, "exists", side_effect=fake_exists), \
                mock.patch.object(util.os, "stat", side_effect=fake_stat):
            util.assert_mounted("/mnt/backup/test-140-157/keycloak")


if __name__ == "__main__":
    unittest.main()
