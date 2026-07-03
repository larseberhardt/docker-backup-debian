from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import detect, templates


class KeycloakTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("keycloak")
        self.assertEqual(tmpl["name"], "keycloak")
        self.assertTrue(tmpl["db_autodetect"])   # Postgres is dumped logically
        self.assertIsNone(tmpl.get("hooks"))
        self.assertFalse(tmpl.get("exclude_patterns"))  # realm data lives in Postgres

    def test_detects_keycloak_by_image(self):
        cj = {"services": {
            "keycloak": {"image": "quay.io/keycloak/keycloak:26.4.0"},
            "keycloak-postgres": {"image": "postgres:16.1-alpine"},
            "pgbackups": {"image": "prodrigestivill/postgres-backup-local"}}}
        self.assertEqual(templates.detect_template(cj), "keycloak")

    def test_detects_db_but_not_backup_sidecar(self):
        # The prodrigestivill/postgres-backup-local sidecar carries 'postgres' in its name
        # but is NOT a DB (backup marker in detect._NOT_DB_MARKERS) -> not dumped.
        cj = {"services": {
            "keycloak-postgres": {"image": "postgres:16.1-alpine",
                                  "environment": {"POSTGRES_DB": "keycloak",
                                                  "POSTGRES_USER": "keycloak",
                                                  "POSTGRES_PASSWORD": "x"}},
            "pgbackups": {"image": "prodrigestivill/postgres-backup-local"}}}
        dbs = detect.find_db_services(cj)
        self.assertEqual([d["service"] for d in dbs], ["keycloak-postgres"])
        self.assertEqual(dbs[0]["engine"], "postgres")


if __name__ == "__main__":
    unittest.main()
