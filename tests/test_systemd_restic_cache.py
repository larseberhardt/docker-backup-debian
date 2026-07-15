from __future__ import annotations

import os
import unittest

import _support


class SystemdResticCacheTest(unittest.TestCase):
    def _read(self, *parts: str) -> str:
        with open(os.path.join(_support.REPO_ROOT, *parts)) as f:
            return f.read()

    def test_all_restic_timer_services_have_managed_cache(self):
        for unit in ("docker-backup@.service", "docker-backup-check.service"):
            with self.subTest(unit=unit):
                text = self._read("systemd", unit)
                self.assertIn("CacheDirectory=docker-backup/restic", text)
                self.assertIn("CacheDirectoryMode=0700", text)
                self.assertIn(
                    "Environment=RESTIC_CACHE_DIR=/var/cache/docker-backup/restic",
                    text,
                )

    def test_installer_copies_both_units_before_daemon_reload(self):
        text = self._read("install.sh")
        backup_copy = text.index("$SRC_DIR/systemd/docker-backup@.service")
        check_copy = text.index("$SRC_DIR/systemd/docker-backup-check.service")
        reload_call = text.index("systemctl daemon-reload")
        self.assertLess(backup_copy, reload_call)
        self.assertLess(check_copy, reload_call)


if __name__ == "__main__":
    unittest.main()
