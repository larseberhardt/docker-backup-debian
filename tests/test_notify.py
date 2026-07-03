from __future__ import annotations

import os
import stat
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import notify, util


def _cfg(on_failure=True, on_success=False, enabled=True, to=None):
    return {
        "enabled": enabled,
        "method": "smtp",
        "on_failure": on_failure,
        "on_success": on_success,
        "smtp": {
            "host": "smtp.example.com",
            "port": 587,
            "security": "starttls",
            "username": "u",
            "password": "secret-pw",
            "from": "backup@example.com",
            "to": to if to is not None else ["admin@example.com"],
            "timeout": 30,
        },
    }


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        util.set_dry_run(True)  # never actually send in the test

    def tearDown(self):
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        os.environ.pop("DOCKER_BACKUP_SMTP_PASSWORD", None)
        util.set_dry_run(False)

    def test_load_absent_returns_none(self):
        self.assertIsNone(notify.load())

    def test_save_roundtrip_and_mode_0600(self):
        path = notify.save(_cfg())
        self.assertTrue(path.endswith("notify.json"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        loaded = notify.load()
        self.assertEqual(loaded["smtp"]["host"], "smtp.example.com")
        self.assertEqual(loaded["smtp"]["to"], ["admin@example.com"])

    def test_send_skipped_when_disabled(self):
        self.assertFalse(notify.send("s", "b", _cfg(enabled=False)))

    def test_send_skipped_without_recipients(self):
        self.assertFalse(notify.send("s", "b", _cfg(to=[])))

    def test_send_dry_run_ok(self):
        self.assertTrue(notify.send("s", "b", _cfg()))

    def test_failure_respects_flag(self):
        notify.save(_cfg(on_failure=True))
        self.assertTrue(notify.notify_failure("xibo"))
        notify.save(_cfg(on_failure=False))
        self.assertFalse(notify.notify_failure("xibo"))

    def test_success_respects_flag(self):
        notify.save(_cfg(on_success=False))
        self.assertFalse(notify.notify_success("xibo", "summary"))
        notify.save(_cfg(on_success=True))
        self.assertTrue(notify.notify_success("xibo", "summary"))

    def test_password_env_override(self):
        os.environ["DOCKER_BACKUP_SMTP_PASSWORD"] = "env-pw"
        self.assertEqual(notify._smtp_password(_cfg()["smtp"]), "env-pw")

    def test_password_inline_fallback(self):
        self.assertEqual(notify._smtp_password(_cfg()["smtp"]), "secret-pw")

    def test_recipients_accepts_comma_string(self):
        self.assertEqual(
            notify._recipients({"to": "a@x.de, b@x.de"}),
            ["a@x.de", "b@x.de"],
        )


if __name__ == "__main__":
    unittest.main()
