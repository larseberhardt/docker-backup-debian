from __future__ import annotations

import unittest

from _support import load_fixture

from docker_backup import compose, detect


class ComposeVolumePlanTest(unittest.TestCase):
    def setUp(self):
        self.cj = load_fixture("compose_xibo.json")
        self.db_services = [
            {"service": d["service"], "engine": d["engine"]}
            for d in detect.find_db_services(self.cj)
        ]

    def test_raw_db_dir_excluded(self):
        excludes, _named = compose.collect_volume_backup_plan(self.cj, self.db_services)
        self.assertIn("/opt/xibo/shared/db", excludes)

    def test_raw_data_exclude_annotated_on_db(self):
        compose.collect_volume_backup_plan(self.cj, self.db_services)
        self.assertEqual(self.db_services[0]["raw_data_exclude"], "/opt/xibo/shared/db")
        self.assertEqual(self.db_services[0]["data_dir_target"], "/var/lib/mysql")

    def test_named_volume_detected_with_real_name(self):
        _excludes, named = compose.collect_volume_backup_plan(self.cj, self.db_services)
        self.assertEqual(len(named), 1)
        nv = named[0]
        self.assertEqual(nv["key"], "library")
        self.assertEqual(nv["real_name"], "xibo_library")
        self.assertEqual(nv["target"], "/var/www/cms/library")

    def test_bind_mount_not_in_named_volumes(self):
        _excludes, named = compose.collect_volume_backup_plan(self.cj, self.db_services)
        keys = [nv["key"] for nv in named]
        self.assertNotIn("/opt/xibo/shared/cms/custom", keys)

    def test_real_volume_name_fallback_to_project(self):
        self.assertEqual(compose.real_volume_name({}, "data", "proj"), "proj_data")
        self.assertEqual(compose.real_volume_name({"data": {"name": "x_data"}}, "data", "proj"), "x_data")

    def test_repo_for_appends_name(self):
        self.assertEqual(compose.repo_for("/mnt/backups", "xibo"), "/mnt/backups/xibo")
        self.assertEqual(compose.repo_for("/mnt/backups/", "xibo"), "/mnt/backups/xibo")
        self.assertEqual(compose.repo_for("s3:host/bucket", "xibo"), "s3:host/bucket/xibo")

    def test_is_local_repo(self):
        self.assertTrue(compose.is_local_repo("/mnt/backups"))
        self.assertFalse(compose.is_local_repo("s3:host/bucket"))
        self.assertFalse(compose.is_local_repo("sftp:user@host:/srv"))

    def test_exec_args_no_tty_and_env(self):
        argv = compose.exec_args(
            "/c/docker-compose.yml", "/c", "db",
            ["mysqldump", "--all-databases"],
            env={"MYSQL_PWD": "secret"}, tty=False, project_name="proj",
        )
        self.assertIn("-T", argv)
        self.assertIn("exec", argv)
        self.assertIn("-e", argv)
        self.assertIn("MYSQL_PWD=secret", argv)
        self.assertEqual(argv[-2:], ["mysqldump", "--all-databases"])
        self.assertEqual(argv[argv.index("db") + 1], "mysqldump")


class Postgres18ParentLayoutTest(unittest.TestCase):
    """postgres:18+ mounts the PARENT /var/lib/postgresql (PGDATA in <parent>/18/docker).
    The raw-data exclude/skip must match this layout too."""

    def _cj(self, volume):
        return {
            "name": "paperless",
            "services": {"db": {"image": "postgres:18", "volumes": [volume]}},
            "volumes": {"pgdata": None},
        }

    def _dbs(self):
        return [{"service": "db", "engine": "postgres"}]

    def test_pg18_bind_parent_is_excluded(self):
        cj = self._cj({"type": "bind", "source": "/opt/paperless/pgdata",
                       "target": "/var/lib/postgresql"})
        dbs = self._dbs()
        excludes, named = compose.collect_volume_backup_plan(cj, dbs)
        self.assertEqual(excludes, ["/opt/paperless/pgdata"])
        self.assertEqual(named, [])
        self.assertEqual(dbs[0]["raw_data_exclude"], "/opt/paperless/pgdata")
        self.assertEqual(dbs[0]["data_dir_target"], "/var/lib/postgresql")

    def test_pg18_named_volume_parent_is_skipped(self):
        cj = self._cj({"type": "volume", "source": "pgdata",
                       "target": "/var/lib/postgresql"})
        _excludes, named = compose.collect_volume_backup_plan(cj, self._dbs())
        self.assertEqual(named, [])  # dump replaces it, no tar

    def test_classic_data_dir_still_excluded(self):
        cj = self._cj({"type": "bind", "source": "/opt/app/db",
                       "target": "/var/lib/postgresql/data"})
        excludes, _named = compose.collect_volume_backup_plan(cj, self._dbs())
        self.assertEqual(excludes, ["/opt/app/db"])

    def test_parent_not_excluded_for_non_db_service(self):
        # only DETECTED DB services get the exclude — an app bind at the same
        # target on a non-DB service stays in the backup
        cj = self._cj({"type": "bind", "source": "/opt/app/data",
                       "target": "/var/lib/postgresql"})
        excludes, _named = compose.collect_volume_backup_plan(cj, [])
        self.assertEqual(excludes, [])


if __name__ == "__main__":
    unittest.main()

