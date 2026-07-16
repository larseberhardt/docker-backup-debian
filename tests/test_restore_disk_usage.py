from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import util
from docker_backup.commands import restore as restore_cmd


class RestoreDiskUsageTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        util.set_dry_run(False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rename_race(self, source_name, swap_path, parked_path, outside_path):
        """Patch rename/replace to swap a destination component at the syscall edge."""
        real_rename = os.rename
        real_replace = os.replace
        race = {"fired": False}

        def wrap(operation):
            def racing_operation(src, dest, *args, **kwargs):
                if (not race["fired"]
                        and os.path.basename(os.fspath(src)) == source_name):
                    race["fired"] = True
                    real_rename(swap_path, parked_path)
                    os.symlink(outside_path, swap_path)
                return operation(src, dest, *args, **kwargs)
            return racing_operation

        return (
            race,
            mock.patch.object(restore_cmd.os, "rename", side_effect=wrap(real_rename)),
            mock.patch.object(restore_cmd.os, "replace", side_effect=wrap(real_replace)),
        )

    def _allow_safe_race_abort(self, operation):
        """A secure placement may detect the swap and abort instead of completing."""
        try:
            operation()
        except (OSError, ValueError, util.CommandError):
            pass

    def test_scratch_is_created_beside_destination(self):
        dest = os.path.join(self.tmp, "nextcloud-restored")
        scratch = restore_cmd._make_restore_scratch("nextcloud", dest)
        try:
            self.assertEqual(os.path.dirname(scratch.path), self.tmp)
            self.assertTrue(os.path.isdir(scratch.path))
        finally:
            restore_cmd._cleanup_restore_scratch(scratch)

    def test_scratch_is_inside_existing_destination_mount(self):
        dest = os.path.join(self.tmp, "gitlab-restored")
        os.makedirs(dest)

        scratch = restore_cmd._make_restore_scratch("gitlab", dest)

        try:
            self.assertEqual(os.path.dirname(scratch.path), dest)
            self.assertTrue(os.path.isdir(scratch.path))
        finally:
            restore_cmd._cleanup_restore_scratch(scratch)

    def test_scratch_creation_does_not_follow_racing_destination_symlink(self):
        dest = os.path.join(self.tmp, "scratch-target")
        parked = os.path.join(self.tmp, "scratch-target-before-race")
        outside = os.path.join(self.tmp, "outside-scratch")
        os.makedirs(dest)
        os.makedirs(outside)
        real_mkdir = os.mkdir
        fired = {"value": False}

        def racing_mkdir(path, mode=0o777, *, dir_fd=None):
            if (not fired["value"] and isinstance(path, str)
                    and path.startswith(".docker-backup-restore.gitlab.")):
                fired["value"] = True
                os.rename(dest, parked)
                os.symlink(outside, dest)
            return real_mkdir(path, mode, dir_fd=dir_fd)

        scratch = None
        try:
            with mock.patch.object(restore_cmd.os, "mkdir", side_effect=racing_mkdir):
                scratch = restore_cmd._make_restore_scratch("gitlab", dest)
            self.assertTrue(fired["value"])
            self.assertEqual(os.listdir(outside), [])
        finally:
            if scratch is not None:
                restore_cmd._cleanup_restore_scratch(scratch)

    def test_snapshot_source_stays_anchored_after_scratch_path_swap(self):
        scratch = os.path.join(self.tmp, "anchored-scratch")
        parked = os.path.join(self.tmp, "anchored-scratch-before-race")
        outside = os.path.join(self.tmp, "outside-source")
        dest = os.path.join(self.tmp, "anchored-target")
        os.makedirs(os.path.join(scratch, "opt", "app"))
        os.makedirs(os.path.join(outside, "opt", "app"))
        with open(os.path.join(scratch, "opt", "app", "marker"), "w") as fh:
            fh.write("trusted snapshot")
        with open(os.path.join(outside, "opt", "app", "marker"), "w") as fh:
            fh.write("attacker tree")
        scratch_fd = restore_cmd._open_absolute_dir_fd(scratch, create=False)
        placed_fd = -1
        try:
            os.rename(scratch, parked)
            os.symlink(outside, scratch)
            placed_fd = restore_cmd._move_tree_from_snapshot(
                scratch_fd, "/opt/app", dest,
            )
        finally:
            os.close(scratch_fd)
            if placed_fd >= 0:
                os.close(placed_fd)

        with open(os.path.join(dest, "marker")) as fh:
            self.assertEqual(fh.read(), "trusted snapshot")

    def test_placed_destination_descriptor_survives_path_replacement(self):
        scratch = os.path.join(self.tmp, "destination-fd-scratch")
        dest = os.path.join(self.tmp, "destination-fd-target")
        parked = os.path.join(self.tmp, "destination-fd-target-original")
        outside = os.path.join(self.tmp, "destination-fd-outside")
        os.makedirs(os.path.join(scratch, "opt", "app"))
        os.makedirs(outside)
        with open(os.path.join(scratch, "opt", "app", "marker"), "w") as fh:
            fh.write("trusted destination")
        scratch_fd = restore_cmd._open_absolute_dir_fd(scratch, create=False)
        placed_fd = -1
        marker_fd = -1
        try:
            placed_fd = restore_cmd._move_tree_from_snapshot(
                scratch_fd, "/opt/app", dest,
            )
            os.rename(dest, parked)
            os.symlink(outside, dest)
            marker_fd = os.open("marker", os.O_RDONLY, dir_fd=placed_fd)
            self.assertEqual(os.read(marker_fd, 64), b"trusted destination")
            with self.assertRaises((OSError, ValueError)):
                restore_cmd._assert_path_matches_fd(dest, placed_fd)
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
            if placed_fd >= 0:
                os.close(placed_fd)
            os.close(scratch_fd)

    def test_move_tree_removes_source_for_new_destination(self):
        src = os.path.join(self.tmp, "scratch", "opt", "nextcloud")
        dest = os.path.join(self.tmp, "nextcloud-restored")
        os.makedirs(os.path.join(src, "data"))
        with open(os.path.join(src, "data", "file.bin"), "wb") as f:
            f.write(b"payload")

        restore_cmd._move_tree(src, dest)

        self.assertFalse(os.path.exists(src))
        with open(os.path.join(dest, "data", "file.bin"), "rb") as f:
            self.assertEqual(f.read(), b"payload")

    def test_move_tree_merges_and_consumes_source(self):
        src = os.path.join(self.tmp, "scratch")
        dest = os.path.join(self.tmp, "target")
        os.makedirs(os.path.join(src, "data"))
        os.makedirs(os.path.join(dest, "data"))
        with open(os.path.join(src, "data", "new"), "w") as f:
            f.write("new")
        with open(os.path.join(dest, "data", "old"), "w") as f:
            f.write("old")

        restore_cmd._move_tree(src, dest)

        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(os.path.join(dest, "data", "new")))
        self.assertTrue(os.path.exists(os.path.join(dest, "data", "old")))

    def test_no_force_move_rejects_directory_appearing_after_empty_check(self):
        scratch = os.path.join(self.tmp, "no-force-race-scratch")
        source = os.path.join(scratch, "opt", "app")
        dest = os.path.join(self.tmp, "no-force-race-target")
        os.makedirs(os.path.join(source, "data"))
        os.makedirs(dest)
        with open(os.path.join(source, "data", "trusted"), "w") as fh:
            fh.write("snapshot")

        scratch_fd = restore_cmd._open_absolute_dir_fd(scratch, create=False)
        dest_fd = restore_cmd._open_absolute_dir_fd(dest, create=False)
        real_move_tree_fds = restore_cmd._move_tree_fds
        placed_fd = -1
        fired = {"value": False}

        def add_attacker_directory(source_fd, target_fd, *, replace=True):
            if not fired["value"]:
                fired["value"] = True
                os.mkdir("data", 0o700, dir_fd=target_fd)
                attacker_dir_fd = restore_cmd._open_dir_at(target_fd, "data")
                attacker_fd = -1
                try:
                    attacker_fd = os.open(
                        "attacker", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=attacker_dir_fd,
                    )
                    os.write(attacker_fd, b"do not merge")
                finally:
                    if attacker_fd >= 0:
                        os.close(attacker_fd)
                    os.close(attacker_dir_fd)
            return real_move_tree_fds(
                source_fd, target_fd, replace=replace,
            )

        rejected = None
        try:
            with mock.patch.object(
                    restore_cmd, "_move_tree_fds",
                    side_effect=add_attacker_directory):
                try:
                    placed_fd = restore_cmd._move_tree_from_snapshot(
                        scratch_fd, "/opt/app", dest,
                        expected_target_fd=dest_fd,
                        allow_replace=False,
                    )
                except FileExistsError as exc:
                    rejected = exc
        finally:
            if placed_fd >= 0:
                os.close(placed_fd)
            os.close(dest_fd)
            os.close(scratch_fd)

        self.assertTrue(fired["value"], "test did not inject the target race")
        self.assertIsNotNone(rejected, "no-force placement merged attacker data")
        self.assertTrue(os.path.isfile(os.path.join(dest, "data", "attacker")))
        self.assertTrue(os.path.isfile(os.path.join(source, "data", "trusted")))

    def test_force_clean_replacement_refuses_mountpoint_without_traversing_it(self):
        scratch = os.path.join(self.tmp, "mountpoint-scratch")
        source = os.path.join(scratch, "opt", "app")
        dest = os.path.join(self.tmp, "mountpoint-target")
        mounted = os.path.join(dest, "mounted-data")
        os.makedirs(source)
        os.makedirs(mounted)
        with open(os.path.join(source, "snapshot"), "w") as fh:
            fh.write("trusted")
        sentinel = os.path.join(mounted, "do-not-delete")
        with open(sentinel, "w") as fh:
            fh.write("mounted data")

        scratch_fd = restore_cmd._open_absolute_dir_fd(scratch, create=False)
        dest_fd = restore_cmd._open_absolute_dir_fd(dest, create=False)
        mounted_inode = os.stat(mounted).st_ino
        traversed_inodes = []
        real_listdir = os.listdir

        def mount_id(fd):
            return 200 if os.fstat(fd).st_ino == mounted_inode else 100

        def track_listdir(fd):
            traversed_inodes.append(os.fstat(fd).st_ino)
            return real_listdir(fd)

        try:
            with mock.patch.object(
                    restore_cmd, "_mount_id_for_fd", side_effect=mount_id), \
                 mock.patch.object(
                    restore_cmd.os, "listdir", side_effect=track_listdir):
                with self.assertRaises(util.CommandError) as raised:
                    restore_cmd._move_tree_from_snapshot(
                        scratch_fd, "/opt/app", dest,
                        expected_target_fd=dest_fd, allow_replace=True,
                    )
        finally:
            os.close(dest_fd)
            os.close(scratch_fd)

        self.assertIn("mounted destination entry", raised.exception.stderr)
        self.assertNotIn(mounted_inode, traversed_inodes)
        self.assertTrue(os.path.isfile(sentinel))
        self.assertTrue(os.path.isfile(os.path.join(source, "snapshot")))

    def test_force_clean_replacement_fails_closed_without_mount_identity(self):
        scratch = os.path.join(self.tmp, "mount-id-scratch")
        source = os.path.join(scratch, "opt", "app")
        dest = os.path.join(self.tmp, "mount-id-target")
        existing = os.path.join(dest, "existing")
        os.makedirs(source)
        os.makedirs(existing)
        sentinel = os.path.join(existing, "do-not-delete")
        with open(sentinel, "w") as fh:
            fh.write("existing data")

        scratch_fd = restore_cmd._open_absolute_dir_fd(scratch, create=False)
        dest_fd = restore_cmd._open_absolute_dir_fd(dest, create=False)
        try:
            with mock.patch.object(
                    restore_cmd, "_mount_id_for_fd",
                    side_effect=OSError(errno.EOPNOTSUPP, "no fdinfo")):
                with self.assertRaises(util.CommandError) as raised:
                    restore_cmd._move_tree_from_snapshot(
                        scratch_fd, "/opt/app", dest,
                        expected_target_fd=dest_fd, allow_replace=True,
                    )
        finally:
            os.close(dest_fd)
            os.close(scratch_fd)

        self.assertIn("Cannot prove", raised.exception.stderr)
        self.assertTrue(os.path.isfile(sentinel))

    def test_move_tree_replaces_conflicting_file_without_copying(self):
        src = os.path.join(self.tmp, "scratch")
        dest = os.path.join(self.tmp, "target")
        os.makedirs(src)
        os.makedirs(dest)
        with open(os.path.join(src, "large-sparse-file"), "wb") as f:
            f.seek(1024 * 1024)
            f.write(b"end")
        with open(os.path.join(dest, "large-sparse-file"), "wb") as f:
            f.write(b"old")

        with mock.patch.object(restore_cmd.os, "rename", wraps=os.rename) as rename:
            restore_cmd._move_tree(src, dest)

        rename.assert_called_once()
        self.assertFalse(os.path.exists(src))
        self.assertEqual(os.path.getsize(os.path.join(dest, "large-sparse-file")),
                         1024 * 1024 + 3)

    def test_move_tree_replaces_file_with_directory(self):
        src = os.path.join(self.tmp, "scratch")
        dest = os.path.join(self.tmp, "target")
        os.makedirs(os.path.join(src, "node"))
        os.makedirs(dest)
        with open(os.path.join(src, "node", "child"), "w") as f:
            f.write("new")
        with open(os.path.join(dest, "node"), "w") as f:
            f.write("old")

        restore_cmd._move_tree(src, dest)

        self.assertTrue(os.path.isdir(os.path.join(dest, "node")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "node", "child")))

    def test_move_tree_replaces_directory_with_file(self):
        src = os.path.join(self.tmp, "scratch")
        dest = os.path.join(self.tmp, "target")
        os.makedirs(src)
        os.makedirs(os.path.join(dest, "node"))
        with open(os.path.join(src, "node"), "w") as f:
            f.write("new")
        with open(os.path.join(dest, "node", "old-child"), "w") as f:
            f.write("old")

        restore_cmd._move_tree(src, dest)

        self.assertTrue(os.path.isfile(os.path.join(dest, "node")))
        with open(os.path.join(dest, "node")) as f:
            self.assertEqual(f.read(), "new")

    def test_move_tree_does_not_follow_destination_symlink(self):
        src = os.path.join(self.tmp, "scratch")
        dest = os.path.join(self.tmp, "target")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(src, "node"))
        os.makedirs(dest)
        os.makedirs(outside)
        with open(os.path.join(src, "node", "restored"), "w") as f:
            f.write("new")
        with open(os.path.join(outside, "keep"), "w") as f:
            f.write("outside")
        os.symlink(outside, os.path.join(dest, "node"))

        restore_cmd._move_tree(src, dest)

        self.assertFalse(os.path.islink(os.path.join(dest, "node")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "node", "restored")))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep")))

    def test_cross_filesystem_sparse_file_fallback_is_fd_relative(self):
        src = os.path.join(self.tmp, "cross-source.bin")
        parent = os.path.join(self.tmp, "cross-target")
        dest = os.path.join(parent, "payload.bin")
        os.makedirs(parent)
        with open(src, "wb") as fh:
            fh.seek(1024 * 1024)
            fh.write(b"end")
        os.chmod(src, 0o640)

        with mock.patch.object(
                restore_cmd.os, "rename",
                side_effect=OSError(errno.EXDEV, "different filesystems")):
            restore_cmd._move_regular_file(src, dest, replace=False)

        self.assertFalse(os.path.exists(src))
        self.assertEqual(os.path.getsize(dest), 1024 * 1024 + 3)
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o640)

    def test_cross_filesystem_no_replace_entry_consumes_source_once(self):
        source_parent = os.path.join(self.tmp, "no-replace-source")
        target_parent = os.path.join(self.tmp, "no-replace-target")
        source = os.path.join(source_parent, "payload.bin")
        target = os.path.join(target_parent, "payload.bin")
        os.makedirs(source_parent)
        os.makedirs(target_parent)
        with open(source, "wb") as fh:
            fh.write(b"start")
            fh.seek(1024 * 1024)
            fh.write(b"end")

        source_parent_fd = restore_cmd._open_absolute_dir_fd(
            source_parent, create=False,
        )
        target_parent_fd = restore_cmd._open_absolute_dir_fd(
            target_parent, create=False,
        )
        real_unlink = os.unlink
        try:
            with mock.patch.object(
                    restore_cmd.os, "link",
                    side_effect=OSError(errno.EXDEV, "different filesystems")), \
                 mock.patch.object(
                    restore_cmd, "_copy_sparse_file_at",
                    wraps=restore_cmd._copy_sparse_file_at) as copy_sparse, \
                 mock.patch.object(
                    restore_cmd.os, "unlink", wraps=real_unlink) as unlink:
                restore_cmd._move_entry_at(
                    source_parent_fd, "payload.bin",
                    target_parent_fd, "payload.bin", replace=False,
                )

            copy_sparse.assert_called_once_with(
                source_parent_fd, "payload.bin",
                target_parent_fd, "payload.bin",
            )
            source_unlinks = [
                call for call in unlink.call_args_list
                if call.args == ("payload.bin",)
                and call.kwargs.get("dir_fd") == source_parent_fd
            ]
            self.assertEqual(len(source_unlinks), 1)
        finally:
            os.close(target_parent_fd)
            os.close(source_parent_fd)

        self.assertFalse(os.path.exists(source))
        self.assertEqual(os.path.getsize(target), 1024 * 1024 + 3)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(5), b"start")
            fh.seek(-3, os.SEEK_END)
            self.assertEqual(fh.read(), b"end")

    def test_cross_filesystem_directory_fallback_consumes_source(self):
        src = os.path.join(self.tmp, "cross-dir-source")
        dest = os.path.join(self.tmp, "cross-dir-target")
        os.makedirs(os.path.join(src, "nested"))
        with open(os.path.join(src, "nested", "payload"), "w") as fh:
            fh.write("restored")

        with mock.patch.object(
                restore_cmd.os, "rename",
                side_effect=OSError(errno.EXDEV, "different filesystems")):
            restore_cmd._move_tree(src, dest)

        self.assertFalse(os.path.exists(src))
        with open(os.path.join(dest, "nested", "payload")) as fh:
            self.assertEqual(fh.read(), "restored")

    def test_move_tree_missing_main_target_does_not_follow_racing_parent_symlink(self):
        src = os.path.join(self.tmp, "main-stack-source")
        target_parent = os.path.join(self.tmp, "main-target-parent")
        parked_parent = os.path.join(self.tmp, "main-target-parent-before-race")
        outside = os.path.join(self.tmp, "outside-main")
        dest = os.path.join(target_parent, "restored-stack")
        os.makedirs(src)
        os.makedirs(target_parent)
        os.makedirs(outside)
        with open(os.path.join(src, "compose.yml"), "w") as f:
            f.write("services: {}\n")
        with open(os.path.join(outside, "sentinel"), "w") as f:
            f.write("outside")

        race, rename, replace = self._rename_race(
            "main-stack-source", target_parent, parked_parent, outside,
        )
        with rename, replace:
            self._allow_safe_race_abort(lambda: restore_cmd._move_tree(src, dest))

        self.assertTrue(race["fired"], "test did not reach the injected race")
        self.assertEqual(os.listdir(outside), ["sentinel"])

    def test_move_tree_merge_does_not_follow_racing_destination_root(self):
        src = os.path.join(self.tmp, "merge-source")
        dest = os.path.join(self.tmp, "merge-target")
        parked_dest = os.path.join(self.tmp, "merge-target-before-race")
        outside = os.path.join(self.tmp, "outside-merge")
        os.makedirs(src)
        os.makedirs(dest)
        os.makedirs(outside)
        with open(os.path.join(src, "restored-payload"), "w") as f:
            f.write("restored")
        with open(os.path.join(outside, "sentinel"), "w") as f:
            f.write("outside")

        race, rename, replace = self._rename_race(
            "restored-payload", dest, parked_dest, outside,
        )
        with rename, replace:
            self._allow_safe_race_abort(lambda: restore_cmd._move_tree(src, dest))

        self.assertTrue(race["fired"], "test did not reach the injected race")
        self.assertEqual(os.listdir(outside), ["sentinel"])

    def test_external_file_restore_does_not_follow_racing_parent_symlink(self):
        scratch = os.path.join(self.tmp, "external-scratch")
        snapshot_path = "/srv/ext-payload.bin"
        restored = os.path.join(scratch, "srv", "ext-payload.bin")
        target_parent = os.path.join(self.tmp, "external-target-parent")
        parked_parent = os.path.join(self.tmp, "external-target-parent-before-race")
        outside = os.path.join(self.tmp, "outside-external")
        target = os.path.join(target_parent, "restored.bin")
        os.makedirs(os.path.dirname(restored))
        os.makedirs(target_parent)
        os.makedirs(outside)
        with open(restored, "wb") as f:
            f.write(b"restored")
        with open(os.path.join(outside, "sentinel"), "w") as f:
            f.write("outside")
        cfg = {
            "extra_backup_paths": [snapshot_path],
            "_manifest_schema_version": 5,
        }

        race, rename, replace = self._rename_race(
            "ext-payload.bin", target_parent, parked_parent, outside,
        )
        real_link = os.link

        def racing_link(src, dst, *args, **kwargs):
            if (not race["fired"]
                    and os.path.basename(os.fspath(src)) == "ext-payload.bin"):
                race["fired"] = True
                os.rename(target_parent, parked_parent)
                os.symlink(outside, target_parent)
            return real_link(src, dst, *args, **kwargs)

        with rename, replace, mock.patch.object(
                restore_cmd.os, "link", side_effect=racing_link,
        ):
            self._allow_safe_race_abort(
                lambda: restore_cmd._restore_extra_paths(
                    cfg, scratch, force=False, mappings=[(snapshot_path, target)],
                )
            )

        self.assertTrue(race["fired"], "test did not reach the injected race")
        self.assertEqual(os.listdir(outside), ["sentinel"])

    def test_success_cleanup_removes_large_artifacts_only(self):
        dest = os.path.join(self.tmp, "nextcloud-restored")
        staging = os.path.join(dest, ".docker-backup")
        os.makedirs(os.path.join(staging, "volumes"))
        os.makedirs(os.path.join(staging, "dumps"))
        with open(os.path.join(staging, "volumes", "nextcloud.tar"), "wb") as f:
            f.write(b"volume archive")
        with open(os.path.join(staging, "dumps", "db.sql"), "wb") as f:
            f.write(b"database dump")
        with open(os.path.join(staging, "keep"), "w") as f:
            f.write("unrelated")

        restore_cmd._cleanup_restore_staging(dest)

        self.assertFalse(os.path.exists(os.path.join(staging, "volumes")))
        self.assertFalse(os.path.exists(os.path.join(staging, "dumps")))
        self.assertTrue(os.path.exists(os.path.join(staging, "keep")))

    def test_cleanup_does_not_follow_racing_staging_symlink(self):
        dest = os.path.join(self.tmp, "cleanup-target")
        staging = os.path.join(dest, ".docker-backup")
        parked = os.path.join(dest, ".docker-backup-before-race")
        outside = os.path.join(self.tmp, "outside-cleanup")
        os.makedirs(os.path.join(staging, "dumps"))
        os.makedirs(os.path.join(outside, "dumps"))
        with open(os.path.join(staging, "dumps", "archive.sql"), "w") as fh:
            fh.write("restored")
        sentinel = os.path.join(outside, "dumps", "sentinel")
        with open(sentinel, "w") as fh:
            fh.write("outside")
        real_open = os.open
        fired = {"value": False}

        def racing_open(path, flags, *args, **kwargs):
            if path == ".docker-backup" and not fired["value"]:
                fired["value"] = True
                os.rename(staging, parked)
                os.symlink(outside, staging)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(restore_cmd.os, "open", side_effect=racing_open):
            self._allow_safe_race_abort(
                lambda: restore_cmd._cleanup_restore_staging(dest)
            )

        self.assertTrue(fired["value"])
        self.assertTrue(os.path.isfile(sentinel))

    def test_failed_restic_restore_does_not_leave_scratch_tree(self):
        dest = os.path.join(self.tmp, "nextcloud-restored")
        cfg = {
            "repo": "/repo",
            "key_file": "/key",
            "stack_path": "/opt/nextcloud",
            "backend_env_file": None,
        }
        seen = []

        def fail_restore(_repo, _key, _snapshot, scratch, paths=None, **kwargs):
            self.assertNotIn("target_fd", kwargs)
            seen.append(scratch)
            self.assertEqual(stat.S_IMODE(os.stat(scratch).st_mode), 0o700)
            with open(os.path.join(scratch, "partial-restore"), "wb") as f:
                f.write(b"partial data")
            raise RuntimeError("restore interrupted")

        with mock.patch.object(restore_cmd.restic, "repo_initialized", return_value=True), \
             mock.patch.object(restore_cmd.restic, "restore", side_effect=fail_restore), \
             mock.patch.object(
                 restore_cmd.compose, "running_writable_bind_mounts_overlapping",
                 return_value=[],
             ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                restore_cmd._run_restore(
                    cfg, "nextcloud", dest, "latest", force=False,
                )

        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))

    def test_restore_rejects_restic_without_sparse_support(self):
        dest = os.path.join(self.tmp, "nextcloud-restored")
        cfg = {
            "repo": "/repo",
            "key_file": "/key",
            "stack_path": "/opt/nextcloud",
            "backend_env_file": None,
        }
        with mock.patch.object(restore_cmd.restic, "restic_version",
                               return_value=(0, 14, 0)), \
             mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd._run_restore(
                cfg, "nextcloud", dest, "latest", force=False,
            )

        self.assertEqual(rc, 1)
        restore.assert_not_called()

    def test_restore_rejects_manifest_path_traversal_before_restic(self):
        dest = os.path.join(self.tmp, "restored")
        cfg = {
            "repo": "/repo", "key_file": "/key", "backend_env_file": None,
            "stack_path": "/../../etc", "extra_backup_paths": [],
        }
        with mock.patch.object(restore_cmd.restic, "repo_initialized", return_value=True), \
             mock.patch.object(restore_cmd.restic, "restore") as restore:
            rc = restore_cmd._run_restore(cfg, "unsafe", dest, "latest", force=False)

        self.assertEqual(rc, 1)
        restore.assert_not_called()

    def test_locate_restored_rejects_parent_symlink_escape(self):
        scratch = os.path.join(self.tmp, "scratch")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(scratch)
        os.makedirs(os.path.join(outside, "app"))
        os.symlink(outside, os.path.join(scratch, "opt"))

        self.assertIsNone(restore_cmd._locate_restored(scratch, "/opt/app"))

    def test_locate_restored_rejects_stack_root_symlink(self):
        scratch = os.path.join(self.tmp, "scratch")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(scratch, "opt"))
        os.makedirs(outside)
        os.symlink(outside, os.path.join(scratch, "opt", "app"))

        self.assertIsNone(restore_cmd._locate_restored(scratch, "/opt/app"))

    def test_failed_database_import_still_stops_service(self):
        db = {"service": "snipeit-mysql", "engine": "mysql",
              "password_source": "none"}
        cfg = {"db_services": [db]}
        dest = os.path.join(self.tmp, "snipeit")
        compose_file = os.path.join(dest, "docker-compose.yml")
        os.makedirs(os.path.join(dest, ".docker-backup", "dumps"))
        with mock.patch.object(restore_cmd.compose, "up_service"), \
             mock.patch.object(restore_cmd.compose, "rm_service") as rm_service, \
             mock.patch.object(restore_cmd.dbdump, "wait_ready", return_value=True), \
             mock.patch.object(restore_cmd.dbdump, "import_dump",
                               side_effect=RuntimeError("import failed")):
            with self.assertRaisesRegex(RuntimeError, "import failed"):
                restore_cmd._import_databases(
                    cfg, {}, compose_file, dest, "snipeit"
                )

        rm_service.assert_called_once_with(
            compose_file, dest, "snipeit-mysql", "snipeit"
        )

    def test_cross_server_dry_run_does_not_resolve_source_database_password(self):
        util.set_dry_run(True)
        db = {
            "service": "snipeit-mysql",
            "engine": "mysql",
            "auth_user": "root",
            "password_source": "env:MYSQL_ROOT_PASSWORD",
            "databases": ["snipe"],
            "all_databases": False,
        }
        cfg = {
            "name": "snipeit",
            "repo": "/mnt/backups/snipeit",
            "key_file": "/etc/docker-backup/keys/snipeit.key",
            "backend_env_file": None,
            "stack_path": "/opt/snipeit",
            # Cross-server manifests deliberately contain only the basename.
            "compose_file": "docker-compose.yml",
            "project_name": "snipeit",
            "extra_backup_paths": [],
            "named_volumes": [],
            "db_services": [db],
        }
        dest = "/opt/snipeit-drill"
        target_compose = "/opt/snipeit-drill/docker-compose.yml"

        with mock.patch.object(restore_cmd.restic, "restore"), \
             mock.patch.object(
                 restore_cmd.runtime, "resolve_password",
                 side_effect=AssertionError(
                     "dry-run must not read source Compose credentials"
                 ),
             ) as resolve_password, \
             mock.patch.object(restore_cmd.compose, "up_service"), \
             mock.patch.object(restore_cmd.compose, "rm_service"), \
             mock.patch.object(restore_cmd.dbdump, "wait_ready", return_value=True), \
             mock.patch.object(restore_cmd.dbdump, "import_dump") as import_dump:
            rc = restore_cmd._run_restore(
                cfg, "snipeit", dest, "snapshot-id", force=False,
                no_custom_restore=True,
            )

        self.assertEqual(rc, 0)
        resolve_password.assert_not_called()
        import_dump.assert_called_once_with(
            db, None, target_compose, dest, "snipeit-drill",
            "/opt/snipeit-drill/.docker-backup/dumps", dumps_fd=None,
        )

    def test_partial_database_start_failure_still_removes_service(self):
        db = {"service": "db", "engine": "mysql", "password_source": "none"}
        cfg = {"db_services": [db]}
        dest = os.path.join(self.tmp, "app")
        compose_file = os.path.join(dest, "docker-compose.yml")
        os.makedirs(os.path.join(dest, ".docker-backup", "dumps"))

        with mock.patch.object(
                restore_cmd.compose, "up_service",
                side_effect=RuntimeError("partial DB start"),
        ) as up_service, mock.patch.object(
                restore_cmd.compose, "rm_service",
        ) as rm_service:
            with self.assertRaisesRegex(RuntimeError, "partial DB start"):
                restore_cmd._import_databases(
                    cfg, {}, compose_file, dest, "app",
                )

        up_service.assert_called_once_with(
            compose_file, dest, "db", "app", no_deps=True,
        )
        rm_service.assert_called_once_with(
            compose_file, dest, "db", "app",
        )


if __name__ == "__main__":
    unittest.main()
