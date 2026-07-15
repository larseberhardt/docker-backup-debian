from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import config, hooks, manifest, templates, util


_SNAPSHOT_ID = "0123456789abcdef" * 4


def _derive(cfg, snapshot_id=_SNAPSHOT_ID, external_bind_descriptors=None):
    return manifest.derive(cfg, snapshot_id, external_bind_descriptors)


def _write(cfg, snapshot_id=_SNAPSHOT_ID, external_bind_descriptors=None):
    return manifest.write(cfg, snapshot_id, external_bind_descriptors)


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
        man = _derive(_sample_cfg())
        self.assertEqual(man["manifest_schema_version"], manifest.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(man["snapshot_id"], _SNAPSHOT_ID)
        self.assertEqual(man["name"], "xibo")
        self.assertEqual(man["stack_path"], "/opt/xibo")
        self.assertEqual(man["project_name"], "xibo")
        self.assertEqual(man["db_services"][0]["service"], "db")
        self.assertEqual(man["named_volumes"][0]["key"], "db_data")
        self.assertEqual(man["external_bind_descriptors"], [])

    def test_compose_file_is_basename(self):
        man = _derive(_sample_cfg())
        self.assertEqual(man["compose_file"], "docker-compose.yml")

    def test_excludes_secret_and_local_fields(self):
        man = _derive(_sample_cfg())
        for k in ("key_file", "backend_env_file", "repo", "offsite",
                  "mount_check", "env_files"):
            self.assertNotIn(k, man)
        # Repo is kept only for diagnostics.
        self.assertEqual(man["source_repo"], "/mnt/backups/xibo")

    def test_no_db_password_value_field(self):
        man = _derive(_sample_cfg())
        for db in man["db_services"]:
            # Only the source reference, never a plaintext password field.
            self.assertNotIn("password", db)
            self.assertEqual(db["password_source"], "env:POSTGRES_PASSWORD")

    def test_rejects_unresolved_dynamic_mysql_scope(self):
        cfg = _sample_cfg()
        cfg["db_services"] = [{
            "service": "db", "engine": "mysql", "auth_user": "root",
            "all_databases": False, "databases": ["app"],
            "database_scope": "non-system",
        }]
        with self.assertRaisesRegex(ValueError, "must be resolved"):
            _derive(cfg)

    def test_never_includes_hooks_or_shell(self):
        cfg = _sample_cfg()
        cfg["hooks"] = {
            "pre_backup": [{"cmd": "docker exec gitlab gitlab-backup create CRON=1"}],
            "post_backup": [{"cmd": "rm -f /opt/gitlab/gitlab/data/backups/*.tar"}],
            "restore": [{"cmd": "docker exec gitlab gitlab-backup restore FORCE=yes"}],
        }
        cfg["hooks_allowed"] = True
        man = _derive(cfg)
        self.assertNotIn("hooks", man)
        self.assertNotIn("hooks_allowed", man)
        self.assertNotIn("hooks_fingerprint", man)
        self.assertTrue(man["custom_restore_required"])
        # No shell strings anywhere in the manifest.
        blob = json.dumps(man)
        self.assertNotIn("gitlab-backup", blob)
        self.assertNotIn("gitlab-ctl", blob)

    def test_template_descriptor_is_allowlisted_and_shell_free(self):
        sentinel = "PWN_SENTINEL"
        cfg = _sample_cfg()
        tmpl = templates.load("gitlab")
        cfg["hooks"] = templates.to_hooks(tmpl)
        cfg["template"] = {
            "name": "gitlab",
            "version": "1",
            "source": "builtin",
            # Neither attacker-controlled extra field may reach the manifest.
            "cmd": sentinel,
            "nested": {"hooks": [{"cmd": sentinel}]},
        }

        man = _derive(cfg)

        self.assertEqual(set(man["template"]), {
            "name", "version", "source", "hooks_fingerprint", "hooks_present",
        })
        self.assertEqual(man["template"]["name"], "gitlab")
        self.assertEqual(
            man["template"]["hooks_fingerprint"],
            hooks.compute_definition_fingerprint(cfg["hooks"]),
        )
        self.assertTrue(man["template"]["hooks_present"])
        blob = json.dumps(man)
        self.assertNotIn(sentinel, blob)
        self.assertNotIn("gitlab-backup", blob)

    def test_template_descriptor_hashes_actual_config_overrides(self):
        cfg = _sample_cfg()
        tmpl = templates.load("gitlab")
        cfg["template"] = templates.provenance(tmpl, source="builtin")
        cfg["hooks"] = templates.to_hooks(tmpl)
        original = _derive(cfg)["template"]["hooks_fingerprint"]
        cfg["hooks"]["restore"][0]["cwd"] = "/tmp"

        changed = _derive(cfg)["template"]["hooks_fingerprint"]

        self.assertNotEqual(original, changed)

    def test_includes_exclude_patterns_and_db_autodetect(self):
        cfg = _sample_cfg()
        cfg["exclude_patterns"] = ["gitlab/logs", "gitlab/data/postgresql"]
        cfg["db_autodetect"] = False
        man = _derive(cfg)
        self.assertEqual(man["exclude_patterns"], ["gitlab/logs", "gitlab/data/postgresql"])
        self.assertFalse(man["db_autodetect"])

    def test_requires_full_lowercase_snapshot_id(self):
        invalid = [
            None,
            "",
            "abcd1234",
            "A" * 64,
            "g" * 64,
            ("0" * 63) + " ",
            123,
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "full 64-character lowercase hex"):
                    manifest.derive(_sample_cfg(), value)

    def test_external_bind_descriptors_are_allowlisted_and_bound_to_selected_paths(self):
        cfg = _sample_cfg()
        cfg["extra_backup_paths"] = ["/srv/appdata"]
        descriptor = {
            "service": "web", "target": "/var/lib/app", "source": "/srv/appdata",
        }

        man = _derive(cfg, external_bind_descriptors=[descriptor])

        self.assertEqual(man["external_bind_descriptors"], [descriptor])

        with self.assertRaisesRegex(ValueError, "contain only"):
            _derive(
                cfg,
                external_bind_descriptors=[dict(descriptor, command="PWN_SENTINEL")],
            )

    def test_external_paths_require_complete_portable_descriptors(self):
        cfg = _sample_cfg()
        cfg["extra_backup_paths"] = ["/srv/a", "/srv/b"]

        for descriptors in (None, [], [
            {"service": "web", "target": "/a", "source": "/srv/a"},
        ]):
            with self.subTest(descriptors=descriptors):
                with self.assertRaisesRegex(ValueError, "external.bind|extra_backup"):
                    _derive(cfg, external_bind_descriptors=descriptors)


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
        path = _write(cfg)
        self.assertEqual(path, os.path.join(cfg["repo"], manifest.MANIFEST_BASENAME))
        self.assertTrue(os.path.exists(path))
        man = manifest.read(cfg["repo"])
        self.assertIsNotNone(man)
        self.assertEqual(man["name"], "xibo")
        self.assertEqual(man["compose_file"], "docker-compose.yml")
        self.assertEqual(man["snapshot_id"], _SNAPSHOT_ID)

    def test_write_carries_portable_external_bind_descriptors(self):
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo-external")
        cfg["extra_backup_paths"] = ["/srv/appdata"]
        descriptors = [{
            "service": "web", "target": "/data", "source": "/srv/appdata",
        }]

        _write(cfg, external_bind_descriptors=descriptors)

        man = manifest.read(cfg["repo"])
        self.assertEqual(man["external_bind_descriptors"], descriptors)

    def test_write_mode_is_0644(self):
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        path = _write(cfg)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o644)

    def test_write_leaves_no_tmp_file(self):
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        _write(cfg)
        leftovers = [f for f in os.listdir(cfg["repo"]) if f.startswith(".manifest.")]
        self.assertEqual(leftovers, [])

    def test_write_skips_remote_repo(self):
        cfg = _sample_cfg()
        cfg["repo"] = "s3:bucket/xibo"
        self.assertIsNone(_write(cfg))

    def test_write_skips_dry_run(self):
        util.set_dry_run(True)
        cfg = _sample_cfg()
        cfg["repo"] = os.path.join(self.tmp, "xibo")
        self.assertIsNone(_write(cfg))
        self.assertFalse(os.path.exists(os.path.join(cfg["repo"], manifest.MANIFEST_BASENAME)))

    def test_read_missing_returns_none(self):
        self.assertIsNone(manifest.read(os.path.join(self.tmp, "nope")))

    def test_write_rejects_missing_or_short_snapshot_id_before_touching_repo(self):
        for value in (None, "abcd1234"):
            cfg = _sample_cfg()
            cfg["repo"] = os.path.join(self.tmp, "invalid-%s" % (value or "missing"))
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    manifest.write(cfg, value)
                self.assertFalse(os.path.exists(cfg["repo"]))


