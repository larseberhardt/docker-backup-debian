from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from _support import load_fixture

from docker_backup import compose, detect, util


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

    def test_config_json_can_replace_inherited_compose_environment(self):
        env = {"PATH": "/usr/bin", "HOME": "/root"}
        proc = SimpleNamespace(stdout="{}", stderr="", returncode=0)
        with mock.patch.object(compose.util, "run", return_value=proc) as run:
            self.assertEqual(
                compose.config_json(
                    "/opt/app/compose.yml", "/opt/app", "app",
                    env=env, env_replace=True,
                ),
                {},
            )
        run.assert_called_once_with(
            [
                "docker", "compose", "-f", "/opt/app/compose.yml",
                "--project-directory", "/opt/app", "-p", "app",
                "config", "--format", "json",
            ],
            capture=True, env=env, env_replace=True,
        )


class ComposeCleanupTest(unittest.TestCase):
    compose_file = "/opt/app/docker-compose.yml"
    project_dir = "/opt/app"
    project_name = "app-prod"

    @staticmethod
    def _proc(stdout=""):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    def test_down_all_removes_orphans_then_verifies_no_project_containers(self):
        with mock.patch.object(compose.util, "run") as run:
            run.side_effect = [self._proc(), self._proc()]

            compose.down_all(self.compose_file, self.project_dir, self.project_name)

        base = [
            "docker", "compose", "-f", self.compose_file,
            "--project-directory", self.project_dir, "-p", self.project_name,
        ]
        self.assertEqual(run.call_args_list, [
            mock.call(
                base + ["down", "--remove-orphans"],
                mutating=True, capture=False,
            ),
            mock.call(
                base + ["ps", "--all", "--quiet"],
                capture=True,
            ),
        ])

    def test_down_all_fails_if_project_container_remains(self):
        with mock.patch.object(compose.util, "run") as run:
            run.side_effect = [self._proc(), self._proc("deadbeef\n")]

            with self.assertRaises(util.CommandError):
                compose.down_all(self.compose_file, self.project_dir, self.project_name)

    def test_rm_service_force_stops_then_verifies_no_service_container(self):
        with mock.patch.object(compose.util, "run") as run:
            run.side_effect = [self._proc(), self._proc()]

            compose.rm_service(
                self.compose_file, self.project_dir, "db", self.project_name,
            )

        base = [
            "docker", "compose", "-f", self.compose_file,
            "--project-directory", self.project_dir, "-p", self.project_name,
        ]
        self.assertEqual(run.call_args_list, [
            mock.call(
                base + ["rm", "-f", "-s", "db"],
                mutating=True, capture=False,
            ),
            mock.call(
                base + ["ps", "--all", "--quiet", "db"],
                capture=True,
            ),
        ])

    def test_rm_service_fails_if_service_container_remains(self):
        with mock.patch.object(compose.util, "run") as run:
            run.side_effect = [self._proc(), self._proc("cafebabe\n")]

            with self.assertRaises(util.CommandError):
                compose.rm_service(
                    self.compose_file, self.project_dir, "db", self.project_name,
                )


class ComposeRunningBindGuardTest(unittest.TestCase):
    @staticmethod
    def _proc(stdout=""):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    def test_finds_writable_bind_ancestors_and_descendants(self):
        inspected = [
            {
                "Id": "a" * 64,
                "Name": "/parent-writer",
                "Mounts": [
                    {"Type": "bind", "Source": "/opt", "RW": True},
                ],
            },
            {
                "Id": "b" * 64,
                "Name": "/child-writer",
                "Mounts": [
                    {"Type": "bind", "Source": "/opt/gitlab/data", "RW": True},
                    {"Type": "bind", "Source": "/opt/gitlab/config", "RW": False},
                    {"Type": "volume", "Source": "named", "RW": True},
                ],
            },
        ]
        with mock.patch.object(compose.util, "run") as run:
            run.side_effect = [
                self._proc("%s\n%s\n" % ("a" * 64, "b" * 64)),
                self._proc(json.dumps(inspected)),
            ]

            found = compose.running_writable_bind_mounts_overlapping("/opt/gitlab")

        self.assertEqual(found, [
            {"container": "parent-writer", "source": "/opt"},
            {"container": "child-writer", "source": "/opt/gitlab/data"},
        ])

    def test_no_running_containers_needs_no_inspect(self):
        with mock.patch.object(
                compose.util, "run", return_value=self._proc(""),
        ) as run:
            self.assertEqual(
                compose.running_writable_bind_mounts_overlapping("/opt/gitlab"),
                [],
            )
        run.assert_called_once_with(
            ["docker", "container", "ls", "--quiet", "--no-trunc"],
            capture=True,
        )


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
