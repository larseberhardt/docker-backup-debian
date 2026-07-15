from __future__ import annotations

import io
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import restic, util


_SNAPSHOT_ID = "0123456789abcdef" * 4


class _FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


class ExactBackupSnapshotTest(unittest.TestCase):
    def setUp(self):
        util.set_dry_run(False)

    def tearDown(self):
        util.set_dry_run(False)

    def test_backup_streams_json_and_returns_summary_snapshot_id(self):
        lines = [
            '{"message_type":"status","percent_done":0.5}\n',
            '{"message_type":"summary","snapshot_id":"%s"}\n' % _SNAPSHOT_ID,
        ]
        output = io.StringIO()
        with mock.patch.object(
            restic.subprocess, "Popen", return_value=_FakeProcess(lines),
        ) as popen, mock.patch.object(restic.sys, "stdout", output):
            snapshot_id = restic.backup(
                "/repo", "/key", ["/opt/app"], [],
                ["docker-backup", "stack:app"],
            )

        self.assertEqual(snapshot_id, _SNAPSHOT_ID)
        self.assertEqual(output.getvalue(), "".join(lines))
        argv = popen.call_args.args[0]
        self.assertIn("--json", argv)
        self.assertEqual(argv[-1], "/opt/app")

    def test_backup_never_invents_id_when_summary_is_missing_or_invalid(self):
        for lines in (
            ['{"message_type":"status","percent_done":1}\n'],
            ['{"message_type":"summary","snapshot_id":"abcd1234"}\n'],
            ["not-json\n"],
        ):
            with self.subTest(lines=lines), mock.patch.object(
                restic.subprocess, "Popen", return_value=_FakeProcess(lines),
            ), mock.patch.object(restic.sys, "stdout", io.StringIO()):
                self.assertIsNone(restic.backup("/repo", "/key", ["/opt/app"], [], []))

    def test_backup_nonzero_exit_raises_after_streaming(self):
        process = _FakeProcess(
            ['{"message_type":"summary","snapshot_id":"%s"}\n' % _SNAPSHOT_ID],
            returncode=1,
        )
        with mock.patch.object(restic.subprocess, "Popen", return_value=process), \
                mock.patch.object(restic.sys, "stdout", io.StringIO()):
            with self.assertRaises(util.CommandError):
                restic.backup("/repo", "/key", ["/opt/app"], [], [])

    def test_snapshot_by_id_requires_exact_matching_record(self):
        payload = '[{"id":"%s","short_id":"01234567"}]' % _SNAPSHOT_ID
        with mock.patch.object(
            restic.util, "run", return_value=mock.Mock(stdout=payload),
        ) as run:
            snapshot = restic.snapshot_by_id("/repo", "/key", _SNAPSHOT_ID)

        self.assertEqual(snapshot["id"], _SNAPSHOT_ID)
        self.assertEqual(run.call_args.args[0][-1], _SNAPSHOT_ID)

        other = "f" * 64
        with mock.patch.object(
            restic.util, "run", return_value=mock.Mock(stdout=payload),
        ):
            self.assertIsNone(restic.snapshot_by_id("/repo", "/key", other))

    def test_snapshot_by_id_rejects_non_full_id_without_running_restic(self):
        with mock.patch.object(restic.util, "run") as run:
            self.assertIsNone(restic.snapshot_by_id("/repo", "/key", "01234567"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
