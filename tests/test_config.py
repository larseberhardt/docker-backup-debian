from __future__ import annotations

import os
import stat
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import config, keys


class ConfigRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _sample(self):
        return {
            "schema_version": config.SCHEMA_VERSION,
            "name": "xibo",
            "stack_path": "/opt/xibo",
            "compose_file": "/opt/xibo/docker-compose.yml",
            "project_name": "xibo",
            "db_services": [{"service": "cms-db", "engine": "mysql"}],
            "repo": "/mnt/backups/xibo",
            "offsite": None,
            "retention": dict(config.DEFAULT_RETENTION),
        }

    def test_save_and_load(self):
        cfg = self._sample()
        path = config.save(cfg)
        self.assertTrue(os.path.exists(path))
        loaded = config.load("xibo")
        self.assertEqual(loaded["name"], "xibo")
        self.assertEqual(loaded["repo"], "/mnt/backups/xibo")
        self.assertEqual(loaded["db_services"][0]["service"], "cms-db")

    def test_config_mode_is_0640(self):
        config.save(self._sample())
        mode = stat.S_IMODE(os.stat(config.config_path("xibo")).st_mode)
        self.assertEqual(mode, 0o640)

    def test_list_names(self):
        config.save(self._sample())
        other = self._sample()
        other["name"] = "wordpress"
        config.save(other)
        self.assertEqual(config.list_names(), ["wordpress", "xibo"])

    def test_sanitize_name_rejects_traversal(self):
        with self.assertRaises(ValueError):
            config.sanitize_name("../etc/passwd")
        with self.assertRaises(ValueError):
            config.sanitize_name("a/b")

    def test_secret_sidecar_roundtrip(self):
        config.save_secret("xibo", "cms-db", "geheimes-pw")
        self.assertEqual(config.read_secret("xibo", "cms-db"), "geheimes-pw")
        mode = stat.S_IMODE(os.stat(config.secret_path("xibo", "cms-db")).st_mode)
        self.assertEqual(mode, 0o600)


class KeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_ensure_key_creates_and_reuses(self):
        p1 = keys.ensure_key("xibo")
        val1 = keys.read_key("xibo")
        p2 = keys.ensure_key("xibo")  # second call must NOT create a new key
        val2 = keys.read_key("xibo")
        self.assertEqual(p1, p2)
        self.assertEqual(val1, val2)
        self.assertTrue(len(val1) > 20)
        mode = stat.S_IMODE(os.stat(p1).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
