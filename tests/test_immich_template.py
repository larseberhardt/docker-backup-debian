from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import detect, restic, templates


class ImmichTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("immich")
        self.assertEqual(tmpl["name"], "immich")
        self.assertFalse(tmpl["db_autodetect"])  # pgvecto-rs is not detected
        self.assertIsNone(tmpl.get("hooks"))

    def test_detects_immich_by_app_image(self):
        cj = {"services": {
            "immich-server": {"image": "ghcr.io/immich-app/immich-server:release"},
            "database": {"image": "docker.io/tensorchord/pgvecto-rs:pg14-v0.2.0"},
            "redis": {"image": "docker.io/valkey/valkey:8-bookworm"}}}
        self.assertEqual(templates.detect_template(cj), "immich")

    def test_pgvecto_rs_is_not_autodetected(self):
        # Rationale for db_autodetect=false: the image name 'pgvecto-rs' contains
        # no Postgres token, so the generic detection intentionally does not apply.
        cj = {"services": {"database": {"image": "docker.io/tensorchord/pgvecto-rs:pg14-v0.2.0"}}}
        self.assertEqual(detect.find_db_services(cj), [])


class ImmichExcludeInvariantTest(unittest.TestCase):
    """Only regenerable derivatives (thumbs, encoded-video) are excluded;
    originals/uploads/profile and the live Postgres directory stay in the backup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _excludes(self):
        tmpl = templates.load("immich")
        return restic.resolve_excludes("/opt/immich", [], tmpl["exclude_patterns"])

    def test_derivatives_excluded(self):
        ex = self._excludes()
        self.assertIn("/opt/immich/library/thumbs", ex)
        self.assertIn("/opt/immich/library/encoded-video", ex)

    def test_originals_and_db_kept(self):
        ex = self._excludes()
        for keep in ("/opt/immich/library/library",   # originals
                     "/opt/immich/library/upload",
                     "/opt/immich/library/profile",
                     "/opt/immich/postgres"):          # live DB directory (fallback)
            for e in ex:
                self.assertNotEqual(e, keep)
                self.assertFalse(keep.startswith(e.rstrip("/") + "/"),
                                 "protected path %r falls under exclude %r" % (keep, e))


if __name__ == "__main__":
    unittest.main()
