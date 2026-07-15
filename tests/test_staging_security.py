"""Staging hardening: 0700 dirs, 0600 dumps, cleanup even on failure."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import dbdump, util
from docker_backup.commands import run as run_cmd


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class StagingPermsTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        # macOS exposes /var as a symlink to /private/var.  The production
        # staging walker deliberately refuses symlink ancestors, so exercise it
        # through the canonical path returned by realpath.
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def test_staging_dirs_are_root_only(self):
        staging = os.path.join(self.tmp, ".docker-backup")
        dumps = os.path.join(staging, "dumps")
        vols = os.path.join(staging, "volumes")
        refs = run_cmd._prepare_staging(staging, [dumps, vols])
        try:
            for d in (staging, dumps, vols):
                self.assertEqual(_mode(d), 0o700, d)
        finally:
            run_cmd._cleanup_staging(refs)

    def test_dump_file_is_owner_only(self):
        out = os.path.join(self.tmp, "dumps", "db.sql")
        # kind without completeness markers → only the permission behavior is tested
        dbdump._run_to_file(["sh", "-c", "echo data"], out, "raw")
        self.assertEqual(_mode(out), 0o600)

    def test_dump_file_truncates_previous_content(self):
        out = os.path.join(self.tmp, "dumps", "db.sql")
        os.makedirs(os.path.dirname(out))
        with open(out, "w") as f:
            f.write("X" * 100)
        dbdump._run_to_file(["sh", "-c", "echo data"], out, "raw")
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"data\n")


class CleanupOnFailureTest(unittest.TestCase):
    """Dumps must not stay on disk when the run fails (plaintext DB contents)."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.stack = os.path.join(self.tmp, "stack")
        os.makedirs(self.stack)
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(run_cmd, "manifest"),
            mock.patch.object(run_cmd, "hooks"),
            mock.patch.object(run_cmd.util, "assert_mounted"),
            mock.patch.object(
                run_cmd.compose, "running_writable_bind_mounts_overlapping",
                return_value=[],
            ),
            mock.patch.object(
                run_cmd.compose, "config_json",
                return_value={"name": "app", "services": {}, "volumes": {}},
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        util.set_dry_run(False)

    def _cfg(self):
        return {
            "name": "app", "stack_path": self.stack,
            "compose_file": os.path.join(self.stack, "docker-compose.yml"),
            "project_name": "app", "key_file": "/k.key", "repo": "/repo",
            "offsite": None, "mount_check": None, "db_services": [],
            "named_volumes": [], "exclude_paths": [], "exclude_patterns": [],
            "retention": {"daily": 7, "weekly": 4, "monthly": 6},
            "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        }

    def test_staging_removed_when_backup_fails(self):
        staging = os.path.join(self.stack, ".docker-backup")

        def fail_backup(*a, **k):
            # at this point the staging dirs exist (dumps would live here)
            self.assertTrue(os.path.isdir(os.path.join(staging, "dumps")))
            raise RuntimeError("boom")

        run_cmd.restic.backup.side_effect = fail_backup
        with self.assertRaises(RuntimeError):
            run_cmd._do_run(self._cfg())
        self.assertFalse(os.path.isdir(os.path.join(staging, "dumps")))
        self.assertFalse(os.path.isdir(os.path.join(staging, "volumes")))

    def test_staging_removed_on_success(self):
        staging = os.path.join(self.stack, ".docker-backup")
        run_cmd._do_run(self._cfg())
        self.assertFalse(os.path.isdir(os.path.join(staging, "dumps")))
        self.assertFalse(os.path.isdir(os.path.join(staging, "volumes")))


if __name__ == "__main__":
    unittest.main()
