from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import restic, volumes
from docker_backup.commands import logs as logs_cmd


class ResticBuilderTest(unittest.TestCase):
    def test_password_file_always_present(self):
        for argv in (
            restic.build_init("/repo", "/k.key"),
            restic.build_backup("/repo", "/k.key", ["/opt/xibo"], ["/opt/xibo/shared/db"], ["docker-backup"]),
            restic.build_forget("/repo", "/k.key", {"daily": 7, "weekly": 4, "monthly": 6}, ["docker-backup"]),
            restic.build_restore("/repo", "/k.key", "latest", "/scratch"),
            restic.build_copy("/off", "/k.key", "/repo"),
        ):
            self.assertIn("--password-file", argv)
            self.assertIn("/k.key", argv)
            self.assertEqual(argv[0], "restic")

    def test_backup_has_exclude_and_tags(self):
        argv = restic.build_backup(
            "/repo", "/k.key", ["/opt/xibo"],
            ["/opt/xibo/shared/db", "/opt/xibo/shared/db2"],
            ["docker-backup", "stack:xibo"],
        )
        self.assertEqual(argv.count("--exclude"), 2)
        self.assertIn("/opt/xibo/shared/db", argv)
        self.assertEqual(argv.count("--tag"), 2)
        self.assertIn("stack:xibo", argv)
        self.assertEqual(argv[-1], "/opt/xibo")

    def test_restore_preserves_sparse_disk_usage(self):
        argv = restic.build_restore(
            "/repo", "/k.key", "latest", "/scratch", paths=["/opt/nextcloud"]
        )
        self.assertIn("--sparse", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "/scratch")
        self.assertEqual(argv[argv.index("--path") + 1], "/opt/nextcloud")

    def test_restore_uses_selected_scratch_path_directly(self):
        with mock.patch.object(restic.util, "run") as run:
            restic.restore(
                "/repo", "/k.key", "snapshot", "/root-only/scratch",
                paths=["/opt/gitlab"],
            )

        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--target") + 1], "/root-only/scratch")
        self.assertNotIn("cwd", run.call_args.kwargs)
        self.assertNotIn("pass_fds", run.call_args.kwargs)

    def test_latest_snapshot_filter_requires_all_stack_tags(self):
        argv = restic.build_snapshots(
            "/repo", "/k.key", latest=True,
            tags=["docker-backup", "stack:xibo"], group_by="tags",
        )
        self.assertEqual(argv[argv.index("--tag") + 1], "docker-backup,stack:xibo")
        self.assertEqual(argv[argv.index("--group-by") + 1], "tags")
        self.assertEqual(argv[argv.index("--latest") + 1], "1")

    def test_tagged_last_snapshot_uses_tag_grouping(self):
        payload = '[{"id":"%s","time":"2026-07-15T00:00:00Z"}]' % ("a" * 64)
        with mock.patch.object(
            restic.util, "run", return_value=mock.Mock(stdout=payload),
        ) as run:
            snapshot = restic.last_snapshot(
                "/repo", "/k.key", tags=["docker-backup", "stack:xibo"],
            )

        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--group-by") + 1], "tags")
        self.assertEqual(snapshot["id"], "a" * 64)

    def test_forget_retention_flags(self):
        argv = restic.build_forget("/repo", "/k.key", {"daily": 7, "weekly": 4, "monthly": 6}, ["docker-backup"])
        self.assertIn("--prune", argv)
        self.assertEqual(argv[argv.index("--keep-daily") + 1], "7")
        self.assertEqual(argv[argv.index("--keep-weekly") + 1], "4")
        self.assertEqual(argv[argv.index("--keep-monthly") + 1], "6")
        self.assertNotIn("--keep-within", argv)

    def test_forget_groups_by_tags(self):
        # Default grouping (host,paths) would freeze the old group forever as soon
        # as the path set changes (extra_backup_paths) or the host is renamed.
        argv = restic.build_forget("/repo", "/k.key", {"daily": 7, "weekly": 4, "monthly": 6}, ["docker-backup"])
        self.assertEqual(argv[argv.index("--group-by") + 1], "tags")

    def test_forget_keep_within_emitted_gitlab_case(self):
        argv = restic.build_forget(
            "/repo", "/k.key",
            {"daily": 7, "weekly": 0, "monthly": 0, "keep_within": "30d"},
            ["docker-backup"],
        )
        self.assertEqual(argv[argv.index("--keep-daily") + 1], "7")
        self.assertEqual(argv[argv.index("--keep-weekly") + 1], "0")
        self.assertEqual(argv[argv.index("--keep-monthly") + 1], "0")
        self.assertEqual(argv[argv.index("--keep-within") + 1], "30d")

    def test_forget_keep_within_omitted_when_none(self):
        argv = restic.build_forget(
            "/repo", "/k.key",
            {"daily": 7, "weekly": 4, "monthly": 6, "keep_within": None},
            ["docker-backup"],
        )
        self.assertNotIn("--keep-within", argv)

    def test_forget_defensive_missing_count_keys(self):
        # A dict without count keys must not raise a KeyError mid-run.
        argv = restic.build_forget("/repo", "/k.key", {"keep_within": "7d"}, [])
        self.assertEqual(argv[argv.index("--keep-daily") + 1], "0")
        self.assertEqual(argv[argv.index("--keep-weekly") + 1], "0")
        self.assertEqual(argv[argv.index("--keep-monthly") + 1], "0")
        self.assertEqual(argv[argv.index("--keep-within") + 1], "7d")

    def test_offsite_init_uses_chunker_params(self):
        argv = restic.build_init_offsite("/off", "/k.key", "/repo")
        self.assertIn("--copy-chunker-params", argv)
        self.assertIn("--from-repo", argv)
        self.assertEqual(argv[argv.index("--from-repo") + 1], "/repo")

    def test_copy_uses_from_repo(self):
        argv = restic.build_copy("/off", "/k.key", "/repo")
        self.assertIn("copy", argv)
        self.assertEqual(argv[argv.index("--from-repo") + 1], "/repo")
        self.assertIn("--from-password-file", argv)


