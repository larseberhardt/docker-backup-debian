from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import __version__, selfupdate


class VersionCompareTest(unittest.TestCase):
    """Pure version comparison -- no I/O."""

    def test_parse_version(self):
        self.assertEqual(selfupdate.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(selfupdate.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(selfupdate.parse_version("1.2"), (1, 2))
        self.assertEqual(selfupdate.parse_version("1.2.3-rc1"), (1, 2, 3))
        self.assertEqual(selfupdate.parse_version("1.2.3+build7"), (1, 2, 3))
        self.assertEqual(selfupdate.parse_version(""), ())
        self.assertEqual(selfupdate.parse_version(None), ())
        self.assertEqual(selfupdate.parse_version("1.x"), ())
        self.assertEqual(selfupdate.parse_version("latest"), ())

    def test_version_lt(self):
        self.assertTrue(selfupdate.version_lt("1.0.0", "1.1.0"))
        self.assertTrue(selfupdate.version_lt("1.0", "1.0.1"))
        self.assertTrue(selfupdate.version_lt("1.9.0", "1.10.0"))
        self.assertFalse(selfupdate.version_lt("1.1.0", "1.0.0"))
        self.assertFalse(selfupdate.version_lt("1.0.0", "1.0.0"))
        # Unparseable never triggers an update:
        self.assertFalse(selfupdate.version_lt("kaputt", "1.0.0"))
        self.assertFalse(selfupdate.version_lt("1.0.0", "kaputt"))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _write_cache(self, **fields):
        data = {"schema": 1, "latest_version": None, "releases_behind": 0,
                "source_version": __version__, "error": None}
        data.update(fields)
        with open(selfupdate.cache_path(), "w") as f:
            json.dump(data, f)

    def test_paths_honor_etc_override(self):
        self.assertTrue(selfupdate.cache_path().startswith(self.tmp))
        self.assertTrue(selfupdate.update_conf_path().startswith(self.tmp))

    def test_missing_cache(self):
        self.assertIsNone(selfupdate.read_cache())
        self.assertIsNone(selfupdate.update_available())

    def test_corrupt_cache(self):
        with open(selfupdate.cache_path(), "w") as f:
            f.write("{ this is not json")
        self.assertIsNone(selfupdate.read_cache())
        self.assertIsNone(selfupdate.update_available())

    def test_newer_version_available(self):
        self._write_cache(latest_version="9.9.9", releases_behind=3)
        info = selfupdate.update_available()
        self.assertIsNotNone(info)
        current, latest, behind = info
        self.assertEqual(current, __version__)
        self.assertEqual(latest, "9.9.9")
        self.assertEqual(behind, 3)

    def test_same_version_no_update(self):
        self._write_cache(latest_version=__version__)
        self.assertIsNone(selfupdate.update_available())

    def test_older_remote_no_update(self):
        self._write_cache(latest_version="0.0.1")
        self.assertIsNone(selfupdate.update_available())

    def test_notice_line_contains_both_versions(self):
        line = selfupdate.notice_line("1.0.0", "1.1.0")
        self.assertIn("1.0.0", line)
        self.assertIn("1.1.0", line)


class NoticeGatingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        # Cache with a newer version available
        with open(selfupdate.cache_path(), "w") as f:
            json.dump({"schema": 1, "latest_version": "9.9.9",
                       "releases_behind": 1, "source_version": __version__,
                       "error": None}, f)

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        os.environ.pop("DOCKER_BACKUP_NO_UPDATE_NOTICE", None)

    def _run(self, command, isatty=True):
        with mock.patch.object(selfupdate.util, "warn") as warn, \
                mock.patch("docker_backup.selfupdate.sys.stderr") as err:
            err.isatty.return_value = isatty
            selfupdate.maybe_print_update_notice(command)
            return warn

    def test_happy_path_prints_once(self):
        warn = self._run("ls", isatty=True)
        warn.assert_called_once()

    def test_not_a_tty_is_silent(self):
        warn = self._run("ls", isatty=False)
        warn.assert_not_called()

    def test_internal_run_command_silent(self):
        warn = self._run("run", isatty=True)
        warn.assert_not_called()

    def test_update_command_silent(self):
        warn = self._run("update", isatty=True)
        warn.assert_not_called()

    def test_env_opt_out(self):
        os.environ["DOCKER_BACKUP_NO_UPDATE_NOTICE"] = "1"
        warn = self._run("ls", isatty=True)
        warn.assert_not_called()

    def test_never_raises_even_if_read_cache_explodes(self):
        with mock.patch.object(selfupdate, "read_cache", side_effect=RuntimeError("boom")), \
                mock.patch.object(selfupdate.util, "warn") as warn, \
                mock.patch("docker_backup.selfupdate.sys.stderr") as err:
            err.isatty.return_value = True
            # must not raise
            self.assertIsNone(selfupdate.maybe_print_update_notice("ls"))
            warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
