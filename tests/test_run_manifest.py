from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util
from docker_backup.commands import run as run_cmd


_SNAPSHOT_ID = "0123456789abcdef" * 4


def _cfg(name="xibo"):
    return {
        "schema_version": 1,
        "name": name,
        "stack_path": "/opt/%s" % name,
        "compose_file": "/opt/%s/docker-compose.yml" % name,
        "project_name": name,
        "key_file": "/etc/docker-backup/keys/%s.key" % name,
        "repo": "/mnt/backups/%s" % name,
        "offsite": None,
        "mount_check": None,
        "db_services": [],
        "named_volumes": [],
        "exclude_paths": [],
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
    }


class RunWritesManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        # DRY_RUN skips staging FS operations; restic is mocked.
        util.set_dry_run(True)
        self._patches = [
            mock.patch.object(run_cmd, "restic"),
            mock.patch.object(
                run_cmd.compose, "config_json",
                return_value={"name": "xibo", "services": {}, "volumes": {}},
            ),
        ]
        for p in self._patches:
            p.start()
        self.manifest_write_patch = mock.patch.object(run_cmd.manifest, "write")
        self.manifest_write = self.manifest_write_patch.start()
        run_cmd.restic.backup.return_value = _SNAPSHOT_ID
        run_cmd.restic.snapshot_by_id.return_value = {
            "id": _SNAPSHOT_ID, "short_id": "01234567", "time": "t",
        }

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.manifest_write_patch.stop()
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        util.set_dry_run(False)

    def test_manifest_written_after_backup(self):
        cfg = _cfg()
        run_cmd._do_run(cfg)
        self.manifest_write.assert_called_once_with(cfg, _SNAPSHOT_ID, [])
        # Order: first restic.backup, then manifest.write.
        self.assertTrue(run_cmd.restic.backup.called)

    def test_created_snapshot_is_verified_after_prune_before_manifest(self):
        events = []
        def backup(*args, **kwargs):
            events.append("backup")
            return _SNAPSHOT_ID

        run_cmd.restic.backup.side_effect = backup
        run_cmd.restic.forget_prune.side_effect = lambda *a, **k: events.append("prune")

        def exact(*args, **kwargs):
            events.append("verify")
            return {"id": _SNAPSHOT_ID, "short_id": "01234567", "time": "t"}

        run_cmd.restic.snapshot_by_id.side_effect = exact
        self.manifest_write.side_effect = lambda *a, **k: events.append("manifest")

        run_cmd._do_run(_cfg())

        self.assertEqual(events, ["backup", "prune", "verify", "manifest"])
        run_cmd.restic.snapshot_by_id.assert_called_once_with(
            "/mnt/backups/xibo", "/etc/docker-backup/keys/xibo.key", _SNAPSHOT_ID,
        )
        run_cmd.restic.last_snapshot.assert_not_called()

    def test_missing_or_short_full_id_warns_and_skips_manifest(self):
        for snapshot_id in (None, "abcd1234", "G" * 64):
            run_cmd.restic.reset_mock()
            self.manifest_write.reset_mock()
            run_cmd.restic.backup.return_value = snapshot_id

            with self.subTest(snapshot_id=snapshot_id), \
                    mock.patch.object(run_cmd.util, "warn") as warn:
                summary = run_cmd._do_run(_cfg())
                self.assertIn("Repo:", summary)
                self.assertTrue(warn.called)
                self.assertIn("Manifest skipped", warn.call_args.args[0])
                self.manifest_write.assert_not_called()
                run_cmd.restic.snapshot_by_id.assert_not_called()

    def test_exact_snapshot_removed_by_retention_skips_manifest(self):
        run_cmd.restic.snapshot_by_id.return_value = None

        with mock.patch.object(run_cmd.util, "warn") as warn:
            summary = run_cmd._do_run(_cfg())

        self.assertNotIn("Snapshot:", summary)
        self.assertIn("no longer exists after retention", warn.call_args.args[0])
        self.manifest_write.assert_not_called()

    def test_external_binds_are_described_before_backup_and_written(self):
        cfg = _cfg()
        cfg["extra_backup_paths"] = ["/srv/source"]
        cj = {
            "services": {"web": {"volumes": [{
                "type": "bind", "source": "/srv/source", "target": "/data",
            }]}},
        }
        descriptors = [{
            "service": "web", "target": "/data", "source": "/srv/source",
        }]

        with mock.patch.object(run_cmd.compose, "config_json", return_value=cj) as load, \
                mock.patch.object(
                    run_cmd.compose, "describe_selected_external_binds",
                    return_value=descriptors,
                ) as describe:
            run_cmd._do_run(cfg)

        load.assert_called_once_with(cfg["compose_file"], cfg["stack_path"], cfg["project_name"])
        describe.assert_called_once_with(cj, ["/srv/source"])
        run_cmd.restic.backup.assert_called_once()
        self.assertEqual(run_cmd.restic.backup.call_args.args[2], ["/opt/xibo", "/srv/source"])
        self.manifest_write.assert_called_once_with(cfg, _SNAPSHOT_ID, descriptors)

    def test_descriptor_failure_aborts_before_backup_or_manifest(self):
        cfg = _cfg()
        cfg["extra_backup_paths"] = ["/srv/source"]
        error = util.CommandError(["compose"], 2, "selected bind is missing")

        with mock.patch.object(run_cmd.compose, "config_json", return_value={"services": {}}), \
                mock.patch.object(
                    run_cmd.compose, "describe_selected_external_binds", side_effect=error,
                ):
            with self.assertRaises(util.CommandError):
                run_cmd._do_run(cfg)

        run_cmd.restic.backup.assert_not_called()
        self.manifest_write.assert_not_called()

    def test_write_boundary_validation_keeps_completed_backup_successful(self):
        self.manifest_write.side_effect = ValueError("descriptor mismatch")

        with mock.patch.object(run_cmd.util, "warn") as warn:
            summary = run_cmd._do_run(_cfg())

        self.assertIn("Snapshot: 01234567", summary)
        self.assertIn("Manifest skipped after backup", warn.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
