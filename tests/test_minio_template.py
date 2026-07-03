from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import restic, templates


class MinioTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp  # empty override directory

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def test_loads_and_validates(self):
        tmpl = templates.load("minio")  # validates; raises on error
        self.assertEqual(tmpl["name"], "minio")
        self.assertFalse(tmpl["db_autodetect"])
        self.assertIsNone(tmpl.get("hooks"))  # pure file-level backup, no hooks

    def test_detects_minio_by_image(self):
        cj = {"services": {"minio": {"image": "quay.io/minio/minio:latest"}}}
        self.assertEqual(templates.detect_template(cj), "minio")


class MinioExcludeInvariantTest(unittest.TestCase):
    """Critical MinIO metadata (.minio.sys/format.json, config, buckets) MUST NOT
    be excluded; only transient temp/multipart directories are dropped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)

    def _excludes(self):
        tmpl = templates.load("minio")
        return restic.resolve_excludes("/opt/s3", [], tmpl["exclude_patterns"])

    def test_transient_dirs_excluded(self):
        excludes = self._excludes()
        for transient in ("/opt/s3/minio-data/.minio.sys/tmp",
                          "/opt/s3/minio-data/.minio.sys/multipart"):
            self.assertIn(transient, excludes)

    def test_critical_metadata_not_excluded(self):
        excludes = self._excludes()
        for keep in ("/opt/s3/minio-data",
                     "/opt/s3/minio-data/.minio.sys",
                     "/opt/s3/minio-data/.minio.sys/format.json",
                     "/opt/s3/minio-data/.minio.sys/config",
                     "/opt/s3/minio-data/.minio.sys/buckets"):
            for e in excludes:
                self.assertNotEqual(e, keep)
                self.assertFalse(keep.startswith(e.rstrip("/") + "/"),
                                 "critical path %r falls under exclude %r" % (keep, e))


if __name__ == "__main__":
    unittest.main()
