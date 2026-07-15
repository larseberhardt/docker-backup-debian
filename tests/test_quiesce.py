"""Built-in quiesce: detection, command builders, begin/release semantics."""

from __future__ import annotations

import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import detect, quiesce, util


class DetectQuiesceTest(unittest.TestCase):
    def _services(self, images):
        cj = {"services": {s: {"image": i} for s, i in images.items()}}
        return {q["service"]: q["engine"] for q in detect.find_quiesce_services(cj)}

    def test_mongo_and_redis_family(self):
        got = self._services({
            "mongo": "mongo:8.0", "bitnami": "docker.io/bitnami/mongodb:7.0",
            "cache": "redis:7-alpine", "valkey": "valkey/valkey:8", "keydb": "eqalpha/keydb",
        })
        self.assertEqual(got, {"mongo": "mongo", "bitnami": "mongo",
                               "cache": "redis", "valkey": "redis", "keydb": "redis"})

    def test_sql_engines_and_sidecars_excluded(self):
        got = self._services({
            "db": "postgres:16",
            "exporter": "oliver006/redis-exporter",
            "app": "ghcr.io/acme/app:2",
        })
        self.assertEqual(got, {})

    def test_credentials_official_mongo(self):
        creds = detect.extract_quiesce_credentials(
            {"MONGO_INITDB_ROOT_USERNAME": "root", "MONGO_INITDB_ROOT_PASSWORD": "pw"}, "mongo")
        self.assertEqual(creds["user_env_key"], "MONGO_INITDB_ROOT_USERNAME")
        self.assertEqual(creds["password_env_key"], "MONGO_INITDB_ROOT_PASSWORD")
        self.assertIsNone(creds["user_value"])

    def test_credentials_bitnami_mongo(self):
        creds = detect.extract_quiesce_credentials({"MONGODB_ROOT_PASSWORD": "pw"}, "mongo")
        self.assertEqual(creds["user_value"], "root")
        self.assertEqual(creds["password_env_key"], "MONGODB_ROOT_PASSWORD")

    def test_credentials_none(self):
        creds = detect.extract_quiesce_credentials({}, "mongo")
        self.assertEqual(creds, {"user_value": None, "user_env_key": None,
                                 "password_env_key": None})

    def test_credentials_redis(self):
        creds = detect.extract_quiesce_credentials({"REDIS_PASSWORD": "pw"}, "redis")
        self.assertEqual(creds["password_env_key"], "REDIS_PASSWORD")


class DataScopeTest(unittest.TestCase):
    def test_named_volume_data_is_staging(self):
        vols = [{"type": "volume", "source": "data", "target": "/data/db"}]
        self.assertEqual(quiesce.data_scope(vols, "mongo"), "staging")

    def test_bind_data_is_live(self):
        vols = [{"type": "bind", "source": "/opt/app/mongo", "target": "/data/db"}]
        self.assertEqual(quiesce.data_scope(vols, "mongo"), "live")

    def test_config_bind_does_not_force_live(self):
        # a read-only config bind must not extend the lock over the upload
        vols = [{"type": "bind", "source": "/opt/app/mongod.conf", "target": "/etc/mongod.conf"},
                {"type": "volume", "source": "data", "target": "/data/db"}]
        self.assertEqual(quiesce.data_scope(vols, "mongo"), "staging")

    def test_unknown_layout_is_live(self):
        self.assertEqual(quiesce.data_scope([], "mongo"), "live")

    def test_redis_bitnami_volume(self):
        vols = [{"type": "volume", "source": "r", "target": "/bitnami/redis"}]
        self.assertEqual(quiesce.data_scope(vols, "redis"), "staging")


