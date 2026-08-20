from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import hooks, templates, util


def _valid():
    return {
        "template_schema_version": 1,
        "name": "demo",
        "description": "x",
        "db_autodetect": False,
        "exclude_patterns": ["data/logs", "tmp"],
        "schedule": "daily 02:00",
        "retention": {"daily": 7, "weekly": 0, "monthly": 0, "keep_within": "30d"},
        "match": {"image_tokens": ["demo/app"]},
        "hooks": {
            "pre_backup": [{"cmd": "echo pre", "on_failure": "abort"}],
            "post_backup": [{"cmd": "echo post", "on_failure": "warn"}],
        },
    }


class ValidateTest(unittest.TestCase):
    def test_valid_passes(self):
        templates.validate(_valid())

    def test_bad_schema_version(self):
        t = _valid(); t["template_schema_version"] = 99
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_unknown_top_level_key(self):
        t = _valid(); t["surprise"] = 1
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_hook_without_cmd(self):
        t = _valid(); t["hooks"]["pre_backup"] = [{"on_failure": "abort"}]
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_unknown_hook_phase(self):
        t = _valid(); t["hooks"]["during_backup"] = [{"cmd": "x"}]
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_bad_on_failure(self):
        t = _valid(); t["hooks"]["pre_backup"][0]["on_failure"] = "explode"
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_traversal_exclude_rejected(self):
        t = _valid(); t["exclude_patterns"] = ["../../etc"]
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_missing_name_rejected(self):
        t = _valid(); del t["name"]
        with self.assertRaises(util.CommandError):
            templates.validate(t)

    def test_restore_services_requires_nonempty_valid_unique_list_and_restore_hook(self):
        for value in (None, [], "gitlab", ["bad/service"], ["gitlab", "gitlab"]):
            t = _valid()
            t["restore_services"] = value
            with self.subTest(value=value), self.assertRaises(util.CommandError):
                templates.validate(t)

        t = _valid()
        t["hooks"]["restore"] = [{"cmd": "docker exec gitlab true"}]
        t["restore_services"] = ["gitlab"]
        self.assertEqual(templates.validate(t)["restore_services"], ["gitlab"])


class ToHooksTest(unittest.TestCase):
    def test_normalizes_phase_defaults(self):
        hk = templates.to_hooks(_valid())
        self.assertEqual(hk["pre_backup"][0]["cmd"], "echo pre")
        self.assertEqual(hk["pre_backup"][0]["on_failure"], "abort")
        self.assertEqual(hk["post_backup"][0]["on_failure"], "warn")
        self.assertEqual(hk["restore"], [])
        # phase defaults for timeout/cwd filled in
        self.assertEqual(hk["pre_backup"][0]["cwd"], "stack")
        self.assertTrue(hk["pre_backup"][0]["timeout"] > 0)

    def test_hook_definition_fingerprint_changes_with_template_hook(self):
        a = _valid()
        b = copy.deepcopy(a)
        b["hooks"]["pre_backup"][0]["cwd"] = "/tmp"
        self.assertNotEqual(
            templates.hook_definition_fingerprint(a),
            templates.hook_definition_fingerprint(b),
        )

    def test_unscoped_fingerprint_remains_v1_and_scope_uses_v2(self):
        t = _valid()
        old = templates.hook_definition_fingerprint(t)
        self.assertEqual(
            old,
            hooks.compute_definition_fingerprint(templates.to_hooks(t)),
        )
        self.assertTrue(old.startswith("sha256-v1:"))

        t["hooks"]["restore"] = [{"cmd": "docker exec gitlab true"}]
        t["restore_services"] = ["gitlab"]
        scoped = templates.hook_definition_fingerprint(t)
        self.assertTrue(scoped.startswith("sha256-v2:"))

        t["restore_services"] = ["gitlab-api"]
        self.assertNotEqual(scoped, templates.hook_definition_fingerprint(t))


