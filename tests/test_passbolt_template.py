from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import detect, templates


class PassboltTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("passbolt")  # validates; raises on error
        self.assertEqual(tmpl["name"], "passbolt")
        self.assertTrue(tmpl["db_autodetect"])   # MariaDB is dumped logically
        self.assertIsNone(tmpl.get("hooks"))
        self.assertFalse(tmpl.get("exclude_patterns"))  # DB data dir + gpg/jwt stay in

    def test_detects_passbolt_by_image(self):
        cj = {"services": {
            "db": {"image": "mariadb:10.3"},
            "passbolt": {"image": "passbolt/passbolt:5.3.2-1-ce"}}}
        self.assertEqual(templates.detect_template(cj), "passbolt")

    def test_random_root_falls_back_to_app_user_dump(self):
        # Foundation of the db_autodetect choice: with a random root, the app user
        # 'passbolt' dumps the DB of the same name (no root access needed).
        env = {"MYSQL_RANDOM_ROOT_PASSWORD": "true", "MYSQL_DATABASE": "passbolt",
               "MYSQL_USER": "passbolt", "MYSQL_PASSWORD": "x"}
        creds = detect.extract_credentials(env, "mysql")
        self.assertEqual(creds["user"], "passbolt")
        self.assertEqual(creds["source"], "app")
        self.assertTrue(creds["all_databases"])
        self.assertEqual(creds["databases"], ["passbolt"])
        self.assertEqual(creds["database_scope"], "non-system")


if __name__ == "__main__":
    unittest.main()
