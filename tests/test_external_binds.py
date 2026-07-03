"""External bind mounts: detection (create), fail-loud (run), restore semantics."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

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
            {"type": "bind", "source": "/dev/ttyUSB0", "target": "/dev/ttyUSB0"},
            {"type": "bind", "source": "/tmp/cache", "target": "/cache"},
        ]})
        self.assertEqual(compose.find_external_binds(cj, self.STACK), [])

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
        self.tmp = tempfile.mkdtemp()

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
    """Semantics: missing → restored; existing → only merged with --force."""

    def setUp(self):
        util.set_dry_run(False)
        self.tmp = tempfile.mkdtemp()
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

    def test_existing_path_merged_with_force(self):
        os.makedirs(self.ext)
        with open(os.path.join(self.ext, "f.txt"), "w") as f:
            f.write("live-data")
        with open(os.path.join(self.ext, "keep.txt"), "w") as f:
            f.write("keep")
        restore_cmd._restore_extra_paths(self._cfg(), self.scratch, force=True)
        with open(os.path.join(self.ext, "f.txt")) as f:
            self.assertEqual(f.read(), "from-snapshot")
        self.assertTrue(os.path.exists(os.path.join(self.ext, "keep.txt")))

    def test_path_not_in_snapshot_is_skipped(self):
        cfg = {"extra_backup_paths": [os.path.join(self.tmp, "other")]}
        restore_cmd._restore_extra_paths(cfg, self.scratch, force=False)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "other")))


if __name__ == "__main__":
    unittest.main()
