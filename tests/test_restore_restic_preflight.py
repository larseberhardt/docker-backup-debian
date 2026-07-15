from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util
from docker_backup.commands import restore as restore_cmd


class RestoreResticPreflightTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)

    def tearDown(self):
        util.set_dry_run(False)

    def test_old_restic_aborts_before_repository_or_restore_access(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(restore_cmd.runtime, "load_backend_env"), \
                mock.patch.object(restore_cmd.restic, "restic_version", return_value=(0, 16, 4)), \
                mock.patch.object(restore_cmd.restic, "repo_initialized") as initialized, \
                mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd._run_restore(
                {
                    "repo": "/repo",
                    "key_file": "/key",
                    "stack_path": "/opt/app",
                    "compose_file": "/opt/app/docker-compose.yml",
                },
                "app",
                os.path.join(tmp, "target"),
                "latest",
                False,
            )
        self.assertEqual(rc, 1)
        initialized.assert_not_called()
        restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
