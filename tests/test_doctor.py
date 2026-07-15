from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, keys, status, util
from docker_backup.commands import doctor


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        key = keys.ensure_key("xibo")
        config.save({"schema_version": config.SCHEMA_VERSION, "name": "xibo",
                     "repo": "/mnt/backups/xibo", "key_file": key})
        self._patches = [
            mock.patch.object(doctor.util, "require_root"),
            mock.patch.object(doctor.systemd_units, "timer_active", return_value="active"),
            mock.patch.object(doctor.systemd_units, "timer_next", return_value="Wed 03:00"),
            mock.patch.object(doctor.runtime, "load_backend_env"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def _args(self, name=None, all=False):
        return argparse.Namespace(name=name, all=all)

    def test_all_healthy_rc0(self):
        status.write_status("xibo", result="success", started_at="a", finished_at="b",
                            duration_sec=1.0, snapshot="abcd")
        with mock.patch.object(doctor.restic, "repo_initialized", return_value=True):
            self.assertEqual(doctor.cmd_doctor(self._args()), 0)

    def test_last_run_failure_rc1(self):
        status.write_status("xibo", result="failure", started_at="a", finished_at="b",
                            duration_sec=1.0, error="boom")
        with mock.patch.object(doctor.restic, "repo_initialized", return_value=True):
            self.assertEqual(doctor.cmd_doctor(self._args()), 1)

    def test_repo_unreachable_rc1(self):
        status.write_status("xibo", result="success", started_at="a", finished_at="b",
                            duration_sec=1.0)
        with mock.patch.object(doctor.restic, "repo_initialized", return_value=False):
            self.assertEqual(doctor.cmd_doctor(self._args()), 1)

    def test_repo_probe_error_is_not_reported_as_missing(self):
        status.write_status("xibo", result="success", started_at="a", finished_at="b",
                            duration_sec=1.0)
        error = util.CommandError(["restic"], 12, "wrong password")
        with mock.patch.object(doctor.restic, "repo_initialized", side_effect=error):
            row, severity = doctor._check_one("xibo")
        self.assertEqual(row[2], "error(rc=12)")
        self.assertEqual(severity, 2)

    def test_unknown_name_rc1(self):
        self.assertEqual(doctor.cmd_doctor(self._args(name="nope")), 1)


if __name__ == "__main__":
    unittest.main()
