"""restic version parsing + offsite/check repo selection."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import restic, util
from docker_backup.commands import check as check_cmd


def _proc(stdout):
    return subprocess.CompletedProcess(["restic", "version"], 0, stdout, "")


class ResticVersionTest(unittest.TestCase):
    def test_parses_standard_output(self):
        with mock.patch.object(restic.util, "run",
                               return_value=_proc("restic 0.16.4 compiled with go1.21 on linux/amd64\n")):
            self.assertEqual(restic.restic_version(), (0, 16, 4))

    def test_parses_debian_package_output(self):
        with mock.patch.object(restic.util, "run",
                               return_value=_proc("restic 0.12.1 (v0.12.1-...) compiled ...\n")):
            self.assertEqual(restic.restic_version(), (0, 12, 1))

    def test_missing_restic_returns_none(self):
        with mock.patch.object(restic.util, "run",
                               side_effect=util.CommandError(["restic"], 127, "not found")):
            self.assertIsNone(restic.restic_version())

    def test_garbage_output_returns_none(self):
        with mock.patch.object(restic.util, "run", return_value=_proc("whatever\n")):
            self.assertIsNone(restic.restic_version())

    def test_version_gates_are_ordered(self):
        self.assertLess(restic.MIN_VERSION, restic.RECOMMENDED_VERSION)


class CheckReposSelectionTest(unittest.TestCase):
    def test_primary_only_without_offsite(self):
        cfg = {"repo": "/repo", "offsite": None}
        self.assertEqual(check_cmd._repos_of(cfg), [("primary", "/repo")])

    def test_offsite_included_when_configured(self):
        cfg = {"repo": "/repo", "offsite": "/off"}
        self.assertEqual(check_cmd._repos_of(cfg),
                         [("primary", "/repo"), ("offsite", "/off")])

    def test_check_repos_reports_offsite_problems(self):
        cfg = {"repo": "/repo", "offsite": "/off"}
        with mock.patch.object(check_cmd, "restic") as m:
            m.repo_initialized.return_value = True
            m.check.side_effect = lambda repo, key, read_data_subset=None: repo != "/off"
            problems = check_cmd._check_repos(cfg, "/k.key", None)
        self.assertEqual(problems, ["offsite: restic check reported errors"])
        # stale locks are cleared for BOTH repos before checking
        self.assertEqual([c.args[0] for c in m.unlock.call_args_list], ["/repo", "/off"])

    def test_check_repos_reports_unreachable(self):
        cfg = {"repo": "/repo", "offsite": None}
        with mock.patch.object(check_cmd, "restic") as m:
            m.repo_initialized.return_value = False
            problems = check_cmd._check_repos(cfg, "/k.key", None)
        self.assertEqual(problems, ["primary repo not reachable"])


if __name__ == "__main__":
    unittest.main()
