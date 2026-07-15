from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import _support  # noqa: F401

from docker_backup import compose, util
from docker_backup.commands import restore as restore_cmd


def _proc(stdout=""):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


class PrepareVolumeForRestoreTest(unittest.TestCase):
    @staticmethod
    def _identity(name="restored_data"):
        return {
            "Name": name,
            "Driver": "local",
            "Mountpoint": "/var/lib/docker/volumes/%s/_data" % name,
            "CreatedAt": "2026-07-15T00:00:00Z",
            "Scope": "local",
            "Options": None,
            "Labels": None,
        }

    def test_existing_volume_without_force_is_refused(self):
        with mock.patch.object(
                 compose, "volume_identity", return_value=self._identity(),
             ), \
             mock.patch.object(compose, "volume_container_ids", return_value=[]), \
             mock.patch.object(compose.util, "run") as run:
            with self.assertRaises(util.CommandError) as raised:
                compose.prepare_volume_for_restore("restored_data", force=False)

        self.assertIn("already exists", raised.exception.stderr)
        run.assert_not_called()

    def test_referenced_volume_is_refused_even_with_force(self):
        container_id = "a" * 64
        with mock.patch.object(
                 compose, "volume_identity", return_value=self._identity(),
             ), \
             mock.patch.object(
                 compose, "volume_container_ids", return_value=[container_id],
             ), \
             mock.patch.object(compose.util, "run") as run:
            with self.assertRaises(util.CommandError) as raised:
                compose.prepare_volume_for_restore("restored_data", force=True)

        self.assertIn("still referenced by container", raised.exception.stderr)
        run.assert_not_called()

    def test_force_recreates_volume_and_verifies_random_ownership_label(self):
        token = "0123456789abcdef" * 2
        label = "%s=%s" % (compose._RESTORE_VOLUME_TOKEN_LABEL, token)

        def docker_run(argv, **_kwargs):
            if argv[1:3] == ["volume", "inspect"]:
                return _proc(json.dumps({compose._RESTORE_VOLUME_TOKEN_LABEL: token}))
            return _proc()

        with mock.patch.object(
                 compose, "volume_identity", return_value=self._identity(),
             ), \
             mock.patch.object(
                 compose, "volume_container_ids", side_effect=[[], []],
             ) as users, \
             mock.patch.object(
                 compose.secrets, "token_hex", return_value=token,
             ) as token_hex, \
             mock.patch.object(compose.util, "run", side_effect=docker_run) as run:
            compose.prepare_volume_for_restore("restored_data", force=True)

        token_hex.assert_called_once_with(16)
        self.assertEqual(users.call_args_list, [
            mock.call("restored_data"),
            mock.call("restored_data"),
        ])
        self.assertEqual(run.call_args_list, [
            mock.call(
                ["docker", "volume", "rm", "restored_data"],
                mutating=True, capture=True,
            ),
            mock.call(
                [
                    "docker", "volume", "create", "--driver", "local",
                    "--label", label,
                    "restored_data",
                ],
                mutating=True, capture=True,
            ),
            mock.call(
                [
                    "docker", "volume", "inspect", "--format",
                    "{{json .Labels}}", "restored_data",
                ],
                capture=True,
            ),
        ])

    def test_concurrently_created_unowned_volume_is_refused(self):
        token = "fedcba9876543210" * 2

        def docker_run(argv, **_kwargs):
            if argv[1:3] == ["volume", "inspect"]:
                return _proc(json.dumps({compose._RESTORE_VOLUME_TOKEN_LABEL: "other"}))
            return _proc()

        with mock.patch.object(compose, "volume_identity", return_value=None), \
             mock.patch.object(compose, "volume_container_ids") as users, \
             mock.patch.object(compose.secrets, "token_hex", return_value=token), \
             mock.patch.object(compose.util, "run", side_effect=docker_run) as run:
            with self.assertRaises(util.CommandError) as raised:
                compose.prepare_volume_for_restore("restored_data", force=False)

        self.assertIn("appeared concurrently", raised.exception.stderr)
        self.assertIn("authenticated labels", raised.exception.stderr)
        users.assert_not_called()
        self.assertFalse(any(call.args[0][1:3] == ["volume", "rm"]
                             for call in run.call_args_list))


class NamedVolumePreflightTest(unittest.TestCase):
    @staticmethod
    def _cfg(service="app", source="data", target="/srv/data"):
        return {
            "named_volumes": [
                {"key": source, "service": service, "target": target},
            ],
        }

    @staticmethod
    def _compose(volume=None):
        mount = volume or {
            "type": "volume", "source": "data", "target": "/srv/data",
        }
        return {
            "services": {"app": {"volumes": [mount]}},
            "volumes": {"data": {}},
        }

    def test_authenticates_exact_service_source_and_target(self):
        cj = self._compose()
        with mock.patch.object(compose, "volume_identity", return_value=None):
            plan = restore_cmd._preflight_named_volume_restore(
                self._cfg(), cj, "restored", force=False,
            )

        self.assertEqual(plan, [
            {
                "key": "data",
                "real_name": "restored_data",
                "archive_key": "data",
                "driver": "local",
                "labels": {},
                "expected_identity": None,
            },
        ])

        invalid_configs = [
            self._cfg(service="worker"),
            self._cfg(source="other"),
            self._cfg(target="/srv/other"),
        ]
        for invalid in invalid_configs:
            with self.subTest(named_volume=invalid["named_volumes"][0]), \
                 self.assertRaisesRegex(ValueError, "authenticated Compose model"):
                restore_cmd._preflight_named_volume_restore(
                    invalid, cj, "restored", force=False,
                )

    def test_external_volume_is_refused(self):
        cj = self._compose()
        cj["volumes"]["data"] = {"external": True}

        with mock.patch.object(compose, "volume_exists") as exists:
            with self.assertRaises(util.CommandError) as raised:
                restore_cmd._preflight_named_volume_restore(
                    self._cfg(), cj, "restored", force=True,
                )

        self.assertIn("externally managed", raised.exception.stderr)
        exists.assert_not_called()

    def test_existing_volume_is_refused_without_force_during_preflight(self):
        identity = PrepareVolumeForRestoreTest._identity()
        with mock.patch.object(compose, "volume_identity", return_value=identity), \
             mock.patch.object(compose, "volume_container_ids", return_value=[]):
            with self.assertRaises(util.CommandError) as raised:
                restore_cmd._preflight_named_volume_restore(
                    self._cfg(), self._compose(), "restored", force=False,
                )

        self.assertIn("already exists", raised.exception.stderr)


if __name__ == "__main__":
    unittest.main()
