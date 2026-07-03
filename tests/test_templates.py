from __future__ import annotations

import copy
import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import templates, util


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


if __name__ == "__main__":
    unittest.main()
