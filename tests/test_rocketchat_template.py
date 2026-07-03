from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, detect, hooks, templates, util
from docker_backup.commands import create

_MONGO_CJ = {
    "name": "rocketchat",
    "services": {
        "rocketchat": {"image": "rocketchat/rocket.chat:8.5.1"},
        "mongo": {
            "image": "mongo:8.0",
            "environment": {},
            "volumes": [{"type": "volume", "source": "mongodb_data", "target": "/data/db"}],
        },
    },
    "volumes": {"mongodb_data": None},
}


class RocketchatTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("rocketchat")
        self.assertEqual(tmpl["name"], "rocketchat")
        self.assertFalse(tmpl["db_autodetect"])  # MongoDB: no SQL detection
        # Since the built-in quiesce (fsyncLock) the template ships NO root hooks
        # anymore — no --allow-hooks friction, tighter lock scope.
        self.assertFalse(tmpl.get("hooks"))

    def test_detects_rocketchat_by_image(self):
        cj = {"services": {"rocketchat": {"image": "rocketchat/rocket.chat:8.5.1"},
                           "mongo": {"image": "mongo:8.0"}}}
        self.assertEqual(templates.detect_template(cj), "rocketchat")

    def test_mongo_is_not_autodetected_as_sql(self):
        cj = {"services": {"mongo": {"image": "mongo:8.0"}}}
        self.assertEqual(detect.find_db_services(cj), [])

    def test_mongo_is_covered_by_builtin_quiesce(self):
        found = detect.find_quiesce_services(_MONGO_CJ)
        self.assertEqual([(q["service"], q["engine"]) for q in found],
                         [("mongo", "mongo")])


class RocketchatCreateFlowTest(unittest.TestCase):
    """create with the template records the built-in quiesce, no hooks needed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = os.path.join(self.tmp, "etc")
        self.stack = os.path.join(self.tmp, "rocketchat")
        os.makedirs(self.stack)
        util.set_dry_run(False)
        self.patches = [
            mock.patch.object(create.util, "require_root"),
            mock.patch.object(create.compose, "find_compose_file",
                              return_value=os.path.join(self.stack, "docker-compose.yml")),
            mock.patch.object(create.compose, "config_json", return_value=_MONGO_CJ),
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

    def _create(self, no_quiesce=False):
        tmpl = templates.load("rocketchat")
        return create.create_one(
            self.stack, target_base="/mnt/backups", schedule_input=tmpl.get("schedule"),
            offsite=None, name="rocketchat", force=True, interactive=False,
            db_autodetect=tmpl.get("db_autodetect", True),
            no_quiesce=no_quiesce,
            exclude_patterns=list(tmpl.get("exclude_patterns") or []),
            hooks_override=templates.to_hooks(tmpl),
            retention_override=tmpl.get("retention"),
            template=templates.provenance(tmpl),
        )

    def test_create_records_quiesce_and_no_hooks(self):
        self.assertEqual(self._create(), 0)
        cfg = config.load("rocketchat")
        self.assertFalse(cfg["db_autodetect"])
        self.assertEqual(cfg["quiesce_services"], [{
            "service": "mongo", "engine": "mongo", "scope": "staging",
            "user_value": None, "user_env_key": None, "password_env_key": None,
        }])
        self.assertFalse(cfg["quiesce_disabled"])
        # no hooks → nothing to approve, the gate never blocks
        self.assertFalse(hooks.has_commands(cfg))
        hooks.ensure_allowed(cfg)

    def test_create_no_quiesce_optout(self):
        self.assertEqual(self._create(no_quiesce=True), 0)
        cfg = config.load("rocketchat")
        self.assertEqual(cfg["quiesce_services"], [])

    def test_mongo_named_volume_is_tarred(self):
        # The mongo data volume is NOT a SQL DB data dir → it stays in the tar plan.
        self.assertEqual(self._create(), 0)
        cfg = config.load("rocketchat")
        self.assertEqual([nv["key"] for nv in cfg["named_volumes"]], ["mongodb_data"])


if __name__ == "__main__":
    unittest.main()
