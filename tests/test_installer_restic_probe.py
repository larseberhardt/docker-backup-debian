from __future__ import annotations

import os
import unittest

import _support


class InstallerResticProbeTest(unittest.TestCase):
    def test_unpacked_binary_is_executable_before_the_probe(self):
        # bunzip2/python write the binary without the mode bit; probing it first
        # failed with "Permission denied" and every restic upgrade was discarded.
        with open(os.path.join(_support.REPO_ROOT, "install.sh")) as f:
            text = f.read()
        unpack = text.rindex('"$tmp/$file" "$tmp/restic"')
        chmod = text.index('chmod 0755 "$tmp/restic"')
        probe = text.index('"$tmp/restic" version')
        self.assertLess(unpack, chmod)
        self.assertLess(chmod, probe)


if __name__ == "__main__":
    unittest.main()
