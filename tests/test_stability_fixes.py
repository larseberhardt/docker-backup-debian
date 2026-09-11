"""Regression tests for the stability review fixes (release hardening)."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import compose, config, dbdump, detect, notify, quiesce, restic, runtime, util
from docker_backup.commands import create, templates as templates_cmd


class KeepWithinValidationTest(unittest.TestCase):
    def test_accepts_restic_durations(self):
        for v in ("30d", "2m", "1y6m", "36h", "1y2m3d4h", " 7d "):
            self.assertEqual(restic.validate_keep_within(v), v.strip())

    def test_rejects_typos(self):
        for v in ("30days", "", "d30", "1w", "30 d", None):
            with self.assertRaises(util.CommandError):
                restic.validate_keep_within(v)


class NotifySecurityTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(True)

    def tearDown(self):
        util.set_dry_run(False)

    def _cfg(self, **smtp):
        base = {"host": "smtp.example.com", "port": 587, "security": "starttls",
                "from": "a@example.com", "to": ["b@example.com"]}
        base.update(smtp)
        return {"enabled": True, "smtp": base}

    def test_unknown_security_mode_fails_closed(self):
        # A typo must not silently degrade to a plaintext login.
        self.assertFalse(notify.send("s", "b", self._cfg(security="startls")))

    def test_known_modes_accepted(self):
        for mode in ("starttls", "SSL", "none"):
            self.assertTrue(notify.send("s", "b", self._cfg(security=mode)))

    def test_invalid_port_is_skipped_not_crashing(self):
        self.assertFalse(notify.send("s", "b", self._cfg(port="abc")))


class RunTimeoutTest(unittest.TestCase):
    def test_timeout_becomes_command_error(self):
        with self.assertRaises(util.CommandError) as ctx:
            util.run(["/bin/sh", "-c", "sleep 5"], timeout=0.2)
        self.assertEqual(ctx.exception.returncode, 124)


class SecretPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_service_name_must_be_single_component(self):
        for bad in ("../x", "a/b", "", ".", ".."):
            with self.assertRaises(ValueError):
                config.secret_path("stack", bad)
        self.assertTrue(config.secret_path("stack", "db").endswith("stack-db.pw"))


class ImportProblemsTest(unittest.TestCase):
    def test_nonzero_rc_without_error_word_is_a_problem(self):
        proc = subprocess.CompletedProcess(["psql"], 1, "", 'service "db" is not running\n')
        problems = dbdump._problems_of(proc)
        self.assertTrue(problems)
        self.assertIn("rc=1", problems[0])

    def test_clean_run_has_no_problems(self):
        proc = subprocess.CompletedProcess(["psql"], 0, "", "")
        self.assertEqual(dbdump._problems_of(proc), [])


class MysqlStaticDatabaseListTest(unittest.TestCase):
    def test_option_like_name_rejected(self):
        with self.assertRaises(util.CommandError):
            dbdump._mysql_dump_cmd("mysqldump", "root", False, ["-x"], True)

    def test_plain_names_pass(self):
        cmd = dbdump._mysql_dump_cmd("mysqldump", "root", False, ["app"], True)
        self.assertEqual(cmd[-2:], ["--databases", "app"])


class RandomRootPasswordTest(unittest.TestCase):
    def test_any_nonempty_value_enables_random_root(self):
        env = {"MYSQL_RANDOM_ROOT_PASSWORD": "y", "MYSQL_ROOT_PASSWORD": "never-applied",
               "MYSQL_USER": "app", "MYSQL_PASSWORD": "pw", "MYSQL_DATABASE": "app"}
        creds = detect.extract_credentials(env, "mysql", "mysql")
        self.assertEqual(creds["user"], "app")


class ComposeInspectRaceTest(unittest.TestCase):
    def test_vanished_container_is_ignored(self):
        ls = subprocess.CompletedProcess([], 0, "a" * 64 + "\n" + "b" * 64 + "\n", "")
        inspect = subprocess.CompletedProcess(
            [], 1, "[]", "Error response from daemon: No such container: " + "b" * 64,
        )
        with mock.patch.object(compose.util, "run", side_effect=[ls, inspect]):
            self.assertEqual(compose.running_writable_bind_mounts_overlapping("/opt/x"), [])

    def test_other_inspect_error_still_raises(self):
        ls = subprocess.CompletedProcess([], 0, "a" * 64 + "\n", "")
        inspect = subprocess.CompletedProcess([], 1, "", "permission denied")
        with mock.patch.object(compose.util, "run", side_effect=[ls, inspect]):
            with self.assertRaises(util.CommandError):
                compose.running_writable_bind_mounts_overlapping("/opt/x")


class BackendEnvScopeTest(unittest.TestCase):
    def test_env_restored_after_block(self):
        tmp = tempfile.mkdtemp()
        env_file = os.path.join(tmp, "b.env")
        with open(env_file, "w") as f:
            f.write("AWS_ACCESS_KEY_ID=abc\n")
        os.environ.pop("AWS_ACCESS_KEY_ID", None)
        with runtime.backend_env({"backend_env_file": env_file}):
            self.assertEqual(os.environ.get("AWS_ACCESS_KEY_ID"), "abc")
        self.assertIsNone(os.environ.get("AWS_ACCESS_KEY_ID"))


class QuiescePartialLockTest(unittest.TestCase):
    def test_first_lock_released_when_second_fails(self):
        cfg = {"name": "s", "compose_file": "/opt/s/docker-compose.yml", "stack_path": "/opt/s",
               "quiesce_services": [
                   {"service": "m1", "engine": "mongo", "scope": "live"},
                   {"service": "m2", "engine": "mongo", "scope": "live"},
               ]}
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            script = argv[-1]
            if "fsync:1,lock:true" in script and any("m2" == a for a in argv):
                raise util.CommandError(argv, 1, "boom")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(quiesce.compose, "service_running", return_value=True), \
                mock.patch.object(quiesce.util, "run", side_effect=fake_run):
            with self.assertRaises(util.CommandError):
                quiesce.begin(cfg, {"services": {}})
        unlocks = [c for c in calls if "fsyncUnlock" in c[-1]]
        self.assertEqual(len(unlocks), 1)
        self.assertIn("m1", unlocks[0])


class CreateDryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(True)

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_dry_run_writes_nothing(self):
        stack = os.path.join(self.tmp, "stack")
        os.makedirs(stack)
        with open(os.path.join(stack, "docker-compose.yml"), "w") as f:
            f.write("services:\n  web:\n    image: nginx\n")
        cj = {"name": "stack", "services": {"web": {"image": "nginx"}}}
        with mock.patch.object(create.compose, "config_json", return_value=cj), \
                mock.patch.object(create.compose, "find_compose_file",
                                  return_value=os.path.join(stack, "docker-compose.yml"),
                                  create=True), \
                mock.patch.object(create.util, "require_root"):
            rc = create.create_one(stack, target_base="/mnt/backups", schedule_input="daily 03:00",
                                   offsite=None, name="stack", force=False, interactive=False)
        self.assertEqual(rc, 0)
        self.assertFalse(config.exists("stack"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "keys", "stack.key")))


class TemplatesListRobustnessTest(unittest.TestCase):
    def test_non_string_description(self):
        with mock.patch.object(templates_cmd.templates, "list_templates", return_value=["x"]), \
                mock.patch.object(templates_cmd.templates, "load",
                                  return_value={"name": "x", "description": None}):
            self.assertEqual(templates_cmd.cmd_templates(argparse.Namespace(templates_action="list")), 0)


if __name__ == "__main__":
    unittest.main()
