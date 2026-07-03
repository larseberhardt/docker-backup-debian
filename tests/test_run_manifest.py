from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util
from docker_backup.commands import run as run_cmd


def _cfg(name="xibo"):
    return {
        "schema_version": 1,
        "name": name,
        "stack_path": "/opt/%s" % name,
        "compose_file": "/opt/%s/docker-compose.yml" % name,
        "project_name": name,
        "key_file": "/etc/docker-backup/keys/%s.key" % name,
        "repo": "/mnt/backups/%s" % name,
        "offsite": None,
        "mount_check": None,
        "db_services": [],
        "named_volumes": [],
        "exclude_paths": [],
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
    }


class RunWritesManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        # DRY_RUN skips staging FS operations; restic is mocked.
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(run_cmd, "manifest"),
        ]
        for p in self._patches:
            p.start()
        run_cmd.restic.last_snapshot.return_value = {"short_id": "abcd1234", "time": "t"}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_manifest_written_after_backup(self):
        cfg = _cfg()
        run_cmd._do_run(cfg)
        run_cmd.manifest.write.assert_called_once_with(cfg)
        # Order: first restic.backup, then manifest.write.
        self.assertTrue(run_cmd.restic.backup.called)


if __name__ == "__main__":
    unittest.main()
