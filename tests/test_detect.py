from __future__ import annotations

import unittest

from _support import load_fixture

from docker_backup import detect


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.cj = load_fixture("compose_xibo.json")

    def test_finds_single_mysql_db(self):
        dbs = detect.find_db_services(self.cj)
        self.assertEqual(len(dbs), 1)
        self.assertEqual(dbs[0]["service"], "cms-db")
        self.assertEqual(dbs[0]["engine"], "mysql")

    def test_app_user_fallback_on_random_root(self):
        dbs = detect.find_db_services(self.cj)
        creds = detect.extract_credentials(dbs[0]["environment"], "mysql")
        self.assertEqual(creds["source"], "app")
        self.assertEqual(creds["user"], "cms")
        self.assertFalse(creds["all_databases"])
        self.assertEqual(creds["databases"], ["cms"])
        self.assertEqual(creds["password_env_key"], "MYSQL_PASSWORD")

    def test_root_used_when_real_root_password(self):
        env = {"MYSQL_ROOT_PASSWORD": "rootpw", "MYSQL_USER": "app", "MYSQL_PASSWORD": "x"}
        creds = detect.extract_credentials(env, "mysql")
        self.assertEqual(creds["source"], "root")
        self.assertEqual(creds["user"], "root")
        self.assertTrue(creds["all_databases"])
        self.assertEqual(creds["password_env_key"], "MYSQL_ROOT_PASSWORD")

    def test_mariadb_env_aliases(self):
        env = {"MARIADB_RANDOM_ROOT_PASSWORD": "yes", "MARIADB_USER": "u",
               "MARIADB_PASSWORD": "p", "MARIADB_DATABASE": "d"}
        creds = detect.extract_credentials(env, "mysql")
        self.assertEqual(creds["user"], "u")
        self.assertEqual(creds["password_env_key"], "MARIADB_PASSWORD")
        self.assertEqual(creds["databases"], ["d"])

    def test_postgres_credentials(self):
        env = {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "appdb"}
        creds = detect.extract_credentials(env, "postgres")
        self.assertEqual(creds["user"], "app")
        self.assertEqual(creds["databases"], ["appdb"])
        self.assertEqual(creds["password_env_key"], "POSTGRES_PASSWORD")

    def test_postgres_defaults(self):
        creds = detect.extract_credentials({}, "postgres")
        self.assertEqual(creds["user"], "postgres")
        self.assertEqual(creds["databases"], ["postgres"])
        self.assertEqual(creds["source"], "unknown")
        self.assertFalse(creds["dump_globals"])

    def test_supabase_flavor_detected(self):
        cj = {"services": {"db": {"image": "supabase/postgres:15.8.1.085"}}}
        dbs = detect.find_db_services(cj)
        self.assertEqual(len(dbs), 1)
        self.assertEqual(dbs[0]["engine"], "postgres")
        self.assertEqual(dbs[0]["flavor"], "supabase")

    def test_plain_postgres_has_no_flavor(self):
        cj = {"services": {"db": {"image": "postgres:16"}}}
        self.assertIsNone(detect.find_db_services(cj)[0]["flavor"])

    def test_supabase_credentials_defaults(self):
        # The Supabase db service sets NO POSTGRES_USER -> superuser supabase_admin.
        env = {"POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "postgres"}
        creds = detect.extract_credentials(env, "postgres", "supabase")
        self.assertEqual(creds["user"], "supabase_admin")
        self.assertEqual(creds["databases"], ["postgres", "_supabase"])
        self.assertTrue(creds["dump_globals"])
        self.assertEqual(creds["source"], "supabase")
        self.assertEqual(creds["password_env_key"], "POSTGRES_PASSWORD")

    def test_supabase_respects_explicit_user_and_db(self):
        env = {"POSTGRES_PASSWORD": "pw", "POSTGRES_USER": "custom", "POSTGRES_DB": "appdb"}
        creds = detect.extract_credentials(env, "postgres", "supabase")
        self.assertEqual(creds["user"], "custom")
        self.assertEqual(creds["databases"], ["appdb", "_supabase"])

    def test_supabase_does_not_duplicate_internal_db(self):
        env = {"POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "_supabase"}
        creds = detect.extract_credentials(env, "postgres", "supabase")
        self.assertEqual(creds["databases"], ["_supabase"])

    def test_sidecars_are_not_databases(self):
        # Image names containing a DB token but which are not a DB must not be detected.
        cj = {"services": {
            "db": {"image": "supabase/postgres:15.8.1.085"},
            "meta": {"image": "supabase/postgres-meta:v0.91.6"},
            "pgbackups": {"image": "prodrigestivill/postgres-backup-local:15-alpine"},
            "exporter": {"image": "wrouesnel/postgres_exporter:latest"},
        }}
        dbs = detect.find_db_services(cj)
        self.assertEqual([d["service"] for d in dbs], ["db"])

    def test_supabase_stack_detects_only_db(self):
        # Full Supabase stack: only 'db' is a database. In particular
        # PostgREST ("rest") must not be detected despite the "postgres" prefix
        # in its name -- pg_dump does not exist there (rc=127).
        cj = {"services": {
            "studio": {"image": "supabase/studio:2025.06.02"},
            "kong": {"image": "kong:2.8.1"},
            "auth": {"image": "supabase/gotrue:v2.176.1"},
            "rest": {"image": "postgrest/postgrest:v12.2.12"},
            "realtime": {"image": "supabase/realtime:v2.36.18"},
            "meta": {"image": "supabase/postgres-meta:v0.91.6"},
            "functions": {"image": "supabase/edge-runtime:v1.67.4"},
            "analytics": {"image": "supabase/logflare:1.14.2"},
            "db": {"image": "supabase/postgres:15.8.1.085"},
            "vector": {"image": "timberio/vector:0.28.1-alpine"},
            "supavisor": {"image": "supabase/supavisor:2.5.7"},
            "imgproxy": {"image": "darthsim/imgproxy:v3.8.0"},
        }}
        dbs = detect.find_db_services(cj)
        self.assertEqual([d["service"] for d in dbs], ["db"])

    def test_image_engine_variants(self):
        cases = {
            "mariadb:11": "mysql",
            "mysql:8.0": "mysql",
            "mysql/mysql-server:8.0": "mysql",
            "percona/percona-xtradb-cluster:8.0": "mysql",
            "docker.io/library/postgres:16": "postgres",
            "postgis/postgis:16-3.4": "postgres",
            "bitnami/postgresql:16": "postgres",
            "myregistry.local/team/postgres12": "postgres",
            "postgrest/postgrest:v12.2.12": None,
            "supabase/postgres-meta:v0.91.6": None,
            "prodrigestivill/postgres-backup-local:15-alpine": None,
            "redis:7": None,
            "nginx": None,
        }
        for image, expected in cases.items():
            cj = {"services": {"x": {"image": image}}}
            dbs = detect.find_db_services(cj)
            got = dbs[0]["engine"] if dbs else None
            self.assertEqual(got, expected, "Image %s -> %s" % (image, got))


if __name__ == "__main__":
    unittest.main()
