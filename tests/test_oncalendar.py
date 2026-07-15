from __future__ import annotations

import unittest

import _support  # noqa: F401  (puts repo root on sys.path)

from docker_backup import systemd_units as su


class OnCalendarTest(unittest.TestCase):
    def test_mappings(self):
        cases = {
            "daily 03:00": "*-*-* 03:00:00",
            "daily 7:5": "*-*-* 07:05:00",
            "": "*-*-* 03:00:00",
            "hourly": "*-*-* *:00:00",
            "weekly": "Mon *-*-* 03:00:00",
            "weekly Mon 04:30": "Mon *-*-* 04:30:00",
            "weekly sunday 05:00": "Sun *-*-* 05:00:00",
            "monthly": "*-*-01 03:00:00",
            "monthly 02:15": "*-*-01 02:15:00",
        }
        for inp, expected in cases.items():
            self.assertEqual(su.oncalendar_from(inp), expected, "input=%r" % inp)

    def test_custom_passthrough(self):
        self.assertEqual(su.oncalendar_from("custom *-*-* 01:23:45"), "*-*-* 01:23:45")
        self.assertEqual(su.oncalendar_from("custom Mon,Fri 06:00"), "Mon,Fri 06:00")

    def test_render_dropin_clears_inherited(self):
        out = su.render_dropin("*-*-* 03:00:00", randomized_delay_sec=120)
        self.assertIn("[Timer]", out)
        # empty OnCalendar first -> clears the inherited value, then the real value
        self.assertIn("OnCalendar=\nOnCalendar=*-*-* 03:00:00\n", out)
        self.assertIn("RandomizedDelaySec=120", out)

    def test_render_dropin_rejects_directive_injection(self):
        with self.assertRaises(ValueError):
            su.render_dropin("*-*-* 03:00:00\nUnit=pwn.service")

    def test_render_dropin_rejects_invalid_delay(self):
        with self.assertRaises(ValueError):
            su.render_dropin("*-*-* 03:00:00", randomized_delay_sec=999999)

    def test_write_dropin(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DOCKER_BACKUP_SYSTEMD_DIR"] = tmp
            try:
                path = su.write_schedule_dropin("xibo", "*-*-* 03:00:00")
                self.assertTrue(os.path.exists(path))
                self.assertTrue(path.endswith("docker-backup@xibo.timer.d/schedule.conf"))
                with open(path) as f:
                    self.assertIn("OnCalendar=*-*-* 03:00:00", f.read())
            finally:
                del os.environ["DOCKER_BACKUP_SYSTEMD_DIR"]


if __name__ == "__main__":
    unittest.main()
