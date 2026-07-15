"""External bind mounts: detection (create), fail-loud (run), restore semantics."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import compose, util
from docker_backup.commands import restore as restore_cmd
from docker_backup.commands import run as run_cmd


def _cj(volumes_by_service):
    return {
        "name": "app",
        "services": {
            svc: {"image": "nginx", "volumes": vols}
            for svc, vols in volumes_by_service.items()
        },
        "volumes": {},
    }


class FindExternalBindsTest(unittest.TestCase):
    STACK = "/opt/app"

    def test_inside_stack_is_covered(self):
        cj = _cj({"web": [
            {"type": "bind", "source": "/opt/app/html", "target": "/usr/share/nginx/html"},
            {"type": "bind", "source": "/opt/app", "target": "/all"},
        ]})
        self.assertEqual(compose.find_external_binds(cj, self.STACK), [])

    def test_outside_stack_is_reported(self):
        cj = _cj({"web": [
            {"type": "bind", "source": "/srv/appdata", "target": "/data"},
            {"type": "bind", "source": "/opt/app/html", "target": "/html"},
        ]})
        self.assertEqual(compose.find_external_binds(cj, self.STACK), ["/srv/appdata"])

    def test_prefix_sibling_is_not_inside(self):
        # /opt/app2 shares the string prefix but is NOT inside /opt/app.
        cj = _cj({"web": [{"type": "bind", "source": "/opt/app2/data", "target": "/d"}]})
        self.assertEqual(compose.find_external_binds(cj, self.STACK), ["/opt/app2/data"])

    def test_db_exclude_paths_are_skipped(self):
        cj = _cj({"db": [{"type": "bind", "source": "/srv/pgdata", "target": "/var/lib/postgresql/data"}]})
        self.assertEqual(
            compose.find_external_binds(cj, self.STACK, exclude_paths=["/srv/pgdata"]), []
        )

    def test_system_paths_are_skipped(self):
        cj = _cj({"web": [
            {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
            {"type": "bind", "source": "/etc/localtime", "target": "/etc/localtime"},
            {"type": "bind", "source": "/dev/fuse", "target": "/dev/fuse"},
            {"type": "bind", "source": "/tmp/cache", "target": "/cache"},
        ]})
        self.assertEqual(compose.find_external_binds(cj, self.STACK), ["/tmp/cache"])

    def test_only_exact_docker_socket_and_kernel_paths_are_system_binds(self):
        self.assertTrue(compose.is_system_bind_source("/run/docker.sock"))
        self.assertTrue(compose.is_system_bind_source("/var/run/docker.sock"))
        self.assertTrue(compose.is_system_bind_source("/dev/fuse"))
        self.assertTrue(compose.is_system_bind_source("/dev/net/tun"))
        self.assertFalse(compose.is_system_bind_source("/home/user/docker.sock"))
        self.assertFalse(compose.is_system_bind_source("/run/user/1000/app-data"))
        self.assertFalse(compose.is_system_bind_source("/dev/shm/application-data"))
        self.assertFalse(compose.is_system_bind_source(
            "/proc/1/root/tmp/application-data"
        ))

    def test_named_volumes_are_ignored_and_dedup(self):
        cj = _cj({
            "a": [{"type": "volume", "source": "data", "target": "/data"},
                  {"type": "bind", "source": "/srv/shared", "target": "/s"}],
            "b": [{"type": "bind", "source": "/srv/shared", "target": "/s"}],
        })
        self.assertEqual(compose.find_external_binds(cj, self.STACK), ["/srv/shared"])

    def test_socket_on_disk_is_skipped(self):
        tmp = tempfile.mkdtemp()
        try:
            fifo = os.path.join(tmp, "pipe")
            os.mkfifo(fifo)
            cj = _cj({"web": [{"type": "bind", "source": fifo, "target": "/pipe"}]})
            self.assertEqual(compose.find_external_binds(cj, self.STACK), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PortableExternalBindDescriptorTest(unittest.TestCase):
    def test_describes_only_explicitly_selected_paths(self):
        cj = _cj({
            "web": [
                {"type": "bind", "source": "/srv/selected", "target": "/data"},
                {"type": "bind", "source": "/srv/not-selected", "target": "/cache"},
            ],
        })

        descriptors = compose.describe_selected_external_binds(cj, ["/srv/selected"])

        self.assertEqual(descriptors, [{
            "service": "web", "target": "/data", "source": "/srv/selected",
        }])

    def test_one_source_can_have_several_portable_identities(self):
        cj = _cj({
            "worker": [{"type": "bind", "source": "/srv/shared", "target": "/work"}],
            "web": [{"type": "bind", "source": "/srv/shared", "target": "/data"}],
        })

        descriptors = compose.describe_selected_external_binds(cj, ["/srv/shared"])

        self.assertEqual(descriptors, [
            {"service": "web", "target": "/data", "source": "/srv/shared"},
            {"service": "worker", "target": "/work", "source": "/srv/shared"},
        ])

    def test_selected_path_missing_from_compose_fails(self):
        with self.assertRaises(util.CommandError):
            compose.describe_selected_external_binds(_cj({}), ["/srv/missing"])

    def test_selected_paths_must_be_canonical_absolute_strings(self):
        cj = _cj({"web": [
            {"type": "bind", "source": "/srv/data", "target": "/data"},
        ]})
        invalid = (
            "/srv/../srv/data", "/srv/data/", "//srv/data", "/",
            "relative/data", 123, None,
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(util.CommandError):
                compose.describe_selected_external_binds(cj, [value])
        with self.assertRaises(util.CommandError):
            compose.describe_selected_external_binds(cj, "/srv/data")

    def test_resolves_relative_to_new_stack_location_by_service_and_target(self):
        descriptors = [{
            "service": "web", "target": "/data", "source": "/opt/shared",
        }]
        target_cj = _cj({"web": [
            {"type": "bind", "source": "/srv/restored/shared", "target": "/data"},
        ]})

        self.assertEqual(
            compose.resolve_external_bind_descriptors(target_cj, descriptors),
            [("/opt/shared", "/srv/restored/shared")],
        )

    def test_resolver_ignores_new_unselected_target_binds(self):
        descriptors = [{
            "service": "web", "target": "/data", "source": "/opt/shared",
        }]
        target_cj = _cj({"web": [
            {"type": "bind", "source": "/srv/shared", "target": "/data"},
            {"type": "bind", "source": "/srv/secret", "target": "/new-secret"},
        ]})

        mappings = compose.resolve_external_bind_descriptors(target_cj, descriptors)

        self.assertEqual(mappings, [("/opt/shared", "/srv/shared")])
        self.assertNotIn("/srv/secret", [target for _source, target in mappings])

    def test_duplicate_identities_resolving_the_same_mapping_are_deduplicated(self):
        descriptors = [
            {"service": "web", "target": "/data", "source": "/opt/shared"},
            {"service": "worker", "target": "/work", "source": "/opt/shared"},
        ]
        target_cj = _cj({
            "web": [{"type": "bind", "source": "/srv/shared", "target": "/data"}],
            "worker": [{"type": "bind", "source": "/srv/shared", "target": "/work"}],
        })

        self.assertEqual(
            compose.resolve_external_bind_descriptors(target_cj, descriptors),
            [("/opt/shared", "/srv/shared")],
        )

    def test_missing_or_ambiguous_target_identity_fails_closed(self):
        descriptor = [{"service": "web", "target": "/data", "source": "/opt/data"}]
        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(_cj({}), descriptor)

        ambiguous = _cj({"web": [
            {"type": "bind", "source": "/srv/a", "target": "/data"},
            {"type": "bind", "source": "/srv/b", "target": "/data"},
        ]})
        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(ambiguous, descriptor)

    def test_conflicting_source_or_target_mappings_fail_closed(self):
        one_source_two_targets = [
            {"service": "web", "target": "/a", "source": "/opt/shared"},
            {"service": "web", "target": "/b", "source": "/opt/shared"},
        ]
        target_cj = _cj({"web": [
            {"type": "bind", "source": "/srv/a", "target": "/a"},
            {"type": "bind", "source": "/srv/b", "target": "/b"},
        ]})
        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(target_cj, one_source_two_targets)

        two_sources_one_target = [
            {"service": "web", "target": "/a", "source": "/opt/a"},
            {"service": "worker", "target": "/b", "source": "/opt/b"},
        ]
        target_cj = _cj({
            "web": [{"type": "bind", "source": "/srv/shared", "target": "/a"}],
            "worker": [{"type": "bind", "source": "/srv/shared", "target": "/b"}],
        })
        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(target_cj, two_sources_one_target)

    def test_overlapping_target_roots_fail_closed(self):
        descriptors = [
            {"service": "web", "target": "/a", "source": "/opt/a"},
            {"service": "worker", "target": "/b", "source": "/opt/b"},
        ]
        target_cj = _cj({
            "web": [{"type": "bind", "source": "/srv/data", "target": "/a"}],
            "worker": [{"type": "bind", "source": "/srv/data/child", "target": "/b"}],
        })

        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(target_cj, descriptors)

    def test_descriptor_shape_and_destination_sources_are_validated(self):
        target_cj = _cj({"web": [
            {"type": "bind", "source": "/srv/data/../data", "target": "/data"},
        ]})
        valid = {"service": "web", "target": "/data", "source": "/opt/data"}
        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(target_cj, [valid])
        for bad in (
            {"service": "web", "target": "/data"},
            dict(valid, command="PWN"),
            dict(valid, source="relative"),
            dict(valid, target="relative"),
            "not-an-object",
        ):
            with self.subTest(bad=bad), self.assertRaises(util.CommandError):
                compose.resolve_external_bind_descriptors(_cj({}), [bad])

        with self.assertRaises(util.CommandError):
            compose.resolve_external_bind_descriptors(_cj({}), {"not": "a list"})


class AnonymousVolumeTest(unittest.TestCase):
    def test_anonymous_volume_is_skipped(self):
        # '- /data' (no source) has no stable name → not in the tar plan, no crash.
        cj = _cj({"web": [{"type": "volume", "target": "/data"}]})
        excl, vols = compose.collect_volume_backup_plan(cj, [])
        self.assertEqual(vols, [])
        self.assertEqual(excl, [])


class ExtraPathsRunTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)
        # macOS exposes /var as a symlink to /private/var. Production restores
        # deliberately reject symlinked host-path ancestors, so use the physical
        # temp path in these placement tests.
        self.tmp = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_extra_path_fails_loud(self):
        cfg = {"extra_backup_paths": [os.path.join(self.tmp, "gone")]}
        with self.assertRaises(util.CommandError):
            run_cmd._extra_paths(cfg)

    def test_existing_extra_path_is_returned(self):
        p = os.path.join(self.tmp, "data")
        os.makedirs(p)
        self.assertEqual(run_cmd._extra_paths({"extra_backup_paths": [p]}), [p])

    def test_empty_config_yields_no_paths(self):
        self.assertEqual(run_cmd._extra_paths({}), [])


class RestoreExtraPathsTest(unittest.TestCase):
    """Semantics: missing → restored; guarded production force → clean replace."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.scratch = os.path.join(self.tmp, "scratch")
        # external path INSIDE the tmp sandbox (absolute, like a real bind source)
        self.ext = os.path.join(self.tmp, "srv", "appdata")
        restored = self.scratch.rstrip("/") + self.ext
        os.makedirs(restored)
        with open(os.path.join(restored, "f.txt"), "w") as f:
            f.write("from-snapshot")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return {"extra_backup_paths": [self.ext]}

    def test_missing_path_is_restored(self):
        restore_cmd._restore_extra_paths(self._cfg(), self.scratch, force=False)
        with open(os.path.join(self.ext, "f.txt")) as f:
            self.assertEqual(f.read(), "from-snapshot")

    def test_existing_path_untouched_without_force(self):
        os.makedirs(self.ext)
        with open(os.path.join(self.ext, "f.txt"), "w") as f:
            f.write("live-data")
        restore_cmd._restore_extra_paths(self._cfg(), self.scratch, force=False)
        with open(os.path.join(self.ext, "f.txt")) as f:
            self.assertEqual(f.read(), "live-data")

    def test_existing_path_clean_replaced_with_force(self):
        os.makedirs(self.ext)
        with open(os.path.join(self.ext, "f.txt"), "w") as f:
            f.write("live-data")
        with open(os.path.join(self.ext, "keep.txt"), "w") as f:
            f.write("keep")
        scratch_fd = restore_cmd._open_absolute_dir_fd(self.scratch, create=False)
        try:
            # The tool is Linux-only in production; macOS CI has no
            # /proc/self/fdinfo, so emulate one shared mount for this ordinary
            # clean-replacement path. Dedicated tests cover mount-ID mismatch
            # and unavailable-identity failure.
            with mock.patch.object(restore_cmd, "_mount_id_for_fd", return_value=1):
                restore_cmd._restore_extra_paths(
                    self._cfg(), self.scratch, force=True, scratch_fd=scratch_fd,
                )
        finally:
            os.close(scratch_fd)
        with open(os.path.join(self.ext, "f.txt")) as f:
            self.assertEqual(f.read(), "from-snapshot")
        self.assertFalse(os.path.exists(os.path.join(self.ext, "keep.txt")))

    def test_path_not_in_snapshot_is_skipped(self):
        cfg = {"extra_backup_paths": [os.path.join(self.tmp, "other")]}
        restore_cmd._restore_extra_paths(cfg, self.scratch, force=False)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "other")))

    def test_restored_external_root_symlink_is_rejected(self):
        shutil.rmtree(self.scratch)
        os.makedirs(os.path.dirname(self.scratch.rstrip("/") + self.ext))
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, self.scratch.rstrip("/") + self.ext)

        with self.assertRaises(util.CommandError):
            restore_cmd._restore_extra_paths(self._cfg(), self.scratch, force=True)

    def test_selected_source_can_be_relocated_to_target_compose_path(self):
        relocated = os.path.join(self.tmp, "new-server", "data")

        targets = restore_cmd._restore_extra_paths(
            self._cfg(), self.scratch, force=False,
            mappings=[(self.ext, relocated)],
        )

        self.assertEqual(targets, [relocated])
        with open(os.path.join(relocated, "f.txt")) as f:
            self.assertEqual(f.read(), "from-snapshot")


if __name__ == "__main__":
    unittest.main()