class ResticCheckBuilderTest(unittest.TestCase):
    def test_default_is_plain_check(self):
        argv = restic.build_check("/repo", "/k.key")
        self.assertEqual(argv[0], "restic")
        self.assertIn("--password-file", argv)
        self.assertIn("/k.key", argv)
        self.assertIn("check", argv)
        self.assertNotIn("--read-data", argv)
        self.assertFalse(any(a.startswith("--read-data-subset") for a in argv))

    def test_read_data_full(self):
        argv = restic.build_check("/repo", "/k.key", read_data=True)
        self.assertIn("--read-data", argv)
        self.assertFalse(any(a.startswith("--read-data-subset") for a in argv))

    def test_read_data_subset(self):
        argv = restic.build_check("/repo", "/k.key", read_data_subset="5%")
        self.assertIn("--read-data-subset=5%", argv)
        self.assertNotIn("--read-data", argv)

    def test_read_data_wins_over_subset(self):
        argv = restic.build_check("/repo", "/k.key", read_data=True, read_data_subset="5%")
        self.assertIn("--read-data", argv)
        self.assertFalse(any(a.startswith("--read-data-subset") for a in argv))


class LogsBuilderTest(unittest.TestCase):
    def test_default(self):
        argv = logs_cmd.build_journalctl_argv("xibo", follow=False, lines=80, notify=False)
        self.assertEqual(argv[:3], ["journalctl", "-u", "docker-backup@xibo.service"])
        self.assertEqual(argv[argv.index("-n") + 1], "80")
        self.assertIn("--no-pager", argv)
        self.assertNotIn("-f", argv)
        self.assertEqual(argv.count("-u"), 1)

    def test_follow_and_notify(self):
        argv = logs_cmd.build_journalctl_argv("xibo", follow=True, lines=50, notify=True)
        self.assertIn("-f", argv)
        self.assertEqual(argv.count("-u"), 2)
        self.assertIn("docker-backup-notify@xibo.service", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "50")


class VolumeBuilderTest(unittest.TestCase):
    def test_backup_cmd_readonly_and_uncompressed_tar(self):
        argv = volumes.build_backup_cmd("xibo_library", "/opt/xibo/.docker-backup/volumes", "library")
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        self.assertIn("xibo_library:/volume:ro", " ".join(argv))
        self.assertIn("busybox", argv)
        self.assertIn("tar", argv)
        # UNCOMPRESSED on purpose: restic dedups/compresses itself; a gzip layer
        # would force a full re-upload of the volume on every run.
        self.assertIn("cf", argv)
        self.assertNotIn("czf", argv)
        self.assertEqual(argv[argv.index("cf") + 1], "-")
        self.assertNotIn(":/backup", " ".join(argv))
        self.assertNotIn("/opt/xibo/.docker-backup/volumes", " ".join(argv))

    def test_restore_cmd_extracts(self):
        argv = volumes.build_restore_cmd(
            "xibo_library", "/opt/xibo/.docker-backup/volumes", volumes.archive_name("library")
        )
        joined = " ".join(argv)
        self.assertIn("xibo_library:/volume", joined)
        self.assertNotIn(":ro", joined)  # restore mounts writable
        self.assertNotIn(":/backup", joined)  # archive is streamed from a verified FD
        self.assertIn("tar xf - -C /volume", joined)

    def test_restore_cmd_legacy_gzip(self):
        # Legacy snapshots (early gzip builds) contain <key>.tar.gz → extract with -z.
        argv = volumes.build_restore_cmd(
            "xibo_library", "/opt/xibo/.docker-backup/volumes",
            volumes.legacy_archive_name("library"),
        )
        self.assertIn("tar xzf - -C /volume", " ".join(argv))

    def test_restore_cmd_rejects_archive_path_traversal(self):
        with self.assertRaises(ValueError):
            volumes.build_restore_cmd("xibo_library", "/backup", "../../etc/passwd")

    def test_restore_streams_verified_archive_instead_of_host_bind(self):
        staging = os.path.realpath(tempfile.mkdtemp())
        try:
            archive = os.path.join(staging, volumes.archive_name("library"))
            payload = b"tar payload"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("payload.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            with open(archive, "rb") as fh:
                archive_payload = fh.read()
            staging_fd = volumes._open_staging_dir(staging)
            seen = []

            def consume(_argv, **kwargs):
                seen.append(kwargs["stdin"].read())
                return mock.Mock(returncode=0)

            try:
                with mock.patch.object(volumes.util, "run", side_effect=consume) as run:
                    volumes.restore_named_volume(
                        "xibo_library", staging, "library", staging_fd=staging_fd,
                    )
            finally:
                os.close(staging_fd)

            self.assertEqual(seen, [archive_payload])
            self.assertNotIn(staging, " ".join(run.call_args.args[0]))
        finally:
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
