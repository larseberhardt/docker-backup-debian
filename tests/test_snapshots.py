from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import snapshots


class SnapshotRowTest(unittest.TestCase):
    def test_row_mapping(self):
        snap = {
            "short_id": "abc12345",
            "time": "2026-06-03T01:00:00.123456+00:00",
            "hostname": "srv1",
            "tags": ["docker-backup", "stack:xibo"],
            "paths": ["/opt/xibo"],
        }
        row = snapshots._row(snap)
        self.assertEqual(row[0], "abc12345")
        self.assertEqual(row[1], "2026-06-03T01:00:00")  # truncated to seconds
        self.assertEqual(row[2], "srv1")
        self.assertEqual(row[3], "docker-backup,stack:xibo")
        self.assertEqual(row[4], "/opt/xibo")

    def test_row_falls_back_to_id(self):
        row = snapshots._row({"id": "0123456789abcdef", "time": "2026-06-03T01:00:00+00:00"})
        self.assertEqual(row[0], "01234567")


class SnapshotsCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        config.save({"schema_version": config.SCHEMA_VERSION, "name": "xibo",
                     "repo": "/mnt/backups/xibo", "key_file": "/etc/x/xibo.key"})
        self._patches = [
            mock.patch.object(snapshots.util, "require_root"),
            mock.patch.object(snapshots.runtime, "load_backend_env"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_repo_unreachable_rc1(self):
        with mock.patch.object(snapshots.restic, "repo_initialized", return_value=False):
            rc = snapshots.cmd_snapshots(argparse.Namespace(name="xibo"))
        self.assertEqual(rc, 1)

    def test_unknown_name_rc1(self):
        self.assertEqual(snapshots.cmd_snapshots(argparse.Namespace(name="nope")), 1)


if __name__ == "__main__":
    unittest.main()
