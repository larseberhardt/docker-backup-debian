from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import restic, util


class RepositoryProbeTest(unittest.TestCase):
    def test_initialized_repository_returns_true(self):
        with mock.patch.object(restic.util, "run") as run:
            self.assertTrue(restic.repo_initialized("/repo", "/key"))
        run.assert_called_once_with(
            ["restic", "-r", "/repo", "--password-file", "/key", "cat", "config"],
            capture=True,
            check=True,
        )

    def test_modern_missing_repository_exit_code_returns_false(self):
        error = util.CommandError(["restic"], 10, "repository does not exist")
        with mock.patch.object(restic.util, "run", side_effect=error):
            self.assertFalse(restic.repo_initialized("/repo", "/key"))

    def test_legacy_ambiguous_missing_config_error_is_propagated(self):
        error = util.CommandError(
            ["restic"],
            1,
            "Fatal: unable to open config file: Stat: stat /repo/config: "
            "no such file or directory\nIs there a repository at the following location?",
        )
        with mock.patch.object(restic.util, "run", side_effect=error):
            with self.assertRaises(util.CommandError) as caught:
                restic.repo_initialized("/repo", "/key")
        self.assertIs(caught.exception, error)

    def test_cache_error_is_not_treated_as_missing_repository(self):
        error = util.CommandError(
            ["restic"],
            1,
            "unable to open cache: unable to locate cache directory: "
            "neither $XDG_CACHE_HOME nor $HOME are defined",
        )
        with mock.patch.object(restic.util, "run", side_effect=error):
            with self.assertRaises(util.CommandError) as caught:
                restic.repo_initialized("/repo", "/key")
        self.assertIs(caught.exception, error)

    def test_wrong_password_is_not_treated_as_missing_repository(self):
        error = util.CommandError(["restic"], 12, "wrong password or no key found")
        with mock.patch.object(restic.util, "run", side_effect=error):
            with self.assertRaises(util.CommandError) as caught:
                restic.repo_initialized("/repo", "/key")
        self.assertIs(caught.exception, error)

    def test_every_non_missing_exit_code_is_propagated(self):
        for returncode in (1, 2, 11, 12, 130, 99):
            with self.subTest(returncode=returncode):
                error = util.CommandError(["restic"], returncode, "backend failed")
                with mock.patch.object(restic.util, "run", side_effect=error):
                    with self.assertRaises(util.CommandError) as caught:
                        restic.repo_initialized("/repo", "/key")
                self.assertIs(caught.exception, error)

    def test_ensure_init_only_initializes_a_missing_repository(self):
        missing = util.CommandError(["restic"], 10, "repository does not exist")
        with mock.patch.object(restic.util, "run", side_effect=[missing, mock.DEFAULT]) as run:
            restic.ensure_init("/repo", "/key")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1], mock.call(
            ["restic", "-r", "/repo", "--password-file", "/key", "init"],
            capture=True,
            mutating=True,
        ))

    def test_ensure_init_propagates_probe_error_without_running_init(self):
        cache_error = util.CommandError(["restic"], 1, "unable to open cache")
        with mock.patch.object(restic.util, "run", side_effect=cache_error) as run:
            with self.assertRaises(util.CommandError):
                restic.ensure_init("/repo", "/key")
        self.assertEqual(run.call_count, 1)

    def test_ensure_init_offsite_propagates_probe_error_without_running_init(self):
        backend_error = util.CommandError(["restic"], 11, "repository is locked")
        with mock.patch.object(restic.util, "run", side_effect=backend_error) as run:
            with self.assertRaises(util.CommandError):
                restic.ensure_init_offsite("/offsite", "/key", "/primary")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
