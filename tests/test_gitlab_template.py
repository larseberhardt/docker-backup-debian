from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, hooks, restic, templates, util
from docker_backup.commands import create


class GitlabTemplateCreateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = os.path.join(self.tmp, "etc")
        self.stack = os.path.join(self.tmp, "gitlab")
        os.makedirs(self.stack)
        util.set_dry_run(False)
        self.patches = [
            mock.patch.object(create.util, "require_root"),
            mock.patch.object(create.compose, "find_compose_file",
                              return_value=os.path.join(self.stack, "docker-compose.yml")),
            mock.patch.object(
                create.compose,
                "config_json",
                return_value={"name": "gitlab", "services": {"gitlab": {}}},
            ),
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
        tmpl = templates.load("gitlab")
        return create.create_one(
            self.stack, target_base="/mnt/backups", schedule_input=tmpl.get("schedule"),
            offsite=None, name="gitlab", force=True, interactive=False,
            db_autodetect=tmpl.get("db_autodetect", True),
            exclude_patterns=list(tmpl.get("exclude_patterns") or []),
            allow_hooks=allow,
            hooks_override=templates.to_hooks(tmpl),
            retention_override=tmpl.get("retention"),
            restore_services=tmpl.get("restore_services"),
            template=templates.provenance(tmpl),
        )

    def test_with_allow_hooks_produces_approved_config(self):
        self.assertEqual(self._create(True), 0)
        cfg = config.load("gitlab")
        self.assertFalse(cfg["db_autodetect"])
        self.assertEqual(cfg["db_services"], [])
        self.assertTrue(cfg["hooks_allowed"])
        self.assertTrue(cfg["hooks_fingerprint"])
        self.assertEqual(cfg["retention"]["keep_within"], "30d")
        self.assertEqual(cfg["retention"]["weekly"], 0)
        self.assertEqual(cfg["schedule"]["oncalendar"], "*-*-* 04:00:00")
        self.assertEqual(cfg["template"]["name"], "gitlab")
        self.assertTrue(cfg["hooks"]["pre_backup"])
        self.assertTrue(cfg["hooks"]["post_backup"])
        self.assertTrue(cfg["hooks"]["restore"])
        self.assertEqual(cfg["restore_services"], ["gitlab"])
        # Fingerprint matches the stored commands -> run would not block.
        hooks.ensure_allowed(cfg)

    def test_without_allow_hooks_is_locked(self):
        self.assertEqual(self._create(False), 0)
        cfg = config.load("gitlab")
        self.assertFalse(cfg["hooks_allowed"])
        self.assertIsNone(cfg["hooks_fingerprint"])
        # Hooks are stored but locked -> the run aborts hard.
        with self.assertRaises(util.CommandError):
            hooks.ensure_allowed(cfg)


class GitlabExcludeInvariantTest(unittest.TestCase):
    """The .tar produced by 'gitlab-backup create' MUST NOT be excluded."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_backups_dir_not_excluded(self):
        tmpl = templates.load("gitlab")
        excludes = restic.resolve_excludes("/opt/gitlab", [], tmpl["exclude_patterns"])
        backups = "/opt/gitlab/gitlab/data/backups"
        archive = backups + "/1234567890_2026_07_16_18.9.0-ee_gitlab_backup.tar"
        for e in excludes:
            self.assertNotEqual(e, backups)
            self.assertFalse(backups.startswith(e.rstrip("/") + "/"),
                             "backups path falls under exclude %r" % e)
            self.assertFalse(archive.startswith(e.rstrip("/") + "/"),
                             "generated GitLab archive falls under exclude %r" % e)

    def test_live_dirs_are_excluded(self):
        tmpl = templates.load("gitlab")
        excludes = restic.resolve_excludes("/opt/gitlab", [], tmpl["exclude_patterns"])
        for live in ("/opt/gitlab/gitlab/data/postgresql",
                     "/opt/gitlab/gitlab/data/gitaly",
                     "/opt/gitlab/gitlab/data/git-data",
                     "/opt/gitlab/gitlab/logs",
                     "/opt/gitlab/gitlab/data/redis"):
            self.assertIn(live, excludes)

    def test_git_data_exclude_is_appended_for_stable_template_order(self):
        tmpl = templates.load("gitlab")
        self.assertEqual(tmpl["exclude_patterns"][-1], "gitlab/data/git-data")


class GitlabRestoreHookInvariantTest(unittest.TestCase):
    def test_restore_does_not_restart_outbound_services(self):
        command = templates.load("gitlab")["hooks"]["restore"][0]["cmd"]
        self.assertIn("gitlab-backup restore", command)
        self.assertNotIn("gitlab-ctl start", command)


if __name__ == "__main__":
    unittest.main()
