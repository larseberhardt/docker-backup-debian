"""Batch tests for the bundled app templates (headscale, mailcow, paperless, …)."""

from __future__ import annotations

import unittest

import _support  # noqa: F401

from docker_backup import detect, templates

NEW = ["headscale", "invoiceninja", "mailcow", "n8n", "nextcloud-aio",
       "nocodb", "paperless", "plausible", "registry", "uptime-kuma", "yourls"]


def _cj(*images):
    return {"services": {"svc%d" % i: {"image": img} for i, img in enumerate(images)}}


class NewTemplatesValidateTest(unittest.TestCase):
    def test_all_load_and_validate(self):
        for name in NEW:
            t = templates.load(name)  # load() runs validate()
            self.assertEqual(t["name"], name)
            self.assertTrue(t.get("description"))
            self.assertTrue((t.get("match") or {}).get("image_tokens"))

    def test_none_ship_root_hooks(self):
        # No template needs --allow-hooks: SQL via autodetect, mongo/redis via quiesce.
        for name in NEW:
            self.assertFalse(templates.load(name).get("hooks"), name)

    def test_only_aio_disables_db_autodetect(self):
        for name in NEW:
            expected = name != "nextcloud-aio"
            self.assertEqual(templates.load(name)["db_autodetect"], expected, name)

    def test_paperless_excludes_pgbackups_sidecar_output(self):
        from docker_backup import restic
        tmpl = templates.load("paperless")
        excludes = restic.resolve_excludes("/opt/paperless", [], tmpl["exclude_patterns"])
        # anchored at the stack root — a 'pgbackups' dir elsewhere is untouched
        self.assertEqual(excludes, ["/opt/paperless/pgbackups"])


class DetectTemplateTest(unittest.TestCase):
    def test_matches(self):
        cases = {
            "headscale": _cj("headscale/headscale:0.23"),
            "yourls": _cj("yourls:1.9", "mariadb:11"),
            "nocodb": _cj("nocodb/nocodb:latest", "postgres:16"),
            "uptime-kuma": _cj("louislam/uptime-kuma:1"),
            "invoiceninja": _cj("invoiceninja/invoiceninja:5", "mariadb:11", "nginx:1.25"),
            "n8n": _cj("docker.n8n.io/n8nio/n8n:1.44"),
            "paperless": _cj("ghcr.io/paperless-ngx/paperless-ngx:2.8",
                             "postgres:16", "redis:7"),
            "plausible": _cj("ghcr.io/plausible/community-edition:v2.1",
                             "clickhouse/clickhouse-server:24.3", "postgres:16"),
            "mailcow": _cj("ghcr.io/mailcow/dovecot:2.0", "mariadb:10.11",
                           "redis:7-alpine"),
            "registry": _cj("registry:2"),
        }
        for expected, cj in cases.items():
            self.assertEqual(templates.detect_template(cj), expected)

    def test_aio_beats_generic_nextcloud(self):
        # Both tokens match the substring; the LONGER (more specific) must win —
        # the two stacks need completely different backup treatment.
        self.assertEqual(templates.detect_template(_cj("nextcloud/all-in-one:latest")),
                         "nextcloud-aio")
        self.assertEqual(templates.detect_template(
            _cj("ghcr.io/nextcloud-releases/all-in-one:latest")), "nextcloud-aio")
        self.assertEqual(templates.detect_template(_cj("nextcloud:29-apache", "mariadb:11")),
                         "nextcloud")

    def test_registry_token_does_not_match_private_registry_hosts(self):
        # Images pulled FROM a private registry contain 'registry.' but not 'registry:'.
        cj = _cj("registry.example.com/website:latest", "mariadb:11")
        self.assertIsNone(templates.detect_template(cj))


class TemplateEngineInteractionTest(unittest.TestCase):
    """The templates rely on generic detection — make sure it actually fires."""

    def test_paperless_stack_engines(self):
        cj = {"services": {
            "webserver": {"image": "ghcr.io/paperless-ngx/paperless-ngx:2.8"},
            "db": {"image": "postgres:16", "environment": {"POSTGRES_PASSWORD": "x"}},
            "broker": {"image": "redis:7"},
        }}
        self.assertEqual([d["service"] for d in detect.find_db_services(cj)], ["db"])
        self.assertEqual([(q["service"], q["engine"])
                          for q in detect.find_quiesce_services(cj)], [("broker", "redis")])

    def test_plausible_clickhouse_is_flagged_stateful(self):
        cj = {"services": {
            "plausible": {"image": "ghcr.io/plausible/community-edition:v2.1"},
            "plausible_db": {"image": "postgres:16", "environment": {"POSTGRES_PASSWORD": "x"}},
            "plausible_events_db": {"image": "clickhouse/clickhouse-server:24.3"},
        }}
        stateful = {s["service"]: s["engine"] for s in detect.find_undumpable_stateful(cj)}
        self.assertEqual(stateful, {"plausible_events_db": "ClickHouse"})
        self.assertEqual([d["service"] for d in detect.find_db_services(cj)],
                         ["plausible_db"])

    def test_mailcow_mysql_and_redis_covered(self):
        cj = {"services": {
            "mysql-mailcow": {"image": "mariadb:10.11",
                              "environment": {"MYSQL_ROOT_PASSWORD": "x"}},
            "redis-mailcow": {"image": "redis:7-alpine"},
            "dovecot-mailcow": {"image": "ghcr.io/mailcow/dovecot:2.0"},
        }}
        self.assertEqual([d["service"] for d in detect.find_db_services(cj)],
                         ["mysql-mailcow"])
        self.assertEqual([q["service"] for q in detect.find_quiesce_services(cj)],
                         ["redis-mailcow"])


if __name__ == "__main__":
    unittest.main()
