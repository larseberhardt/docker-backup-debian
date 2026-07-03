from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import detect, restic, templates


class SupabaseTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("supabase")
        self.assertEqual(tmpl["name"], "supabase")
        self.assertTrue(tmpl["db_autodetect"])
        self.assertIsNone(tmpl.get("hooks"))

    def test_detects_supabase_by_image(self):
        cj = {"services": {"db": {"image": "supabase/postgres:15.1.0.117"}}}
        self.assertEqual(templates.detect_template(cj), "supabase")

    def test_supabase_flavor_dumps_globals_and_internal_db(self):
        # Core flavor behavior: superuser dump incl. roles/globals + internal _supabase DB.
        cj = {"services": {"db": {"image": "supabase/postgres:15.1.0.117",
                                  "environment": {"POSTGRES_PASSWORD": "x"}}}}
        dbs = detect.find_db_services(cj)
        self.assertEqual([d["service"] for d in dbs], ["db"])
        self.assertEqual(dbs[0]["flavor"], "supabase")
        creds = detect.extract_credentials(dbs[0]["environment"], "postgres", "supabase")
        self.assertTrue(creds["dump_globals"])
        self.assertIn("_supabase", creds["databases"])


class SupabaseExcludeInvariantTest(unittest.TestCase):
    """Only regenerable caches/logs are excluded; the DB data directory and the
    logical dump stay in the backup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _excludes(self):
        tmpl = templates.load("supabase")
        return restic.resolve_excludes("/opt/supabase", [], tmpl["exclude_patterns"])

    def test_caches_excluded(self):
        ex = self._excludes()
        self.assertIn("/opt/supabase/volumes/storage/tmp", ex)
        self.assertIn("/opt/supabase/volumes/logs", ex)

    def test_db_data_and_dump_kept(self):
        ex = self._excludes()
        for keep in ("/opt/supabase/volumes/db/data",
                     "/opt/supabase/volumes/storage",
                     "/opt/supabase/.docker-backup/dumps"):
            for e in ex:
                self.assertNotEqual(e, keep)
                self.assertFalse(keep.startswith(e.rstrip("/") + "/"),
                                 "protected path %r falls under exclude %r" % (keep, e))


if __name__ == "__main__":
    unittest.main()
