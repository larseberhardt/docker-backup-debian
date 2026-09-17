"""``ls`` shows which backups were created from a template and which were not."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import ls as ls_cmd


def _base_cfg(name, **kw):
    cfg = {"schema_version": config.SCHEMA_VERSION, "name": name,
           "stack_path": "/opt/" + name, "repo": "/mnt/backups/" + name, "offsite": None,
           "schedule": {"oncalendar": "*-*-* 03:00:00"},
           "retention": dict(config.DEFAULT_RETENTION)}
    cfg.update(kw)
    return cfg


class TemplateLabelTest(unittest.TestCase):
    def test_no_template(self):
        self.assertEqual(ls_cmd.template_label(None), "-")
        self.assertEqual(ls_cmd.template_label({}), "-")
        self.assertEqual(ls_cmd.template_label("gitlab"), "-")

    def test_builtin_template_shows_its_name(self):
        self.assertEqual(
            ls_cmd.template_label({"name": "gitlab", "version": "1", "source": "builtin"}),
            "gitlab")

    def test_operator_override_is_marked(self):
        self.assertEqual(
            ls_cmd.template_label({"name": "gitlab", "version": "1", "source": "operator"}),
            "gitlab (operator)")


class LsTemplateColumnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        config.save(_base_cfg("gitlab", template={"name": "gitlab", "version": "1",
                                                   "source": "builtin"}))
        config.save(_base_cfg("plain"))

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _ls(self, **kw):
        args = argparse.Namespace(on_repo=None, snapshots=False, **kw)
        out = io.StringIO()
        with mock.patch.object(ls_cmd.systemd_units, "timer_active", return_value="active"), \
             redirect_stdout(out):
            rc = ls_cmd.cmd_ls(args)
        self.assertEqual(rc, 0)
        return {line.split()[0]: line.split() for line in out.getvalue().splitlines()
                if line.strip() and not line.startswith("-")}

    def test_template_column_per_backup(self):
        rows = self._ls()
        self.assertEqual(rows["NAME"][-1], "TEMPLATE")
        self.assertEqual(rows["gitlab"][-1], "gitlab")
        self.assertEqual(rows["plain"][-1], "-")

    def test_corrupt_config_keeps_the_column_count(self):
        with open(os.path.join(config.configs_dir(), "broken.json"), "w") as f:
            f.write("{ not json")
        rows = self._ls()
        self.assertEqual(len(rows["broken"]), 2)  # name + "(corrupt)", blanks collapse
        self.assertEqual(rows["broken"][1], "(corrupt)")

    def test_on_repo_view_shows_manifest_template(self):
        found = [
            ("/mnt/backups/gitlab", {"name": "gitlab", "stack_path": "/opt/gitlab",
                                     "db_services": [],
                                     "template": {"name": "gitlab", "version": "1",
                                                  "source": "operator"}}),
            ("/mnt/backups/plain", {"name": "plain", "stack_path": "/opt/plain",
                                    "db_services": [{"service": "db"}], "template": None}),
        ]
        out = io.StringIO()
        with mock.patch.object(ls_cmd.manifest, "find_manifests", return_value=found), \
             redirect_stdout(out):
            rc = ls_cmd.cmd_ls(argparse.Namespace(on_repo="/mnt/backups", snapshots=False))
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertIn("TEMPLATE", lines[0])
        gitlab = next(l for l in lines if l.startswith("gitlab"))
        plain = next(l for l in lines if l.startswith("plain"))
        self.assertEqual(gitlab.split()[-2:], ["gitlab", "(operator)"])
        self.assertEqual(plain.split()[-2:], ["db", "-"])


if __name__ == "__main__":
    unittest.main()