class BuilderTest(unittest.TestCase):
    def test_mongo_lock_and_unlock(self):
        lock = quiesce.build_mongo_cmd(lock=True)
        unlock = quiesce.build_mongo_cmd(lock=False)
        self.assertEqual(lock[:2], ["sh", "-c"])
        self.assertIn("fsync:1,lock:true", lock[2])
        self.assertIn("fsyncUnlock", unlock[2])
        # shell probe + auth only via env vars, never argv
        for argv in (lock, unlock):
            self.assertIn("command -v mongosh || command -v mongo", argv[2])
            self.assertIn("DOCKER_BACKUP_MONGO_USER", argv[2])
            self.assertNotIn("--password", argv[2])

    def test_redis_bgsave_waits_for_completion(self):
        argv = quiesce.build_redis_bgsave_cmd(wait_seconds=42)
        script = argv[2]
        self.assertIn("BGSAVE", script)
        self.assertIn("rdb_bgsave_in_progress:0", script)
        self.assertIn("rdb_last_bgsave_status:ok", script)
        self.assertIn("-lt 42", script)
        self.assertIn("keydb-cli", script)  # redis/valkey/keydb probe


def _cfg(entries, **extra):
    cfg = {
        "name": "app",
        "stack_path": "/opt/app",
        "compose_file": "/opt/app/docker-compose.yml",
        "project_name": "app",
        "quiesce_services": entries,
    }
    cfg.update(extra)
    return cfg


_MONGO = {"service": "mongo", "engine": "mongo", "scope": "staging",
          "user_value": None, "user_env_key": None, "password_env_key": None}
_REDIS = {"service": "cache", "engine": "redis", "scope": "staging",
          "user_value": None, "user_env_key": None, "password_env_key": None}


class BeginReleaseTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        self._compose = mock.patch.object(quiesce, "compose")
        self.compose = self._compose.start()
        self.compose.service_running.return_value = True
        self.compose.exec_args.side_effect = (
            lambda cf, pd, svc, cmd, env=None, tty=False, project_name=None: ["exec", svc] + cmd
        )
        self._run = mock.patch.object(quiesce.util, "run")
        self.run = self._run.start()

    def tearDown(self):
        self._run.stop()
        self._compose.stop()

    def test_mongo_lock_held_redis_not(self):
        locked = quiesce.begin(_cfg([_MONGO, _REDIS]), None)
        self.assertEqual([e["service"] for e in locked], ["mongo"])
        self.assertEqual(self.run.call_count, 2)  # lock + bgsave

    def test_stopped_service_skipped(self):
        self.compose.service_running.return_value = False
        self.assertEqual(quiesce.begin(_cfg([_MONGO]), None), [])
        self.assertFalse(self.run.called)

    def test_disabled_flag_skips_all(self):
        locked = quiesce.begin(_cfg([_MONGO], quiesce_disabled=True), None)
        self.assertEqual(locked, [])
        self.assertFalse(self.run.called)

    def test_mongo_lock_failure_aborts(self):
        self.run.side_effect = util.CommandError(["x"], 1, "boom")
        with self.assertRaises(util.CommandError):
            quiesce.begin(_cfg([_MONGO]), None)

    def test_redis_failure_only_warns(self):
        self.run.side_effect = util.CommandError(["x"], 1, "boom")
        self.assertEqual(quiesce.begin(_cfg([_REDIS]), None), [])

    def test_release_scope_filter_and_removal(self):
        staging = dict(_MONGO, scope="staging", _env={})
        live = dict(_MONGO, service="mongo2", scope="live", _env={})
        locked = [staging, live]
        quiesce.release(_cfg([]), locked, scope="staging")
        self.assertEqual([e["service"] for e in locked], ["mongo2"])
        quiesce.release(_cfg([]), locked)  # None = rest
        self.assertEqual(locked, [])
        self.assertEqual(self.run.call_count, 2)

    def test_unlock_failure_raises_when_no_other_error(self):
        self.run.side_effect = util.CommandError(["x"], 1, "boom")
        locked = [dict(_MONGO, _env={})]
        with self.assertRaises(util.CommandError):
            quiesce.release(_cfg([]), locked)
        self.assertEqual(locked, [])  # removed despite failure (no retry loop)

    def test_unlock_failure_does_not_mask_active_exception(self):
        self.run.side_effect = util.CommandError(["x"], 1, "boom")
        locked = [dict(_MONGO, _env={})]
        try:
            raise RuntimeError("original")
        except RuntimeError:
            quiesce.release(_cfg([]), locked)  # must NOT raise here
        self.assertEqual(locked, [])

    def test_env_keys_resolved_from_compose_config(self):
        entry = dict(_MONGO, user_env_key="MONGO_INITDB_ROOT_USERNAME",
                     password_env_key="MONGO_INITDB_ROOT_PASSWORD")
        cj = {"services": {"mongo": {"environment": {
            "MONGO_INITDB_ROOT_USERNAME": "root", "MONGO_INITDB_ROOT_PASSWORD": "s3cr3t"}}}}
        locked = quiesce.begin(_cfg([entry]), cj)
        env = self.compose.exec_args.call_args.kwargs["env"]
        self.assertEqual(env["DOCKER_BACKUP_MONGO_USER"], "root")
        self.assertEqual(env["DOCKER_BACKUP_MONGO_PW"], "s3cr3t")
        self.assertEqual(locked[0]["_env"], env)
        # secret registered for log scrubbing
        self.assertIn("***", util.scrub("x s3cr3t y"))


