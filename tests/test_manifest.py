from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import config, manifest, util


def _sample_cfg():
    return {
        "schema_version": config.SCHEMA_VERSION,
        "name": "xibo",
        "stack_path": "/opt/xibo",
        "compose_file": "/opt/xibo/docker-compose.yml",
        "project_name": "xibo",
        "env_files": ["/opt/xibo/.env"],
        "db_services": [{"service": "db", "engine": "postgres",
                         "password_source": "env:POSTGRES_PASSWORD"}],
        "named_volumes": [{"key": "db_data", "real_name": "xibo_db_data"}],
        "exclude_paths": ["/opt/xibo/data/pg"],
        "repo": "/mnt/backups/xibo",
        "offsite": "s3:bucket/xibo",
        "backend_env_file": "/etc/docker-backup/backends/xibo.env",
        "key_file": "/etc/docker-backup/keys/xibo.key",
        "retention": dict(config.DEFAULT_RETENTION),
        "mount_check": "/mnt/backups",
    }


class DeriveTest(unittest.TestCase):
    def test_includes_restore_fields(self):
        man = manifest.derive(_sample_cfg())
        self.assertEqual(man["manifest_schema_version"], manifest.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(man["name"], "xibo")
        self.assertEqual(man["stack_path"], "/opt/xibo")
        self.assertEqual(man["project_name"], "xibo")
        self.assertEqual(man["db_services"][0]["service"], "db")
        self.assertEqual(man["named_volumes"][0]["key"], "db_data")

    def test_compose_file_is_basename(self):
        man = manifest.derive(_sample_cfg())
        self.assertEqual(man["compose_file"], "docker-compose.yml")

    def test_excludes_secret_and_local_fields(self):
        man = manifest.derive(_sample_cfg())
        for k in ("key_file", "backend_env_file", "repo", "offsite",
                  "mount_check", "env_files"):
            self.assertNotIn(k, man)
        # Repo is kept only for diagnostics.
        self.assertEqual(man["source_repo"], "/mnt/backups/xibo")

    def test_no_db_password_value_field(self):
        man = manifest.derive(_sample_cfg())
        for db in man["db_services"]:
            # Only the source reference, never a plaintext password field.
            self.assertNotIn("password", db)
            self.assertEqual(db["password_source"], "env:POSTGRES_PASSWORD")

    def test_never_includes_hooks_or_shell(self):
        cfg = _sample_cfg()
        cfg["hooks"] = {
            "pre_backup": [{"cmd": "docker exec gitlab gitlab-backup create CRON=1"}],
            "post_backup": [{"cmd": "rm -f /opt/gitlab/gitlab/data/backups/*.tar"}],
            "restore": [{"cmd": "docker exec gitlab gitlab-backup restore FORCE=yes"}],
        }
        cfg["hooks_allowed"] = True
        man = manifest.derive(cfg)
        self.assertNotIn("hooks", man)
        self.assertNotIn("hooks_allowed", man)
        self.assertNotIn("hooks_fingerprint", man)
        # No shell strings anywhere in the manifest.
        blob = json.dumps(man)
        self.assertNotIn("gitlab-backup", blob)
        self.assertNotIn("gitlab-ctl", blob)

    def test_includes_exclude_patterns_and_db_autodetect(self):
        cfg = _sample_cfg()
        cfg["exclude_patterns"] = ["gitlab/logs", "gitlab/data/postgresql"]
        cfg["db_autodetect"] = False
        man = manifest.derive(cfg)
        self.assertEqual(man["exclude_patterns"], ["gitlab/logs", "gitlab/data/postgresql"])
        self.assertFalse(man["db_autodetect"])


class WriteReadTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        util.set_dry_run(False)

    def test_write_then_read_roundtrip(self):
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        os.makedirs(cfg["repo"])
        path = manifest.write(cfg)
        self.assertEqual(path, os.path.join(cfg["repo"], manifest.MANIFEST_BASENAME))
        self.assertTrue(os.path.exists(path))
        man = manifest.read(cfg["repo"])
        self.assertIsNotNone(man)
        self.assertEqual(man["name"], "xibo")
        self.assertEqual(man["compose_file"], "docker-compose.yml")

    def test_write_mode_is_0644(self):
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        path = manifest.write(cfg)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o644)

    def test_write_leaves_no_tmp_file(self):
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        manifest.write(cfg)
        leftovers = [f for f in os.listdir(cfg["repo"]) if f.startswith(".manifest.")]
        self.assertEqual(leftovers, [])

    def test_write_skips_remote_repo(self):
        cfg = _sample_cfg()
        cfg["repo"] = "s3:bucket/xibo"
        self.assertIsNone(manifest.write(cfg))

    def test_write_skips_dry_run(self):
        util.set_dry_run(True)
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        self.assertIsNone(manifest.write(cfg))
        self.assertFalse(os.path.exists(os.path.join(cfg["repo"], manifest.MANIFEST_BASENAME)))

    def test_read_missing_returns_none(self):
        self.assertIsNone(manifest.read(os.path.join(self.tmp, "nope")))


