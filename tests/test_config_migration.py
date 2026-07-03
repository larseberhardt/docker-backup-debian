from __future__ import annotations

import json
import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import config


class ConfigMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _write_v1(self):
        os.makedirs(config.configs_dir(), exist_ok=True)
        path = config.config_path("legacy")
        v1 = {
            "schema_version": 1,
            "name": "legacy",
            "stack_path": "/opt/legacy",
            "compose_file": "/opt/legacy/docker-compose.yml",
            "repo": "/mnt/backups/legacy",
            "db_services": [{"service": "db", "engine": "mysql"}],
            "retention": {"daily": 7, "weekly": 4, "monthly": 6},
        }
        with open(path, "w") as f:
            json.dump(v1, f)
        return path

    def test_v1_loads_with_safe_defaults(self):
        self._write_v1()
        cfg = config.load("legacy")
        self.assertEqual(cfg["hooks"], {"pre_backup": [], "post_backup": [], "restore": []})
        self.assertFalse(cfg["hooks_allowed"])    # security-relevant: NEVER flipped to True
        self.assertTrue(cfg["db_autodetect"])     # current behavior is preserved
        self.assertEqual(cfg["exclude_patterns"], [])
        self.assertIsNone(cfg["retention"]["keep_within"])
        self.assertIsNone(cfg["template"])

    def test_load_does_not_persist_on_read(self):
        path = self._write_v1()
        before = open(path).read()
        config.load("legacy")
        after = open(path).read()
        self.assertEqual(before, after)  # v1 file stays unchanged on disk

    def test_defaults_idempotent(self):
        once = config._defaults({"name": "x"})
        twice = config._defaults(config._defaults({"name": "x"}))
        self.assertEqual(once, twice)

    def test_defaults_preserve_existing_values(self):
        cfg = config._defaults({
            "name": "x",
            "hooks_allowed": True,
            "hooks": {"pre_backup": [{"cmd": "echo hi"}]},
            "db_autodetect": False,
        })
        self.assertTrue(cfg["hooks_allowed"])
        self.assertFalse(cfg["db_autodetect"])
        self.assertEqual(cfg["hooks"]["pre_backup"], [{"cmd": "echo hi"}])
        self.assertEqual(cfg["hooks"]["post_backup"], [])  # missing phase filled in


if __name__ == "__main__":
    unittest.main()
