from __future__ import annotations

import os
import tempfile
import unittest

import _support  # noqa: F401

from docker_backup import hooks, util


def _cfg_with(phase, cmd, *, cwd, on_failure=None, allowed=True):
    cfg = {
        "name": "t",
        "stack_path": cwd,
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        "hooks_allowed": False,
        "hooks_fingerprint": None,
    }
    cfg["hooks"][phase] = [hooks.make_hook(cmd, phase=phase, on_failure=on_failure, cwd=cwd)]
    if allowed:
        hooks.approve(cfg)
    return cfg


class BuildHookEnvTest(unittest.TestCase):
    def test_strips_backend_secrets_keeps_context(self):
        injected = {
            "AWS_SECRET_ACCESS_KEY": "shh", "AWS_ACCESS_KEY_ID": "shh",
            "RESTIC_PASSWORD": "shh", "B2_ACCOUNT_KEY": "shh", "MY_API_TOKEN": "shh",
        }
        for k, v in injected.items():
            os.environ[k] = v
        try:
            env = hooks.build_hook_env(
                {"name": "gitlab", "stack_path": "/opt/gitlab",
                 "compose_file": "/opt/gitlab/dc.yml", "project_name": "gitlab"},
                "pre_backup",
            )
        finally:
            for k in injected:
                os.environ.pop(k, None)
        for k in injected:
            self.assertNotIn(k, env)  # no backend secret in the hook env
        self.assertIn("PATH", env)    # ordinary vars are preserved
        self.assertEqual(env["DOCKER_BACKUP_STACK_PATH"], "/opt/gitlab")
        self.assertEqual(env["DOCKER_BACKUP_NAME"], "gitlab")
        self.assertEqual(env["DOCKER_BACKUP_PHASE"], "pre_backup")


class FingerprintTest(unittest.TestCase):
    def test_changes_when_cmd_changes(self):
        a = hooks.compute_fingerprint({"pre_backup": [{"cmd": "echo a"}]})
        b = hooks.compute_fingerprint({"pre_backup": [{"cmd": "echo b"}]})
        self.assertNotEqual(a, b)

    def test_approve_sets_matching_fingerprint(self):
        cfg = {"hooks": {"pre_backup": [{"cmd": "echo a"}], "post_backup": [], "restore": []}}
        hooks.approve(cfg)
        self.assertTrue(cfg["hooks_allowed"])
        self.assertEqual(cfg["hooks_fingerprint"],
                         hooks.compute_fingerprint(cfg["hooks"]))

    def test_definition_fingerprint_covers_execution_attributes(self):
        base = {
            "pre_backup": [hooks.make_hook(
                "echo hi", phase="pre_backup", cwd="stack", timeout=10,
                on_failure="abort",
            )],
            "post_backup": [],
            "restore": [],
        }
        original = hooks.compute_definition_fingerprint(base)
        self.assertTrue(original.startswith("sha256-v1:"))
        for field, value in (
            ("cmd", "echo changed"),
            ("cwd", "/tmp"),
            ("timeout", 11),
            ("on_failure", "warn"),
        ):
            changed = {phase: [dict(h) for h in items] for phase, items in base.items()}
            changed["pre_backup"][0][field] = value
            self.assertNotEqual(
                hooks.compute_definition_fingerprint(changed), original, field
            )

    def test_definition_fingerprint_is_stable_across_dict_key_order(self):
        a = {"pre_backup": [{"cmd": "x", "cwd": "stack", "timeout": 5,
                              "on_failure": "abort"}],
             "post_backup": [], "restore": []}
        b = {"restore": [], "post_backup": [],
             "pre_backup": [{"on_failure": "abort", "timeout": 5,
                              "cwd": "stack", "cmd": "x"}]}
        self.assertEqual(
            hooks.compute_definition_fingerprint(a),
            hooks.compute_definition_fingerprint(b),
        )

    def test_revoke_clears(self):
        cfg = {"hooks": {"pre_backup": [{"cmd": "x"}]}, "hooks_allowed": True,
               "hooks_fingerprint": "abc"}
        hooks.revoke(cfg)
        self.assertFalse(cfg["hooks_allowed"])
        self.assertIsNone(cfg["hooks_fingerprint"])


class EnsureAllowedTest(unittest.TestCase):
    def test_no_hooks_ok(self):
        hooks.ensure_allowed({"name": "t", "hooks": {"pre_backup": [], "post_backup": [], "restore": []}})

    def test_unapproved_raises(self):
        cfg = {"name": "t", "hooks": {"pre_backup": [{"cmd": "echo hi"}],
                                      "post_backup": [], "restore": []},
               "hooks_allowed": False}
        with self.assertRaises(util.CommandError):
            hooks.ensure_allowed(cfg)

    def test_fingerprint_mismatch_raises(self):
        cfg = {"name": "t", "hooks": {"pre_backup": [{"cmd": "echo hi"}],
                                      "post_backup": [], "restore": []},
               "hooks_allowed": True, "hooks_fingerprint": "stale"}
        with self.assertRaises(util.CommandError):
            hooks.ensure_allowed(cfg)

    def test_approved_and_matching_ok(self):
        cfg = {"name": "t", "hooks": {"pre_backup": [{"cmd": "echo hi"}],
                                      "post_backup": [], "restore": []}}
        hooks.approve(cfg)
        hooks.ensure_allowed(cfg)  # must not raise


class RunHooksExecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        util.set_dry_run(False)

    def tearDown(self):
        util.set_dry_run(False)

    def test_success_runs_in_cwd(self):
        cfg = _cfg_with("pre_backup", "touch ran", cwd=self.tmp)
        hooks.run_hooks(cfg, "pre_backup")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "ran")))

    def test_abort_reraises(self):
        cfg = _cfg_with("pre_backup", "exit 3", cwd=self.tmp, on_failure="abort")
        with self.assertRaises(util.CommandError):
            hooks.run_hooks(cfg, "pre_backup")

    def test_warn_swallows_failure(self):
        cfg = _cfg_with("post_backup", "exit 1", cwd=self.tmp, on_failure="warn")
        hooks.run_hooks(cfg, "post_backup")  # must NOT raise

    def test_unapproved_run_raises_before_exec(self):
        cfg = _cfg_with("pre_backup", "touch shouldnotrun", cwd=self.tmp, allowed=False)
        with self.assertRaises(util.CommandError):
            hooks.run_hooks(cfg, "pre_backup")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "shouldnotrun")))

    def test_dry_run_does_not_execute(self):
        util.set_dry_run(True)
        try:
            cfg = _cfg_with("pre_backup", "touch marker", cwd=self.tmp)
            hooks.run_hooks(cfg, "pre_backup")
        finally:
            util.set_dry_run(False)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "marker")))


if __name__ == "__main__":
    unittest.main()
