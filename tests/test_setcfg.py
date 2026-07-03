from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, util
from docker_backup.commands import setcfg


def _args(name="xibo", **kw):
    base = dict(name=name, schedule=None, retention=None, offsite=None,
                target=None, ack_dangerous=False)
    base.update(kw)
    return argparse.Namespace(**base)


class ParseRetentionTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(setcfg._parse_retention("7/4/6"),
                         {"daily": 7, "weekly": 4, "monthly": 6})

    def test_rejects_wrong_count(self):
        with self.assertRaises(util.CommandError):
            setcfg._parse_retention("7/4")

    def test_rejects_non_int(self):
        with self.assertRaises(util.CommandError):
            setcfg._parse_retention("a/b/c")

    def test_rejects_negative(self):
        with self.assertRaises(util.CommandError):
            setcfg._parse_retention("7/-1/6")


class SetCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(False)
        config.save({
            "schema_version": config.SCHEMA_VERSION, "name": "xibo",
            "repo": "/mnt/backups/xibo", "offsite": None,
            "schedule": {"input": "daily 03:00", "oncalendar": "*-*-* 03:00:00",
                         "randomized_delay_sec": 300},
            "retention": dict(config.DEFAULT_RETENTION),
        })
        self._patches = [
            mock.patch.object(setcfg.util, "require_root"),
            mock.patch.object(setcfg.systemd_units, "validate_oncalendar", return_value=True),
            mock.patch.object(setcfg.systemd_units, "write_schedule_dropin"),
            mock.patch.object(setcfg.systemd_units, "daemon_reload"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_schedule_updates_oncalendar(self):
        rc = setcfg.cmd_set(_args(schedule="weekly Mon 04:00"))
        self.assertEqual(rc, 0)
        cfg = config.load("xibo")
        self.assertEqual(cfg["schedule"]["input"], "weekly Mon 04:00")
        self.assertEqual(cfg["schedule"]["oncalendar"], "Mon *-*-* 04:00:00")
        self.assertTrue(setcfg.systemd_units.write_schedule_dropin.called)

    def test_retention_updates(self):
        rc = setcfg.cmd_set(_args(retention="10/8/12"))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["retention"],
                         {"daily": 10, "weekly": 8, "monthly": 12, "keep_within": None})

    def test_offsite_set_and_clear(self):
        setcfg.cmd_set(_args(offsite="s3:bucket"))
        self.assertEqual(config.load("xibo")["offsite"], "s3:bucket/xibo")
        setcfg.cmd_set(_args(offsite=""))
        self.assertIsNone(config.load("xibo")["offsite"])

    def test_target_without_ack_refused(self):
        rc = setcfg.cmd_set(_args(target="/mnt/other"))
        self.assertEqual(rc, 2)
        self.assertEqual(config.load("xibo")["repo"], "/mnt/backups/xibo")  # unchanged

    def test_target_with_ack(self):
        rc = setcfg.cmd_set(_args(target="/mnt/other", ack_dangerous=True))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["repo"], "/mnt/other/xibo")

    def test_nothing_to_change(self):
        self.assertEqual(setcfg.cmd_set(_args()), 0)

    def test_add_exclude_patterns(self):
        rc = setcfg.cmd_set(_args(exclude=["gitlab/logs", "gitlab/data/postgresql"]))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["exclude_patterns"],
                         ["gitlab/logs", "gitlab/data/postgresql"])

    def test_exclude_dedup(self):
        setcfg.cmd_set(_args(exclude=["gitlab/logs"]))
        setcfg.cmd_set(_args(exclude=["gitlab/logs", "tmp"]))
        self.assertEqual(config.load("xibo")["exclude_patterns"], ["gitlab/logs", "tmp"])

    def test_exclude_clear(self):
        setcfg.cmd_set(_args(exclude=["gitlab/logs"]))
        setcfg.cmd_set(_args(exclude_clear=True))
        self.assertEqual(config.load("xibo")["exclude_patterns"], [])

    def test_invalid_exclude_rejected(self):
        with self.assertRaises(util.CommandError):
            setcfg.cmd_set(_args(exclude=["../etc"]))

    def test_keep_within_set_and_clear(self):
        setcfg.cmd_set(_args(keep_within="30d"))
        self.assertEqual(config.load("xibo")["retention"]["keep_within"], "30d")
        setcfg.cmd_set(_args(no_keep_within=True))
        self.assertIsNone(config.load("xibo")["retention"]["keep_within"])

    def test_retention_preserves_keep_within(self):
        setcfg.cmd_set(_args(keep_within="14d"))
        setcfg.cmd_set(_args(retention="5/3/2"))
        ret = config.load("xibo")["retention"]
        self.assertEqual(ret["daily"], 5)
        self.assertEqual(ret["keep_within"], "14d")

    def test_offsite_retention_set(self):
        rc = setcfg.cmd_set(_args(offsite_retention="30/12/24"))
        self.assertEqual(rc, 0)
        self.assertEqual(config.load("xibo")["offsite_retention"],
                         {"daily": 30, "weekly": 12, "monthly": 24})

    def test_offsite_retention_invalid_rejected(self):
        with self.assertRaises(util.CommandError):
            setcfg.cmd_set(_args(offsite_retention="30/12"))

    def test_offsite_prune_toggle(self):
        setcfg.cmd_set(_args(offsite_prune=False))
        self.assertFalse(config.load("xibo")["offsite_prune"])
        setcfg.cmd_set(_args(offsite_prune=True))
        self.assertTrue(config.load("xibo")["offsite_prune"])


if __name__ == "__main__":
    unittest.main()