class RunFlowOrderingTest(unittest.TestCase):
    """Lock window in _do_run: after dumps, staging release before the upload,
    live release right after it, everything released on error paths."""

    def setUp(self):
        import os
        import tempfile
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(True)
        from docker_backup.commands import run as run_cmd
        self.run_cmd = run_cmd
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(run_cmd, "manifest"),
            mock.patch.object(run_cmd, "quiesce"),
            mock.patch.object(run_cmd, "volumes"),
            mock.patch.object(
                run_cmd.compose, "config_json",
                return_value={"name": "app", "services": {}, "volumes": {}},
            ),
            mock.patch.object(
                run_cmd, "_verified_backup_named_volumes",
                side_effect=lambda configured, _db, _cj: configured,
            ),
        ]
        for p in self._patches:
            p.start()
        run_cmd.restic.last_snapshot.return_value = None

    def tearDown(self):
        import os
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def _base_cfg(self):
        return {
            "name": "app", "stack_path": "/opt/app",
            "compose_file": "/opt/app/docker-compose.yml", "project_name": "app",
            "key_file": "/k.key", "repo": "/repo", "offsite": None,
            "mount_check": None, "db_services": [],
            "named_volumes": [{"real_name": "app_data", "key": "data"}],
            "exclude_paths": [], "exclude_patterns": [],
            "retention": {"daily": 7, "weekly": 4, "monthly": 6},
            "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        }

    def test_ordering_tar_release_backup_release(self):
        calls = []
        rc = self.run_cmd
        rc.quiesce.begin.side_effect = lambda cfg, cj: (calls.append("begin"), [{"s": 1}])[1]
        rc.volumes.backup_named_volume.side_effect = lambda *a, **k: calls.append("tar")
        rc.quiesce.release.side_effect = lambda cfg, locked, scope=None: calls.append(
            "release:%s" % scope)
        rc.restic.backup.side_effect = lambda *a, **k: calls.append("backup")
        rc.restic.forget_prune.side_effect = lambda *a, **k: calls.append("forget")
        self.run_cmd._do_run(self._base_cfg())
        self.assertEqual(calls, [
            "begin", "tar", "release:staging", "backup", "release:live",
            "release:None", "forget",
        ])

    def test_release_runs_on_backup_failure(self):
        rc = self.run_cmd
        rc.quiesce.begin.return_value = [{"s": 1}]
        rc.restic.backup.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_cmd._do_run(self._base_cfg())
        scopes = [c.kwargs.get("scope") or (c.args[2] if len(c.args) > 2 else None)
                  for c in rc.quiesce.release.call_args_list]
        self.assertIn(None, scopes)  # the finally released the rest


if __name__ == "__main__":
    unittest.main()
