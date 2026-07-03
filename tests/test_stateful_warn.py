"""Detection of stateful services WITHOUT dump support (warned at create time)."""

from __future__ import annotations

import unittest

import _support  # noqa: F401

from docker_backup import detect


def _cj(images_by_service):
    return {"services": {s: {"image": img} for s, img in images_by_service.items()}}


class UndumpableStatefulTest(unittest.TestCase):
    def _engines(self, images_by_service):
        return {e["service"]: e["engine"]
                for e in detect.find_undumpable_stateful(_cj(images_by_service))}

    def test_mongo_redis_detected(self):
        got = self._engines({"mongo": "mongo:7", "cache": "redis:7-alpine"})
        self.assertEqual(got, {"mongo": "MongoDB", "cache": "Redis"})

    def test_registry_and_bitnami_variants(self):
        got = self._engines({
            "db": "docker.io/bitnami/mongodb:7.0",
            "search": "docker.elastic.co/elasticsearch/elasticsearch:8.13.0",
        })
        self.assertEqual(got, {"db": "MongoDB", "search": "Elasticsearch"})

    def test_dumpable_engines_not_reported(self):
        # mysql/postgres have dump support → they are NOT in this list.
        got = self._engines({"db": "postgres:16", "mysql": "mariadb:11"})
        self.assertEqual(got, {})

    def test_sidecars_not_reported(self):
        got = self._engines({
            "exporter": "oliver006/redis-exporter:latest",
            "backup": "someone/mongodb-backup:1.0",
        })
        self.assertEqual(got, {})

    def test_word_boundary_no_false_positive(self):
        # 'mongoose' must not match 'mongo'.
        got = self._engines({"app": "myorg/mongoose-server:1.0"})
        self.assertEqual(got, {})

    def test_plain_app_images_not_reported(self):
        got = self._engines({"web": "nginx:1.25", "app": "ghcr.io/acme/app:2"})
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
