from __future__ import annotations

import unittest

import _support  # noqa: F401

from docker_backup import restic, util


class ResolveExcludesTest(unittest.TestCase):
    """Proves the anchoring semantics of resolve_excludes (the MUST-VERIFY point)."""

    def test_absolute_auto_paths_passthrough(self):
        out = restic.resolve_excludes("/opt/gitlab", ["/opt/gitlab/shared/db"], [])
        self.assertEqual(out, ["/opt/gitlab/shared/db"])

    def test_relative_with_slash_anchored_to_stack(self):
        out = restic.resolve_excludes(
            "/opt/gitlab", [], ["gitlab/logs", "gitlab/data/postgresql"]
        )
        self.assertEqual(out, ["/opt/gitlab/gitlab/logs", "/opt/gitlab/gitlab/data/postgresql"])

    def test_leading_slash_anchored_at_stack_not_filesystem_root(self):
        out = restic.resolve_excludes("/opt/gitlab", [], ["/logs"])
        self.assertEqual(out, ["/opt/gitlab/logs"])  # NOT /logs

    def test_bare_name_or_glob_passes_through(self):
        # Without '/', restic matches the basename anywhere in the tree -> intentionally not anchored.
        out = restic.resolve_excludes("/opt/gitlab", [], ["*.log", "node_modules"])
        self.assertEqual(out, ["*.log", "node_modules"])

    def test_glob_with_slash_is_anchored(self):
        out = restic.resolve_excludes("/opt/gitlab", [], ["data/*.tmp"])
        self.assertEqual(out, ["/opt/gitlab/data/*.tmp"])

    def test_stack_trailing_slash_normalized(self):
        out = restic.resolve_excludes("/opt/gitlab/", [], ["gitlab/logs"])
        self.assertEqual(out, ["/opt/gitlab/gitlab/logs"])

    def test_empty_patterns_skipped(self):
        out = restic.resolve_excludes("/opt/gitlab", [], ["", "   ", "gitlab/logs"])
        self.assertEqual(out, ["/opt/gitlab/gitlab/logs"])

    def test_merges_auto_then_user(self):
        out = restic.resolve_excludes("/opt/x", ["/opt/x/db"], ["logs", "data/cache"])
        self.assertEqual(out, ["/opt/x/db", "logs", "/opt/x/data/cache"])


class ValidateExcludePatternTest(unittest.TestCase):
    def test_rejects_traversal(self):
        with self.assertRaises(util.CommandError):
            restic.validate_exclude_pattern("../etc/passwd")
        with self.assertRaises(util.CommandError):
            restic.validate_exclude_pattern("a/../../b")

    def test_rejects_empty(self):
        with self.assertRaises(util.CommandError):
            restic.validate_exclude_pattern("   ")

    def test_accepts_and_trims_normal(self):
        self.assertEqual(restic.validate_exclude_pattern("  gitlab/logs  "), "gitlab/logs")


if __name__ == "__main__":
    unittest.main()