class CfgFromManifestTest(unittest.TestCase):
    def test_overrides_repo_and_key(self):
        man = manifest.derive(_sample_cfg())
        cfg = manifest.cfg_from_manifest(man, "/mnt/other/xibo", "/root/xibo.key")
        self.assertEqual(cfg["repo"], "/mnt/other/xibo")
        self.assertEqual(cfg["key_file"], "/root/xibo.key")
        self.assertIsNone(cfg["mount_check"])
        self.assertIsNone(cfg["backend_env_file"])
        self.assertEqual(cfg["stack_path"], "/opt/xibo")
        self.assertEqual(cfg["db_services"][0]["service"], "db")

    def test_name_override(self):
        man = manifest.derive(_sample_cfg())
        cfg = manifest.cfg_from_manifest(man, "/mnt/x", "/k", name="xibo-test")
        self.assertEqual(cfg["name"], "xibo-test")

    def test_sets_empty_hooks_not_allowed(self):
        man = manifest.derive(_sample_cfg())
        cfg = manifest.cfg_from_manifest(man, "/mnt/x", "/k")
        self.assertEqual(cfg["hooks"], {"pre_backup": [], "post_backup": [], "restore": []})
        self.assertFalse(cfg["hooks_allowed"])
        self.assertIsNone(cfg["hooks_fingerprint"])

    def test_v1_manifest_read_no_keyerror(self):
        # An old (v1) manifest without the new keys must be readable by a v2 binary
        # without a KeyError.
        v1 = {
            "manifest_schema_version": 1,
            "config_schema_version": 1,
            "name": "xibo",
            "stack_path": "/opt/xibo",
            "compose_file": "docker-compose.yml",
            "project_name": "xibo",
            "db_services": [],
            "named_volumes": [],
            "exclude_paths": [],
            "retention": {"daily": 7, "weekly": 4, "monthly": 6},
        }
        cfg = manifest.cfg_from_manifest(v1, "/mnt/x", "/k")
        self.assertEqual(cfg["exclude_patterns"], [])
        self.assertTrue(cfg["db_autodetect"])  # default when not present in the manifest


class FindManifestsTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        self.base = tempfile.mkdtemp()

    def _make_repo(self, name):
        cfg = _sample_cfg()
        cfg["name"] = name
        cfg["repo"] = os.path.join(self.base, name)
        os.makedirs(cfg["repo"])
        manifest.write(cfg)
        return cfg["repo"]

    def test_discovers_repos_under_base(self):
        self._make_repo("alpha")
        self._make_repo("beta")
        os.makedirs(os.path.join(self.base, "not-a-repo"))  # without a manifest
        found = manifest.find_manifests(self.base)
        names = [man["name"] for _repo, man in found]
        self.assertEqual(names, ["alpha", "beta"])

    def test_does_not_descend_into_repo(self):
        repo = self._make_repo("alpha")
        # Simulate restic internals -- must NOT show up as their own repos.
        os.makedirs(os.path.join(repo, "data", "ab"))
        found = manifest.find_manifests(self.base)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], repo)

    def test_missing_base_returns_empty(self):
        self.assertEqual(manifest.find_manifests(os.path.join(self.base, "nope")), [])


if __name__ == "__main__":
    unittest.main()
