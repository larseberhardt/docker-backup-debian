from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, hooks, restic, templates, util
from docker_backup.commands import create


class MinioColdTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp  # empty override directory

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("minio-cold")  # validates; raises on error
        self.assertEqual(tmpl["name"], "minio-cold")
        self.assertFalse(tmpl["db_autodetect"])

    def test_hooks_stop_then_start(self):
        hk = templates.to_hooks(templates.load("minio-cold"))
        # pre: stop the container, abort if that fails (no snapshot while running).
        self.assertEqual(len(hk["pre_backup"]), 1)
        self.assertIn("stop minio", hk["pre_backup"][0]["cmd"])
        self.assertEqual(hk["pre_backup"][0]["on_failure"], "abort")
        self.assertEqual(hk["pre_backup"][0]["cwd"], "stack")
        # post: start the container again; runs ALWAYS in the finally, on_failure=warn
        # never masks a real backup error.
        self.assertEqual(len(hk["post_backup"]), 1)
        self.assertIn("start minio", hk["post_backup"][0]["cmd"])
        self.assertEqual(hk["post_backup"][0]["on_failure"], "warn")
        self.assertEqual(hk["restore"], [])

    def test_cold_variant_does_not_shadow_autodetect(self):
        # minio-cold deliberately carries NO match -> autodetect still suggests 'minio'
        # (without downtime); the cold variant is an explicit opt-in.
        cj = {"services": {"s3": {"image": "quay.io/minio/minio:latest"}}}
        self.assertEqual(templates.detect_template(cj), "minio")


class MinioColdExcludeInvariantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_critical_metadata_not_excluded(self):
        tmpl = templates.load("minio-cold")
        excludes = restic.resolve_excludes("/opt/s3", [], tmpl["exclude_patterns"])
        for keep in ("/opt/s3/minio-data",
                     "/opt/s3/minio-data/.minio.sys/format.json",
                     "/opt/s3/minio-data/.minio.sys/config",
                     "/opt/s3/minio-data/.minio.sys/buckets"):
            for e in excludes:
                self.assertNotEqual(e, keep)
                self.assertFalse(keep.startswith(e.rstrip("/") + "/"),
                                 "critical path %r falls under exclude %r" % (keep, e))


class MinioColdCreateFlowTest(unittest.TestCase):
    """The --allow-hooks gate flow must work cleanly for the cold variant."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = os.path.join(self.tmp, "etc")
        self.stack = os.path.join(self.tmp, "s3")
        os.makedirs(self.stack)
        util.set_dry_run(False)
        self.patches = [
            mock.patch.object(create.util, "require_root"),
            mock.patch.object(create.compose, "find_compose_file",
                              return_value=os.path.join(self.stack, "docker-compose.yml")),
            mock.patch.object(create.compose, "config_json", return_value={"name": "s3"}),
            mock.patch.object(create.compose, "collect_volume_backup_plan", return_value=([], [])),
            mock.patch.object(create.compose, "find_env_files", return_value=[]),
            mock.patch.object(create.systemd_units, "validate_oncalendar", return_value=True),
            mock.patch.object(create, "_install_timer"),
            mock.patch.object(create.keys, "ensure_key",
                              return_value=os.path.join(self.tmp, "k.key")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def _create(self, allow):
        tmpl = templates.load("minio-cold")
        return create.create_one(
            self.stack, target_base="/mnt/backups", schedule_input=tmpl.get("schedule"),
            offsite=None, name="minio", force=True, interactive=False,
            db_autodetect=tmpl.get("db_autodetect", True),
            exclude_patterns=list(tmpl.get("exclude_patterns") or []),
            allow_hooks=allow,
            hooks_override=templates.to_hooks(tmpl),
            retention_override=tmpl.get("retention"),
            template=templates.provenance(tmpl),
        )

    def test_with_allow_hooks_produces_approved_config(self):
        self.assertEqual(self._create(True), 0)
        cfg = config.load("minio")
        self.assertFalse(cfg["db_autodetect"])
        self.assertTrue(cfg["hooks_allowed"])
        self.assertTrue(cfg["hooks_fingerprint"])
        self.assertTrue(cfg["hooks"]["pre_backup"])
        self.assertTrue(cfg["hooks"]["post_backup"])
        hooks.ensure_allowed(cfg)  # fingerprint matches -> would not block

    def test_without_allow_hooks_is_locked(self):
        self.assertEqual(self._create(False), 0)
        cfg = config.load("minio")
        self.assertFalse(cfg["hooks_allowed"])
        self.assertIsNone(cfg["hooks_fingerprint"])
        with self.assertRaises(util.CommandError):
            hooks.ensure_allowed(cfg)  # hooks stored but locked -> hard abort


if __name__ == "__main__":
    unittest.main()