class ShippedTemplatesTest(unittest.TestCase):
    """Every shipped template must be valid (otherwise CI fails)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp  # empty override directory

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_all_builtins_validate(self):
        names = templates.list_templates()
        self.assertIn("gitlab", names)
        self.assertIn("supabase", names)
        for n in names:
            templates.load(n)  # validates; raises on error

    def test_gitlab_restore_waits_for_first_boot_and_never_prompts(self):
        gitlab = templates.load("gitlab")
        self.assertEqual(gitlab["template_schema_version"], 1)
        restore = gitlab["hooks"]["restore"]
        self.assertEqual(len(restore), 1)
        command = restore[0]["cmd"]
        self.assertIn("docker inspect -f '{{.State.Running}}' gitlab", command)
        self.assertIn("[c]inc-client|[c]hef-client|[g]itlab-ctl reconfigure", command)
        self.assertIn("docker logs --tail 300 gitlab", command)
        self.assertIn('while [ "$reconfigure_idle" -lt 3 ]', command)
        self.assertIn(
            "/opt/gitlab/bin/gitlab-healthcheck --fail --max-time 10", command,
        )
        self.assertIn("gitlab-rake gitlab:env:info", command)
        self.assertIn('while [ "$ready_passes" -lt 2 ]', command)
        self.assertIn('"$reconfigure_attempt" -ge 90', command)
        self.assertIn('"$ready_attempt" -ge 90', command)
        self.assertIn("GITLAB_ASSUME_YES=1", command)
        self.assertIn('BACKUP="$backup"', command)
        self.assertIn("Expected exactly one regular GitLab backup archive", command)
        self.assertLess(command.index("reconfigure_idle"),
                        command.index("gitlab-healthcheck"))
        self.assertLess(command.index("gitlab-healthcheck"),
                        command.index("gitlab-ctl stop puma"))
        self.assertLess(command.index("gitlab-ctl stop puma"),
                        command.index("gitlab-backup restore"))
        # Keep this equal to hooks.make_hook's restore default: an existing
        # config aligned through `set --restore-cmd` must have the same full
        # definition fingerprint as the builtin template.
        self.assertEqual(restore[0]["timeout"], 7200)

    def test_gitlab_restore_refuses_ambiguous_archives_before_docker(self):
        command = templates.load("gitlab")["hooks"]["restore"][0]["cmd"]
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = os.path.join(tmp, "gitlab", "data", "backups")
            os.makedirs(backup_dir)
            for backup_id in ("1_2026_01_01_18.0.0", "2_2026_01_02_18.0.0"):
                with open(os.path.join(
                        backup_dir, backup_id + "_gitlab_backup.tar"), "wb"):
                    pass
            result = subprocess.run(
                ["sh", "-c", command], cwd=tmp, capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected exactly one", result.stderr)
        self.assertNotIn("docker:", result.stderr)


class DetectTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_detects_gitlab_by_image(self):
        cj = {"services": {"gitlab": {"image": "gitlab/gitlab-ee:17.0.0-ee.0"}}}
        self.assertEqual(templates.detect_template(cj), "gitlab")

    def test_no_match_returns_none(self):
        cj = {"services": {"web": {"image": "nginx:latest"}}}
        self.assertIsNone(templates.detect_template(cj))


class OverrideDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_operator_override_wins(self):
        os.makedirs(templates.override_dir(), exist_ok=True)
        t = copy.deepcopy(_valid())
        t["name"] = "gitlab"
        t["description"] = "OPERATOR OVERRIDE"
        with open(os.path.join(templates.override_dir(), "gitlab.json"), "w") as f:
            import json
            json.dump(t, f)
        loaded = templates.load("gitlab")
        self.assertEqual(loaded["description"], "OPERATOR OVERRIDE")

    def test_exact_builtin_loader_bypasses_operator_override(self):
        os.makedirs(templates.override_dir(), exist_ok=True)
        t = copy.deepcopy(_valid())
        t["name"] = "gitlab"
        t["description"] = "MALICIOUS OVERRIDE"
        t["hooks"]["pre_backup"][0]["cmd"] = "PWN_SENTINEL"
        with open(os.path.join(templates.override_dir(), "gitlab.json"), "w") as f:
            import json
            json.dump(t, f)

        loaded = templates.load_exact("gitlab", "builtin")

        self.assertNotEqual(loaded["description"], "MALICIOUS OVERRIDE")
        self.assertNotIn("PWN_SENTINEL", str(loaded))

    def test_provenance_records_operator_source(self):
        os.makedirs(templates.override_dir(), exist_ok=True)
        t = copy.deepcopy(_valid())
        t["name"] = "gitlab"
        with open(os.path.join(templates.override_dir(), "gitlab.json"), "w") as f:
            import json
            json.dump(t, f)

        loaded, source = templates.load_with_source("gitlab")

        self.assertEqual(source, "operator")
        self.assertEqual(templates.provenance(loaded, source=source)["source"], "operator")

    def test_exact_loader_rejects_unknown_source(self):
        with self.assertRaises(util.CommandError):
            templates.load_exact("gitlab", "backup-share")

    def test_exact_operator_loader_rejects_symlink_and_writable_file(self):
        os.makedirs(templates.override_dir(), exist_ok=True)
        path = os.path.join(templates.override_dir(), "gitlab.json")
        os.symlink(os.path.join(templates.builtin_dir(), "gitlab.json"), path)
        with self.assertRaises(util.CommandError):
            templates.load_exact("gitlab", "operator")
        os.unlink(path)

        t = copy.deepcopy(_valid())
        t["name"] = "gitlab"
        with open(path, "w") as f:
            import json
            json.dump(t, f)
        os.chmod(path, 0o666)
        with self.assertRaises(util.CommandError):
            templates.load_exact("gitlab", "operator")


if __name__ == "__main__":
    unittest.main()
