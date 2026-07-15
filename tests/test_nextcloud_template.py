from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import detect, restic, templates


class NextcloudTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("nextcloud")
        self.assertEqual(tmpl["name"], "nextcloud")
        self.assertTrue(tmpl["db_autodetect"])
        self.assertIsNone(tmpl.get("hooks"))

    def test_detects_nextcloud_by_image(self):
        cj = {"services": {
            "db": {"image": "mariadb:10.7"},
            "app": {"image": "nextcloud:33.0.5"}}}
        self.assertEqual(templates.detect_template(cj), "nextcloud")

    def test_root_password_selects_non_system_scope_seeded_by_configured_database(self):
        env = {"MYSQL_ROOT_PASSWORD": "x", "MYSQL_DATABASE": "nextcloud",
               "MYSQL_USER": "nextcloud", "MYSQL_PASSWORD": "y"}
        creds = detect.extract_credentials(env, "mysql")
        self.assertEqual(creds["user"], "root")
        self.assertTrue(creds["all_databases"])
        self.assertEqual(creds["databases"], ["nextcloud"])
        self.assertEqual(creds["database_scope"], "non-system")


class NextcloudExcludeInvariantTest(unittest.TestCase):
    """Only regenerable data (previews, cache, incomplete uploads) is
    excluded; user files, the DB data directory and the logical dump stay in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _excludes(self):
        tmpl = templates.load("nextcloud")
        return restic.resolve_excludes("/opt/nc", [], tmpl["exclude_patterns"])

    def test_regenerable_excluded(self):
        ex = self._excludes()
        self.assertIn("/opt/nc/nextcloud/data/appdata_*/preview", ex)
        self.assertIn("/opt/nc/nextcloud/data/*/cache", ex)
        self.assertIn("/opt/nc/nextcloud/data/*/uploads", ex)

    def test_user_files_db_and_dump_kept(self):
        ex = self._excludes()
        for keep in ("/opt/nc/nextcloud/data/alice/files",
                     "/opt/nc/nextcloud/config",
                     "/opt/nc/data",
                     "/opt/nc/.docker-backup/dumps"):
            for e in ex:
                self.assertNotEqual(e, keep)
                self.assertFalse(keep.startswith(e.rstrip("/") + "/"),
                                 "protected path %r falls under exclude %r" % (keep, e))


if __name__ == "__main__":
    unittest.main()
