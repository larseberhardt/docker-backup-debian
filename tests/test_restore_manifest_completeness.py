"""Bind plaintext v5 restore plans to encrypted snapshot/Compose metadata."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import manifest, util
from docker_backup.commands import restore as restore_cmd


_SNAPSHOT_ID = "a" * 64


def _snapshot_cfg(**updates):
    cfg = {
        "name": "app-target",
        "repo": "/repo/app",
        "key_file": "/keys/app.key",
        "stack_path": "/opt/app",
        "compose_file": "/opt/app/docker-compose.yml",
        "extra_backup_paths": ["/srv/app-media"],
        "db_services": [],
        "named_volumes": [],
        "db_autodetect": True,
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        "_manifest_schema_version": manifest.MANIFEST_SCHEMA_VERSION,
        "_manifest_source_name": "app",
        "_manifest_snapshot_id": _SNAPSHOT_ID,
    }
    cfg.update(updates)
    return cfg


def _snapshot_metadata(paths=None, tags=None, snapshot_id=_SNAPSHOT_ID):
    return {
        "id": snapshot_id,
        "paths": paths if paths is not None else ["/opt/app", "/srv/app-media"],
        "tags": tags if tags is not None else ["docker-backup", "stack:app"],
    }


def _compose_model():
    return {
        "name": "app",
        "services": {
            "db": {
                "image": "postgres:16",
                "volumes": [{
                    "type": "volume",
                    "source": "db_data",
                    "target": "/var/lib/postgresql/data",
                }],
            },
            "app": {
                "image": "example/app:1",
                "volumes": [{
                    "type": "volume",
                    "source": "media",
                    "target": "/srv/media",
                }],
            },
        },
        "volumes": {"db_data": {}, "media": {}},
    }


def _db():
    return {"service": "db", "engine": "postgres"}


def _media_volume():
    return {
        "key": "media",
        "real_name": "app_media",
        "target": "/srv/media",
        "service": "app",
    }


class SnapshotMetadataAuthenticationTest(unittest.TestCase):
    def test_exact_snapshot_paths_and_tags_are_accepted(self):
        cfg = _snapshot_cfg()
        with mock.patch.object(
                restore_cmd.restic, "snapshot_by_id",
                return_value=_snapshot_metadata(),
        ) as lookup:
            restore_cmd._authenticate_manifest_snapshot(
                cfg, _SNAPSHOT_ID, ["/opt/app", "/srv/app-media"],
            )

        lookup.assert_called_once_with(cfg["repo"], cfg["key_file"], _SNAPSHOT_ID)

    def test_manifest_omitting_authenticated_external_root_is_rejected(self):
        cfg = _snapshot_cfg(extra_backup_paths=[])
        with mock.patch.object(
                restore_cmd.restic, "snapshot_by_id",
                return_value=_snapshot_metadata(),
        ), self.assertRaises(util.CommandError) as raised:
            restore_cmd._authenticate_manifest_snapshot(
                cfg, _SNAPSHOT_ID, ["/opt/app"],
            )

        self.assertIn("do not exactly match", raised.exception.stderr)

    def test_snapshot_omitting_manifest_external_root_is_rejected(self):
        cfg = _snapshot_cfg()
        with mock.patch.object(
                restore_cmd.restic, "snapshot_by_id",
                return_value=_snapshot_metadata(paths=["/opt/app"]),
        ), self.assertRaises(util.CommandError) as raised:
            restore_cmd._authenticate_manifest_snapshot(
                cfg, _SNAPSHOT_ID, ["/opt/app", "/srv/app-media"],
            )

        self.assertIn("do not exactly match", raised.exception.stderr)

    def test_wrong_snapshot_record_or_stack_tag_is_rejected(self):
        cfg = _snapshot_cfg()
        for metadata in (
            _snapshot_metadata(snapshot_id="b" * 64),
            _snapshot_metadata(tags=["docker-backup", "stack:other"]),
        ):
            with self.subTest(metadata=metadata), mock.patch.object(
                    restore_cmd.restic, "snapshot_by_id", return_value=metadata,
            ), self.assertRaises(util.CommandError):
                restore_cmd._authenticate_manifest_snapshot(
                    cfg, _SNAPSHOT_ID, ["/opt/app", "/srv/app-media"],
                )

    def test_run_aborts_before_restic_restore_or_target_reservation(self):
        cfg = _snapshot_cfg(extra_backup_paths=[])
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(os.path.realpath(tmp), "restore-target")
            with mock.patch.object(restore_cmd.runtime, "load_backend_env"), \
                    mock.patch.object(restore_cmd.restic, "restic_version", return_value=None), \
                    mock.patch.object(restore_cmd.restic, "repo_initialized", return_value=True), \
                    mock.patch.object(
                        restore_cmd.restic, "snapshot_by_id",
                        return_value=_snapshot_metadata(),
                    ), \
                    mock.patch.object(restore_cmd.restic, "restore") as restore, \
                    mock.patch.object(
                        restore_cmd, "_reserve_restore_directory",
                    ) as reserve:
                rc = restore_cmd._run_restore(
                    cfg, "app-target", dest, _SNAPSHOT_ID, force=False,
                )

        self.assertEqual(rc, 1)
        reserve.assert_not_called()
        restore.assert_not_called()


class ComposePlanAuthenticationTest(unittest.TestCase):
    def _cfg(self, **updates):
        cfg = {
            "_manifest_schema_version": manifest.MANIFEST_SCHEMA_VERSION,
            "db_autodetect": True,
            "db_services": [_db()],
            "named_volumes": [_media_volume()],
            "extra_backup_paths": [],
            "external_bind_descriptors": [],
        }
        cfg.update(updates)
        return cfg

    def test_exact_detected_db_and_non_db_volume_plan_is_accepted(self):
        restore_cmd._authenticate_manifest_compose_plan(
            self._cfg(), _compose_model(),
        )

    def test_omitted_detected_database_is_rejected(self):
        with self.assertRaises(util.CommandError) as raised:
            restore_cmd._authenticate_manifest_compose_plan(
                self._cfg(db_services=[]), _compose_model(),
            )
        self.assertIn("database services do not exactly match", raised.exception.stderr)

    def test_database_engine_substitution_is_rejected(self):
        with self.assertRaises(util.CommandError):
            restore_cmd._authenticate_manifest_compose_plan(
                self._cfg(db_services=[{"service": "db", "engine": "mysql"}]),
                _compose_model(),
            )

    def test_omitted_non_database_named_volume_is_rejected(self):
        with self.assertRaises(util.CommandError) as raised:
            restore_cmd._authenticate_manifest_compose_plan(
                self._cfg(named_volumes=[]), _compose_model(),
            )
        self.assertIn("named volumes do not exactly match", raised.exception.stderr)

    def test_added_or_incomplete_named_volume_is_rejected(self):
        extra = _media_volume()
        extra["key"] = "missing"
        extra["real_name"] = "app_missing"
        for configured in ([_media_volume(), extra], [{"key": "media"}]):
            with self.subTest(configured=configured), self.assertRaises(
                    (ValueError, util.CommandError)):
                restore_cmd._authenticate_manifest_compose_plan(
                    self._cfg(named_volumes=configured), _compose_model(),
                )

    def test_autodetect_disabled_treats_db_volume_as_archived_data(self):
        db_archive = {
            "key": "db_data",
            "real_name": "app_db_data",
            "target": "/var/lib/postgresql/data",
            "service": "db",
        }
        restore_cmd._authenticate_manifest_compose_plan(
            self._cfg(
                db_autodetect=False,
                db_services=[],
                named_volumes=[db_archive, _media_volume()],
            ),
            _compose_model(),
        )

    def test_swapped_external_descriptor_sources_are_rejected(self):
        model = {
            "name": "app",
            "services": {
                "web": {"image": "example/web", "volumes": [{
                    "type": "bind", "source": "/srv/a", "target": "/data",
                }]},
                "worker": {"image": "example/worker", "volumes": [{
                    "type": "bind", "source": "/srv/b", "target": "/work",
                }]},
            },
            "volumes": {},
        }
        correct = [
            {"service": "web", "target": "/data", "source": "/srv/a"},
            {"service": "worker", "target": "/work", "source": "/srv/b"},
        ]
        cfg = self._cfg(
            db_services=[], named_volumes=[],
            extra_backup_paths=["/srv/a", "/srv/b"],
            external_bind_descriptors=correct,
        )
        restore_cmd._authenticate_manifest_compose_plan(cfg, model)

        swapped = [dict(item) for item in correct]
        swapped[0]["source"], swapped[1]["source"] = (
            swapped[1]["source"], swapped[0]["source"],
        )
        cfg["external_bind_descriptors"] = swapped
        with self.assertRaises(util.CommandError) as raised:
            restore_cmd._authenticate_manifest_compose_plan(cfg, model)
        self.assertIn("external bind descriptors", raised.exception.stderr)

    def test_scratch_relative_external_source_is_rebased_to_snapshot_path(self):
        mirror = "/proc/123/fd/9"
        project = mirror + "/opt/app"
        protected_model = {
            "name": "app",
            "services": {"web": {"image": "example/web", "volumes": [{
                # A source written as ../../srv/data in Compose resolves below
                # the protected restic mirror during authentication.
                "type": "bind",
                "source": mirror + "/srv/data",
                "target": "/data",
            }]}},
            "volumes": {},
        }
        source_model = restore_cmd._rebase_scratch_paths(
            protected_model, project, mirror, "/opt/app", "/opt/app",
        )
        cfg = self._cfg(
            db_services=[], named_volumes=[],
            extra_backup_paths=["/srv/data"],
            external_bind_descriptors=[{
                "service": "web", "target": "/data", "source": "/srv/data",
            }],
        )

        restore_cmd._authenticate_manifest_compose_plan(cfg, source_model)


class EncryptedArtifactCompletenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.stack = os.path.join(self.tmp, "opt", "app")
        self.dumps = os.path.join(self.stack, ".docker-backup", "dumps")
        self.archives = os.path.join(self.stack, ".docker-backup", "volumes")
        os.makedirs(self.dumps)
        os.makedirs(self.archives)
        self.scratch_fd = os.open(
            self.tmp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )

    def tearDown(self):
        os.close(self.scratch_fd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _cfg(**updates):
        cfg = {
            "_manifest_schema_version": manifest.MANIFEST_SCHEMA_VERSION,
            "db_autodetect": True,
            "db_services": [_db()],
            "named_volumes": [_media_volume()],
            "extra_backup_paths": [],
            "external_bind_descriptors": [],
        }
        cfg.update(updates)
        return cfg

    def _artifact(self, directory, filename):
        with open(os.path.join(directory, filename), "wb") as handle:
            handle.write(b"authenticated snapshot artifact")

    def test_exact_dump_and_volume_archive_sets_are_accepted(self):
        self._artifact(self.dumps, "db.postgres.sql")
        self._artifact(self.archives, "media.tar")

        restore_cmd._authenticate_manifest_artifacts(
            self._cfg(), self.scratch_fd, "/opt/app",
        )

    def test_false_autodetect_plus_omitted_bind_db_leaves_unclaimed_dump(self):
        # This Compose plan is otherwise plausible when the plaintext flag is
        # flipped false: the DB uses an in-stack bind, so no DB volume archive
        # is expected.  The encrypted dump artifact proves autodetection was on.
        model = _compose_model()
        model["services"]["db"]["volumes"] = [{
            "type": "bind",
            "source": "/opt/app/db",
            "target": "/var/lib/postgresql/data",
        }]
        cfg = self._cfg(db_autodetect=False, db_services=[])
        restore_cmd._authenticate_manifest_compose_plan(cfg, model)

        self._artifact(self.dumps, "db.postgres.sql")
        self._artifact(self.archives, "media.tar")
        with self.assertRaises(util.CommandError) as raised:
            restore_cmd._authenticate_manifest_artifacts(
                cfg, self.scratch_fd, "/opt/app",
            )
        self.assertIn("database dump", raised.exception.stderr)

    def test_listed_missing_or_unclaimed_named_archive_is_rejected(self):
        self._artifact(self.dumps, "db.postgres.sql")
        for archives in ([], ["media.tar", "unclaimed.tar"]):
            for filename in os.listdir(self.archives):
                os.unlink(os.path.join(self.archives, filename))
            for filename in archives:
                self._artifact(self.archives, filename)
            with self.subTest(archives=archives), self.assertRaises(util.CommandError):
                restore_cmd._authenticate_manifest_artifacts(
                    self._cfg(), self.scratch_fd, "/opt/app",
                )


class ComposeFilenameAuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.stack_fd = os.open(
            self.tmp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        self.cfg = {"_manifest_schema_version": manifest.MANIFEST_SCHEMA_VERSION}

    def tearDown(self):
        os.close(self.stack_fd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name):
        with open(os.path.join(self.tmp, name), "w") as handle:
            handle.write("services: {}\n")

    def test_only_selected_regular_compose_file_is_accepted(self):
        self._file("docker-compose.yml")
        restore_cmd._authenticate_manifest_compose_filename(
            self.cfg, self.stack_fd, "docker-compose.yml",
        )

    def test_alternate_regular_compose_file_makes_selection_ambiguous(self):
        self._file("docker-compose.yml")
        self._file("compose.yml")
        with self.assertRaises(util.CommandError):
            restore_cmd._authenticate_manifest_compose_filename(
                self.cfg, self.stack_fd, "docker-compose.yml",
            )

    def test_manifest_cannot_select_missing_or_symlinked_compose_file(self):
        self._file("docker-compose.yml")
        os.symlink("docker-compose.yml", os.path.join(self.tmp, "compose.yml"))
        with self.assertRaises(util.CommandError):
            restore_cmd._authenticate_manifest_compose_filename(
                self.cfg, self.stack_fd, "compose.yml",
            )


if __name__ == "__main__":
    unittest.main()