class CfgFromManifestTest(unittest.TestCase):
    def test_overrides_repo_and_key(self):
        man = _derive(_sample_cfg())
        cfg = manifest.cfg_from_manifest(man, "/mnt/other/xibo", "/root/xibo.key")
        self.assertEqual(cfg["repo"], "/mnt/other/xibo")
        self.assertEqual(cfg["key_file"], "/root/xibo.key")
        self.assertIsNone(cfg["mount_check"])
        self.assertIsNone(cfg["backend_env_file"])
        self.assertEqual(cfg["stack_path"], "/opt/xibo")
        self.assertEqual(cfg["db_services"][0]["service"], "db")
        self.assertEqual(cfg["_manifest_snapshot_id"], _SNAPSHOT_ID)
        self.assertIsNone(cfg["_manifest_snapshot_id_error"])

    def test_name_override(self):
        man = _derive(_sample_cfg())
        cfg = manifest.cfg_from_manifest(man, "/mnt/x", "/k", name="xibo-test")
        self.assertEqual(cfg["name"], "xibo-test")

    def test_sets_empty_hooks_not_allowed(self):
        man = _derive(_sample_cfg())
        cfg = manifest.cfg_from_manifest(man, "/mnt/x", "/k")
        self.assertEqual(cfg["hooks"], {"pre_backup": [], "post_backup": [], "restore": []})
        self.assertFalse(cfg["hooks_allowed"])
        self.assertIsNone(cfg["hooks_fingerprint"])

    def test_roundtrips_shell_free_template_descriptor(self):
        cfg = _sample_cfg()
        tmpl = templates.load("gitlab")
        cfg["template"] = templates.provenance(tmpl, source="builtin")
        cfg["hooks"] = templates.to_hooks(tmpl)
        man = _derive(cfg)

        restored = manifest.cfg_from_manifest(man, "/mnt/gitlab", "/key")

        self.assertEqual(restored["template"], man["template"])
        self.assertIsNone(restored["_template_descriptor_error"])
        self.assertEqual(restored["hooks"], {
            "pre_backup": [], "post_backup": [], "restore": [],
        })

    def test_roundtrips_raw_external_bind_descriptors_for_target_resolver(self):
        cfg = _sample_cfg()
        cfg["extra_backup_paths"] = ["/srv/source"]
        descriptors = [{
            "service": "web", "target": "/data", "source": "/srv/source",
        }]
        man = _derive(cfg, external_bind_descriptors=descriptors)

        restored = manifest.cfg_from_manifest(man, "/mnt/x", "/key")

        self.assertEqual(restored["external_bind_descriptors"], descriptors)

        # Reading is deliberately non-executing and leaves validation to the
        # target Compose resolver, including rejection of unknown fields.
        man["external_bind_descriptors"][0]["command"] = "PWN_SENTINEL"
        restored = manifest.cfg_from_manifest(man, "/mnt/x", "/key")
        self.assertEqual(
            restored["external_bind_descriptors"][0]["command"], "PWN_SENTINEL"
        )

    def test_rejects_template_descriptor_with_injected_shell_field(self):
        man = _derive(_sample_cfg())
        man["template"] = {
            "name": "gitlab", "version": "1", "source": "builtin",
            "hooks_fingerprint": "sha256-v1:" + "0" * 64,
            "hooks_present": True,
            "hooks": [{"cmd": "PWN_SENTINEL"}],
        }

        restored = manifest.cfg_from_manifest(man, "/mnt/gitlab", "/key")

        self.assertIsNone(restored["template"])
        self.assertIn("unknown field", restored["_template_descriptor_error"])
        self.assertNotIn("PWN_SENTINEL", json.dumps(restored["hooks"]))

    def test_disagreeing_hook_markers_fail_template_resolution_closed(self):
        cfg = _sample_cfg()
        tmpl = templates.load("gitlab")
        cfg["template"] = templates.provenance(tmpl, source="builtin")
        cfg["hooks"] = templates.to_hooks(tmpl)
        man = _derive(cfg)
        man["hooks_present"] = False

        restored = manifest.cfg_from_manifest(man, "/mnt/gitlab", "/key")

        self.assertIsNone(restored["template"])
        self.assertTrue(restored["hooks_present"])
        self.assertIn("disagree", restored["_template_descriptor_error"])

    def test_legacy_gitlab_manifest_marks_custom_restore_required(self):
        cfg = _sample_cfg()
        cfg.update({
            # Template users can choose an arbitrary stack/config name. The v1-v3
            # compatibility check therefore identifies the shipped GitLab layout,
            # not this label.
            "name": "ils-gitlab-prod",
            "db_autodetect": False,
            "db_services": [],
            "exclude_patterns": ["gitlab/data/gitaly", "gitlab/data/postgresql"],
        })
        man = _derive(cfg)
        man.pop("custom_restore_required")  # simulate schema v3

        restored = manifest.cfg_from_manifest(man, "/mnt/gitlab", "/key")

        self.assertTrue(restored["custom_restore_required"])

    def test_one_gitlab_like_exclude_does_not_trigger_legacy_marker(self):
        man = _derive(_sample_cfg())
        man.pop("custom_restore_required")  # simulate schema v3
        man["db_autodetect"] = False
        man["exclude_patterns"] = ["gitlab/data/postgresql"]

        restored = manifest.cfg_from_manifest(man, "/mnt/other", "/key")

        self.assertFalse(restored["custom_restore_required"])

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
        self.assertIsNone(cfg["_manifest_snapshot_id"])
        self.assertIsNone(cfg["_manifest_snapshot_id_error"])

    def test_pre_v5_manifest_ignores_unrecognized_snapshot_binding(self):
        man = _derive(_sample_cfg())
        man["manifest_schema_version"] = 4
        man["snapshot_id"] = "not-a-restic-id"

        cfg = manifest.cfg_from_manifest(man, "/mnt/x", "/k")

        self.assertIsNone(cfg["_manifest_snapshot_id"])
        self.assertIsNone(cfg["_manifest_snapshot_id_error"])

    def test_v5_missing_or_invalid_snapshot_binding_fails_closed(self):
        for value in (None, "abcd1234", "G" * 64):
            man = _derive(_sample_cfg())
            if value is None:
                man.pop("snapshot_id")
            else:
                man["snapshot_id"] = value

            restored = manifest.cfg_from_manifest(man, "/mnt/x", "/k")

            with self.subTest(value=value):
                self.assertIsNone(restored["_manifest_snapshot_id"])
                self.assertIn("full 64-character", restored["_manifest_snapshot_id_error"])


class FindManifestsTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        self.base = tempfile.mkdtemp()

    def _make_repo(self, name):
        cfg = _sample_cfg()
        cfg["name"] = name
        cfg["repo"] = os.path.join(self.base, name)
        os.makedirs(cfg["repo"])
        _write(cfg)
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
