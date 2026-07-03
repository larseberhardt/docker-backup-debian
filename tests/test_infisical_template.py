from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import detect, restic, templates


class InfisicalTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp  # empty override directory

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("infisical")  # validates; raises on error
        self.assertEqual(tmpl["name"], "infisical")
        self.assertTrue(tmpl["db_autodetect"])  # Postgres is dumped logically
        self.assertIsNone(tmpl.get("hooks"))    # pg_dump is consistent, no hooks

    def test_detects_infisical_by_backend_image(self):
        cj = {"services": {
            "backend": {"image": "infisical/infisical:v0.161.9"},
            "redis": {"image": "redis"},
            "db": {"image": "postgres:14-alpine"},
        }}
        self.assertEqual(templates.detect_template(cj), "infisical")

    def test_postgres_service_is_autodetected_as_db(self):
        # Foundation of the exclude decision: the DB MUST be detected, otherwise
        # excluding pg_data would wipe the only copy of the data.
        cj = {"services": {
            "backend": {"image": "infisical/infisical:v0.161.9"},
            "db": {"image": "postgres:14-alpine",
                   "environment": {"POSTGRES_USER": "infisical",
                                   "POSTGRES_PASSWORD": "x", "POSTGRES_DB": "infisical"}},
        }}
        dbs = detect.find_db_services(cj)
        self.assertEqual([d["service"] for d in dbs], ["db"])
        self.assertEqual(dbs[0]["engine"], "postgres")
        self.assertIsNone(dbs[0]["flavor"])  # no Supabase flavor
        creds = detect.extract_credentials(dbs[0]["environment"], "postgres", None)
        self.assertEqual(creds["databases"], ["infisical"])
        self.assertFalse(creds["dump_globals"])  # single DB, no cluster roles


class InfisicalExcludeInvariantTest(unittest.TestCase):
    """Only the regenerable Redis cache is excluded. The logical DB dump,
    and the '.env' must stay in the backup. The raw 'pg_data' volume is handled by
    the volume plan (dump replaces it), NOT by template excludes - a template
    exclude there would be redundant and fragile."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _excludes(self):
        tmpl = templates.load("infisical")
        return restic.resolve_excludes("/opt/infisical", [], tmpl["exclude_patterns"])

    def test_only_redis_cache_excluded(self):
        excludes = self._excludes()
        self.assertIn("/opt/infisical/redis_data", excludes)
        self.assertEqual(len(excludes), 1)  # ONLY the cache, nothing else

    def test_db_data_dump_and_env_not_excluded(self):
        excludes = self._excludes()
        # the dump and the .env (ENCRYPTION_KEY/AUTH_SECRET) stay in; pg_data is
        # handled by the volume plan, not by template excludes.
        for keep in ("/opt/infisical/pg_data",
                     "/opt/infisical/.docker-backup/dumps",
                     "/opt/infisical/.env",
                     "/opt/infisical"):
            for e in excludes:
                self.assertNotEqual(e, keep)
                self.assertFalse(keep.startswith(e.rstrip("/") + "/"),
                                 "protected path %r falls under exclude %r" % (keep, e))


if __name__ == "__main__":
    unittest.main()
