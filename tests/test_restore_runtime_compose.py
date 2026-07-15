from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup.commands import restore as restore_cmd


class RestoreProjectNameTest(unittest.TestCase):
    def test_compose_normalization_inherits_no_ambient_variables(self):
        with mock.patch.dict(os.environ, {
            "IMAGE": "attacker/image:latest",
            "COMMAND": "rm -rf /",
            "RESTIC_PASSWORD": "must-not-interpolate",
            "COMPOSE_PROJECT_NAME": "wrong-project",
        }, clear=False):
            self.assertEqual(restore_cmd._protected_compose_environment(), {})

    def test_target_project_name_is_derived_from_operator_selected_target(self):
        self.assertEqual(
            restore_cmd._target_project_name("/opt/GitLab.Test"),
            "gitlabtest",
        )

    def test_authenticated_explicit_compose_name_is_honored(self):
        self.assertEqual(
            restore_cmd._authenticated_project_name(
                {"name": "corp-gitlab"},
                "/proc/4242/fd/71",
                "/opt/gitlab-restored",
            ),
            "corp-gitlab",
        )

    def test_transient_fd_default_is_replaced_with_target_project_name(self):
        self.assertEqual(
            restore_cmd._authenticated_project_name(
                {"name": "71"},
                "/proc/4242/fd/71",
                "/opt/GitLab-Restored",
            ),
            "gitlab-restored",
        )

    def test_scratch_rebase_preserves_parent_relative_compose_source(self):
        mirror = "/proc/4242/fd/70"
        project = mirror + "/opt/gitlab"
        self.assertEqual(
            restore_cmd._rebase_scratch_path(
                mirror + "/opt/shared", project, mirror,
                "/srv/restores/gitlab-test",
            ),
            "/srv/restores/shared",
        )
        self.assertEqual(
            restore_cmd._rebase_scratch_path(
                project + "/data", project, mirror,
                "/srv/restores/gitlab-test",
            ),
            "/srv/restores/gitlab-test/data",
        )
        self.assertEqual(
            restore_cmd._rebase_scratch_path(
                "/opt/gitlab/data", project, mirror,
                "/srv/restores/gitlab-test", "/opt/gitlab",
            ),
            "/srv/restores/gitlab-test/data",
        )


class RestoreRuntimeComposeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_project_cleanup_compose_is_sealed_inert_model(self):
        anonymous_files = []

        def fake_memfd_create(_name, _flags=0):
            backing = tempfile.TemporaryFile()
            anonymous_files.append(backing)
            return os.dup(backing.fileno())

        cleanup_fd = -1
        try:
            with mock.patch.object(
                    restore_cmd.os, "memfd_create", create=True,
                    side_effect=fake_memfd_create,
            ), mock.patch.object(
                    restore_cmd, "_fd_host_path",
                    side_effect=lambda fd: "/proc/4242/fd/%d" % fd,
            ):
                cleanup_fd, cleanup_path = (
                    restore_cmd._write_project_cleanup_compose("corp-gitlab")
                )

            self.assertEqual(cleanup_path, "/proc/4242/fd/%d" % cleanup_fd)
            os.lseek(cleanup_fd, 0, os.SEEK_SET)
            model = json.loads(os.read(cleanup_fd, 1024 * 1024))
            self.assertEqual(model["name"], "corp-gitlab")
            self.assertEqual(set(model["services"]), {
                "docker_backup_restore_cleanup",
            })
            self.assertEqual(
                model["services"]["docker_backup_restore_cleanup"],
                {"image": "scratch"},
            )
        finally:
            if cleanup_fd >= 0:
                os.close(cleanup_fd)
            for backing in anonymous_files:
                backing.close()

    def test_runtime_compose_is_anonymous_retained_and_rewrites_selected_roots(self):
        canonical_dest = os.path.join(self.tmp, "gitlab")
        external = os.path.join(self.tmp, "gitlab-registry")
        os.makedirs(canonical_dest)
        os.makedirs(external)
        os.makedirs(os.path.join(external, "data"))
        with open(os.path.join(external, "registry.secret"), "w") as fh:
            fh.write("secret\n")
        before = set(os.listdir(canonical_dest))

        operation_dest = "/proc/4242/fd/71"
        external_fd = os.open(external, os.O_RDONLY)
        anonymous_files = []

        def fake_memfd_create(_name, flags=0):
            # TemporaryFile is anonymous (or unlinked immediately), while dup lets
            # the production helper own and return the descriptor independently.
            backing = tempfile.TemporaryFile()
            anonymous_files.append(backing)
            return os.dup(backing.fileno())

        model = {
            "name": "transient-default",
            "services": {
                "gitlab": {
                    "image": "gitlab/gitlab-ee:18.1.1-ee.0",
                    "volumes": [
                        {"type": "bind", "source": canonical_dest,
                         "target": "/srv/gitlab"},
                        {"type": "bind",
                         "source": os.path.join(canonical_dest, "config"),
                         "target": "/etc/gitlab"},
                        {"type": "bind",
                         "source": os.path.join(external, "data"),
                         "target": "/var/opt/gitlab/registry"},
                        {"type": "bind", "source": "/etc/localtime",
                         "target": "/etc/localtime"},
                        {"type": "volume", "source": "gitlab-cache",
                         "target": "/var/cache/gitlab"},
                    ],
                    "build": {
                        "context": os.path.join(canonical_dest, "image"),
                    },
                },
            },
            "configs": {
                "omnibus": {
                    "file": os.path.join(canonical_dest, "gitlab.rb"),
                },
            },
            "secrets": {
                "registry": {
                    "file": os.path.join(external, "registry.secret"),
                },
            },
        }

        runtime_fd = -1
        runtime_source_fds = []
        try:
            with (
                mock.patch.object(
                    restore_cmd.os, "memfd_create", create=True,
                    side_effect=fake_memfd_create,
                ) as memfd_create,
                mock.patch.object(
                    restore_cmd.os, "MFD_CLOEXEC", 1, create=True,
                ),
                mock.patch.object(
                    restore_cmd, "_fd_host_path",
                    side_effect=lambda fd: "/proc/4242/fd/%d" % fd,
                ),
            ):
                runtime_fd, runtime_path, runtime_source_fds = (
                    restore_cmd._write_runtime_compose(
                    model,
                    operation_dest,
                    canonical_dest,
                    [(external, external_fd)],
                    "restored-gitlab",
                    )
                )

            memfd_create.assert_called_once()
            self.assertTrue(stat.S_ISREG(os.fstat(runtime_fd).st_mode))
            self.assertEqual(runtime_path, "/proc/4242/fd/%d" % runtime_fd)
            self.assertEqual(set(os.listdir(canonical_dest)), before)

            os.lseek(runtime_fd, 0, os.SEEK_SET)
            runtime_model = json.loads(os.read(runtime_fd, 1024 * 1024))
            service = runtime_model["services"]["gitlab"]
            sources = [volume["source"] for volume in service["volumes"]]
            retained_by_path = dict(runtime_source_fds)

            self.assertEqual(runtime_model["name"], "restored-gitlab")
            self.assertIn(operation_dest, sources)
            self.assertIn(operation_dest + "/config", sources)
            self.assertIn(
                "/proc/4242/fd/%d" % retained_by_path[
                    os.path.join(external, "data")
                ], sources,
            )
            self.assertIn("/etc/localtime", sources)
            self.assertIn("gitlab-cache", sources)
            self.assertEqual(service["build"]["context"], operation_dest + "/image")
            self.assertEqual(
                runtime_model["configs"]["omnibus"]["file"],
                operation_dest + "/gitlab.rb",
            )
            self.assertEqual(
                runtime_model["secrets"]["registry"]["file"],
                "/proc/4242/fd/%d" % retained_by_path[
                    os.path.join(external, "registry.secret")
                ],
            )
        finally:
            if runtime_fd >= 0:
                os.close(runtime_fd)
            restore_cmd._close_path_fds(runtime_source_fds)
            os.close(external_fd)
            for backing in anonymous_files:
                backing.close()

    def test_runtime_bind_children_are_pinned_to_their_exact_fds(self):
        canonical_dest = os.path.join(self.tmp, "app")
        external = os.path.join(self.tmp, "external")
        os.makedirs(os.path.join(canonical_dest, "data"))
        os.makedirs(os.path.join(external, "registry"))
        dest_fd = os.open(canonical_dest, os.O_RDONLY)
        external_fd = os.open(external, os.O_RDONLY)
        anonymous_files = []

        def fake_memfd_create(_name, _flags=0):
            backing = tempfile.TemporaryFile()
            anonymous_files.append(backing)
            return os.dup(backing.fileno())

        model = {
            "services": {
                "app": {
                    "image": "example/app:1",
                    "volumes": [
                        {"type": "bind",
                         "source": os.path.join(canonical_dest, "data"),
                         "target": "/data"},
                        {"type": "bind",
                         "source": os.path.join(external, "registry"),
                         "target": "/registry"},
                        {"type": "bind",
                         "source": os.path.join(canonical_dest, "logs"),
                         "target": "/logs",
                         "bind": {"create_host_path": True}},
                    ],
                },
            },
        }
        runtime_fd = -1
        retained = []
        try:
            with mock.patch.object(
                    restore_cmd.os, "memfd_create", create=True,
                    side_effect=fake_memfd_create,
            ), mock.patch.object(
                    restore_cmd, "_fd_host_path",
                    side_effect=lambda fd: "/proc/4242/fd/%d" % fd,
            ):
                runtime_fd, _runtime_path, retained = (
                    restore_cmd._write_runtime_compose(
                        model, "/proc/4242/fd/%d" % dest_fd,
                        canonical_dest, [(external, external_fd)], "app",
                        dest_fd=dest_fd,
                    )
                )

            os.lseek(runtime_fd, 0, os.SEEK_SET)
            runtime_model = json.loads(os.read(runtime_fd, 1024 * 1024))
            sources = [
                volume["source"]
                for volume in runtime_model["services"]["app"]["volumes"]
            ]
            retained_paths = {path for path, _fd in retained}
            self.assertEqual(retained_paths, {
                os.path.join(canonical_dest, "data"),
                os.path.join(canonical_dest, "logs"),
                os.path.join(external, "registry"),
            })
            self.assertTrue(os.path.isdir(os.path.join(canonical_dest, "logs")))
            self.assertTrue(all(source.startswith("/proc/4242/fd/") for source in sources))
            self.assertNotIn(
                "/proc/4242/fd/%d/data" % dest_fd,
                sources,
            )
            for path, fd in retained:
                restore_cmd._assert_path_matches_fd(path, fd)
        finally:
            if runtime_fd >= 0:
                os.close(runtime_fd)
            restore_cmd._close_path_fds(retained)
            os.close(external_fd)
            os.close(dest_fd)
            for backing in anonymous_files:
                backing.close()

    def test_compose_secret_is_pinned_from_scratch_and_target_is_verified(self):
        scratch = os.path.join(self.tmp, "scratch")
        snapshot_file = os.path.join(scratch, "opt", "app", "secret.txt")
        os.makedirs(os.path.dirname(snapshot_file))
        with open(snapshot_file, "w") as fh:
            fh.write("original-secret\n")
        scratch_fd = os.open(scratch, os.O_RDONLY)
        mirror = "/proc/4242/fd/70"
        target = os.path.join(self.tmp, "restored-app")
        os.makedirs(target)
        target_file = os.path.join(target, "secret.txt")
        shutil.copyfile(snapshot_file, target_file)
        dest_fd = os.open(target, os.O_RDONLY)
        anonymous_files = []

        def fake_memfd_create(_name, _flags=0):
            backing = tempfile.TemporaryFile()
            anonymous_files.append(backing)
            return os.dup(backing.fileno())

        trusted = []
        runtime_fd = -1
        retained = []
        try:
            source_model = {
                "services": {"app": {"image": "example/app:1"}},
                "secrets": {
                    "token": {"file": mirror + "/opt/app/secret.txt"},
                },
            }
            with mock.patch.object(
                    restore_cmd.os, "memfd_create", create=True,
                    side_effect=fake_memfd_create,
            ):
                trusted = restore_cmd._pin_compose_file_artifacts(
                    source_model, scratch_fd, mirror + "/opt/app",
                    mirror, target, "/opt/app",
                )
            self.assertEqual(trusted[0][0], target_file)
            os.lseek(trusted[0][1], 0, os.SEEK_SET)
            self.assertEqual(os.read(trusted[0][1], 100), b"original-secret\n")

            runtime_model = {
                "services": {"app": {"image": "example/app:1"}},
                "secrets": {"token": {"file": target_file}},
            }
            with mock.patch.object(
                    restore_cmd.os, "memfd_create", create=True,
                    side_effect=fake_memfd_create,
            ), mock.patch.object(
                    restore_cmd, "_fd_host_path",
                    side_effect=lambda fd: "/proc/4242/fd/%d" % fd,
            ):
                runtime_fd, _runtime_path, retained = (
                    restore_cmd._write_runtime_compose(
                        runtime_model, "/proc/4242/fd/%d" % dest_fd,
                        target, [], "app", dest_fd=dest_fd,
                        trusted_file_fds=trusted,
                    )
                )
            os.lseek(runtime_fd, 0, os.SEEK_SET)
            rendered = json.loads(os.read(runtime_fd, 1024 * 1024))
            self.assertEqual(
                rendered["secrets"]["token"]["file"],
                "/proc/4242/fd/%d" % trusted[0][1],
            )
            self.assertIn(target_file, dict(retained))

            with open(target_file, "w") as fh:
                fh.write("tampered\n")
            with mock.patch.object(
                    restore_cmd.os, "memfd_create", create=True,
                    side_effect=fake_memfd_create,
            ), mock.patch.object(
                    restore_cmd, "_fd_host_path",
                    side_effect=lambda fd: "/proc/4242/fd/%d" % fd,
            ):
                with self.assertRaisesRegex(ValueError, "changed before use"):
                    restore_cmd._write_runtime_compose(
                        runtime_model, "/proc/4242/fd/%d" % dest_fd,
                        target, [], "app", dest_fd=dest_fd,
                        trusted_file_fds=trusted,
                    )
        finally:
            if runtime_fd >= 0:
                os.close(runtime_fd)
            restore_cmd._close_path_fds(retained)
            restore_cmd._close_trusted_file_fds(trusted)
            os.close(dest_fd)
            os.close(scratch_fd)
            for backing in anonymous_files:
                backing.close()


if __name__ == "__main__":
    unittest.main()
