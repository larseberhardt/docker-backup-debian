from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from docker_backup import config, hooks, manifest, templates, util
from docker_backup.commands import restore as restore_cmd


def _gitlab_manifest():
    tmpl = templates.load_exact("gitlab", "builtin")
    cfg = {
        "schema_version": config.SCHEMA_VERSION,
        "name": "gitlab",
        "stack_path": "/opt/gitlab",
        "compose_file": "/opt/gitlab/docker-compose.yml",
        "project_name": "gitlab",
        "db_services": [],
        "named_volumes": [],
        "exclude_paths": [],
        "extra_backup_paths": [],
        "exclude_patterns": list(tmpl.get("exclude_patterns") or []),
        "db_autodetect": False,
        "hooks": templates.to_hooks(tmpl),
        "template": templates.provenance(tmpl, source="builtin"),
        "schedule": {"input": "daily 04:00", "oncalendar": "*-*-* 04:00:00",
                     "randomized_delay_sec": 300},
        "retention": dict(tmpl.get("retention") or config.DEFAULT_RETENTION),
        "repo": "/mnt/backups/gitlab",
    }
    return manifest.derive(cfg, "1" * 64)


def _args(**kw):
    base = dict(
        dest="/opt/gitlab", from_name=None, from_repo="/mnt/backups/gitlab",
        key_file="/root/gitlab.key", bootstrap_name=None, save_config=False,
        snapshot="latest", force=False, restore_cmd=None, no_custom_restore=False,
        use_template_hooks=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class TemplateHookResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        self.key_file = os.path.join(self.tmp, "provided-gitlab.key")
        with open(self.key_file, "w") as f:
            f.write("test-restic-key\n")
        util.set_dry_run(False)

    def tearDown(self):
        util.set_dry_run(False)
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, man=None):
        return manifest.cfg_from_manifest(
            man or _gitlab_manifest(), "/mnt/backups/gitlab", "/root/gitlab.key"
        )

    def test_exact_builtin_template_loads_all_phases_after_confirmation(self):
        cfg = self._cfg()
        with mock.patch.object(restore_cmd.wizard, "confirm", return_value=True) as confirm:
            self.assertTrue(restore_cmd._apply_local_template_hooks(cfg, save_config=True))

        confirm.assert_called_once()
        self.assertFalse(confirm.call_args.kwargs["default"])
        self.assertTrue(cfg["hooks"]["pre_backup"])
        self.assertTrue(cfg["hooks"]["post_backup"])
        self.assertTrue(cfg["hooks"]["restore"])
        self.assertTrue(cfg["_template_hooks_confirmed"])
        hooks.ensure_allowed(cfg)

    def test_confirmation_decline_even_with_force_stops_before_restic(self):
        man = _gitlab_manifest()
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read", return_value=man), \
             mock.patch.object(restore_cmd, "_resolve_bootstrap_key",
                               return_value="/root/gitlab.key"), \
             mock.patch.object(restore_cmd.config, "exists", return_value=False), \
             mock.patch.object(restore_cmd.wizard, "confirm", return_value=False), \
             mock.patch.object(restore_cmd.restic, "restore") as restic_restore:
            rc = restore_cmd.cmd_restore(_args(use_template_hooks=True, force=True))

        self.assertEqual(rc, 1)
        restic_restore.assert_not_called()

    def test_metadata_never_auto_loads_without_explicit_flag(self):
        util.set_dry_run(True)
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read", return_value=_gitlab_manifest()), \
             mock.patch.object(restore_cmd.restic, "restore") as restic_restore:
            rc = restore_cmd.cmd_restore(_args())

        self.assertEqual(rc, 1)
        restic_restore.assert_not_called()

    def test_hash_mismatch_fails_closed(self):
        man = _gitlab_manifest()
        man["template"]["hooks_fingerprint"] = "sha256-v1:" + "0" * 64
        cfg = self._cfg(man)
        with mock.patch.object(restore_cmd.wizard, "confirm") as confirm:
            self.assertFalse(restore_cmd._apply_local_template_hooks(cfg, save_config=False))
        confirm.assert_not_called()
        self.assertEqual(cfg["hooks"]["restore"], [])

    def test_version_mismatch_fails_closed(self):
        man = _gitlab_manifest()
        man["template"]["version"] = "999"
        cfg = self._cfg(man)
        with mock.patch.object(restore_cmd.wizard, "confirm") as confirm:
            self.assertFalse(restore_cmd._apply_local_template_hooks(cfg, save_config=False))
        confirm.assert_not_called()

    def test_malformed_descriptor_with_shell_field_is_rejected(self):
        man = _gitlab_manifest()
        man["template"]["cmd"] = "PWN_SENTINEL"
        cfg = self._cfg(man)

        self.assertFalse(restore_cmd._apply_local_template_hooks(cfg, save_config=False))
        self.assertEqual(cfg["hooks"]["restore"], [])

    def test_legacy_manifest_requires_manual_restore_command(self):
        man = _gitlab_manifest()
        man["manifest_schema_version"] = 4
        man.pop("template")
        cfg = self._cfg(man)

        self.assertFalse(restore_cmd._apply_local_template_hooks(cfg, save_config=False))

    def test_builtin_binding_ignores_malicious_operator_override(self):
        man = _gitlab_manifest()
        os.makedirs(templates.override_dir(), exist_ok=True)
        malicious = copy.deepcopy(templates.load_exact("gitlab", "builtin"))
        malicious["hooks"]["restore"][0]["cmd"] = "PWN_SENTINEL"
        with open(os.path.join(templates.override_dir(), "gitlab.json"), "w") as f:
            json.dump(malicious, f)
        cfg = self._cfg(man)

        with mock.patch.object(restore_cmd.wizard, "confirm", return_value=True):
            self.assertTrue(restore_cmd._apply_local_template_hooks(cfg, save_config=False))

        self.assertNotIn("PWN_SENTINEL", json.dumps(cfg["hooks"]))

    def test_reconstruction_uses_only_full_manifest_bound_snapshot(self):
        cfg = self._cfg()
        bound = "1" * 64

        self.assertEqual(
            restore_cmd._bound_reconstruction_snapshot(cfg, "latest"), bound
        )
        self.assertEqual(
            restore_cmd._bound_reconstruction_snapshot(cfg, bound), bound
        )
        self.assertIsNone(
            restore_cmd._bound_reconstruction_snapshot(cfg, "2" * 64)
        )

    def test_stored_db_password_blocks_config_persistence_preflight(self):
        cfg = self._cfg()
        cfg["key_file"] = self.key_file
        cfg["db_services"] = [{
            "service": "db", "engine": "postgres", "password_source": "stored",
        }]

        self.assertFalse(restore_cmd._preflight_saved_config(cfg, "gitlab"))

    def test_save_config_rejects_tampered_template_owned_schedule(self):
        cfg = self._cfg()
        cfg["schedule"]["input"] = "daily 04:00\n[Unit]\nWants=pwn.service"

        with mock.patch.object(restore_cmd.wizard, "confirm") as confirm:
            self.assertFalse(restore_cmd._apply_local_template_hooks(
                cfg, save_config=True,
            ))

        confirm.assert_not_called()


class RestoredMysqlScopeTest(unittest.TestCase):
    def test_future_seed_comes_from_restored_compose_not_snapshot_import_list(self):
        db_services = [{
            "service": "db", "engine": "mysql", "auth_user": "root",
            "all_databases": False,
            "databases": ["snipeit", "later_deleted_audit"],
        }]
        compose_model = {"services": {"db": {
            "image": "mariadb:10.7",
            "environment": {
                "MYSQL_ROOT_PASSWORD": "secret",
                "MYSQL_DATABASE": "snipeit",
            },
        }}}

        restore_cmd._enable_target_mysql_scope(db_services, compose_model)

        self.assertEqual(db_services[0]["databases"], ["snipeit"])
        self.assertTrue(db_services[0]["all_databases"])
        self.assertEqual(db_services[0]["database_scope"], "non-system")

    def test_seedless_restored_compose_keeps_dynamic_scope_without_required_seed(self):
        db_services = [{
            "service": "db", "engine": "mysql", "auth_user": "root",
            "all_databases": False, "databases": ["snapshot_database"],
        }]
        compose_model = {"services": {"db": {
            "image": "mariadb:10.7",
            "environment": {"MARIADB_ROOT_PASSWORD": "secret"},
        }}}

        restore_cmd._enable_target_mysql_scope(db_services, compose_model)

        self.assertEqual(db_services[0]["databases"], [])
        self.assertTrue(db_services[0]["all_databases"])
        self.assertEqual(db_services[0]["database_scope"], "non-system")


class TemplateRestoreSaveFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["DOCKER_BACKUP_ETC"] = self.tmp
        self.key_file = os.path.join(self.tmp, "provided-gitlab.key")
        with open(self.key_file, "w") as f:
            f.write("test-restic-key\n")
        util.set_dry_run(False)

    def tearDown(self):
        util.set_dry_run(False)
        os.environ.pop("DOCKER_BACKUP_ETC", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _command_patches(self, run_result=0):
        return (
            mock.patch.object(restore_cmd.util, "require_root"),
            mock.patch.object(restore_cmd.manifest, "read", return_value=_gitlab_manifest()),
            mock.patch.object(restore_cmd, "_resolve_bootstrap_key",
                              return_value=self.key_file),
            mock.patch.object(restore_cmd.config, "exists", return_value=False),
            mock.patch.object(restore_cmd.wizard, "confirm", return_value=True),
            mock.patch.object(restore_cmd, "_run_restore", return_value=run_result),
            mock.patch.object(restore_cmd, "_save_restored_config"),
        )

    def test_save_happens_only_after_success_with_all_approved_hooks(self):
        patches = self._command_patches(run_result=0)
        mocks = [p.start() for p in patches]
        try:
            rc = restore_cmd.cmd_restore(_args(use_template_hooks=True, save_config=True))
        finally:
            for p in reversed(patches):
                p.stop()

        self.assertEqual(rc, 0)
        run_mock, save_mock = mocks[-2], mocks[-1]
        run_mock.assert_called_once()
        save_mock.assert_called_once()
        cfg = save_mock.call_args.args[0]
        self.assertTrue(cfg["hooks"]["pre_backup"])
        self.assertTrue(cfg["hooks"]["post_backup"])
        self.assertTrue(cfg["hooks"]["restore"])
        hooks.ensure_allowed(cfg)

    def test_failed_restore_never_saves_config(self):
        patches = self._command_patches(run_result=1)
        mocks = [p.start() for p in patches]
        try:
            rc = restore_cmd.cmd_restore(_args(use_template_hooks=True, save_config=True))
        finally:
            for p in reversed(patches):
                p.stop()

        self.assertEqual(rc, 1)
        mocks[-1].assert_not_called()

    def test_bound_snapshot_id_is_passed_to_restore(self):
        patches = self._command_patches(run_result=0)
        mocks = [p.start() for p in patches]
        try:
            rc = restore_cmd.cmd_restore(_args(
                use_template_hooks=True, snapshot="latest",
            ))
        finally:
            for p in reversed(patches):
                p.stop()

        self.assertEqual(rc, 0)
        self.assertEqual(mocks[-2].call_args.args[3], "1" * 64)

    def test_save_failure_is_reported_after_success_without_false_success(self):
        patches = self._command_patches(run_result=0)
        mocks = [p.start() for p in patches]
        mocks[-1].side_effect = OSError("schedule write failed")
        try:
            with mock.patch.object(restore_cmd.util, "error") as error:
                rc = restore_cmd.cmd_restore(_args(
                    use_template_hooks=True, save_config=True,
                ))
        finally:
            for p in reversed(patches):
                p.stop()

        self.assertEqual(rc, 1)
        self.assertIn("Application restore succeeded", error.call_args.args[0])

    def test_restore_cmd_cannot_be_saved_as_complete_backup_config(self):
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read") as read:
            rc = restore_cmd.cmd_restore(_args(
                restore_cmd="docker exec gitlab gitlab-backup restore FORCE=yes",
                save_config=True,
            ))
        self.assertEqual(rc, 1)
        read.assert_not_called()

    def test_template_mode_is_mutually_exclusive_with_file_only_mode(self):
        with mock.patch.object(restore_cmd.util, "require_root"), \
             mock.patch.object(restore_cmd.manifest, "read") as read:
            rc = restore_cmd.cmd_restore(_args(
                use_template_hooks=True, no_custom_restore=True,
            ))
        self.assertEqual(rc, 1)
        read.assert_not_called()

    def test_saved_config_is_retargeted_complete_and_timer_disabled(self):
        dest = os.path.join(self.tmp, "gitlab-restored")
        os.makedirs(dest)
        with open(os.path.join(dest, "docker-compose.yml"), "w") as f:
            f.write("services: {}\n")
        with open(os.path.join(dest, ".env"), "w") as f:
            f.write("X=1\n")
        cfg = manifest.cfg_from_manifest(
            _gitlab_manifest(), "/mnt/backups/gitlab", self.key_file
        )
        with mock.patch.object(restore_cmd.wizard, "confirm", return_value=True):
            self.assertTrue(restore_cmd._apply_local_template_hooks(cfg, save_config=True))

        cj = {"name": "gitlab-restored", "services": {}, "volumes": {}}
        with mock.patch.object(restore_cmd.config, "exists", return_value=False), \
             mock.patch.object(restore_cmd.compose, "config_json", return_value=cj), \
             mock.patch.object(restore_cmd.config, "save_new", return_value="/etc/cfg") as save, \
             mock.patch.object(restore_cmd.systemd_units, "validate_oncalendar",
                               return_value=True), \
             mock.patch.object(restore_cmd.systemd_units, "write_schedule_dropin") as dropin, \
             mock.patch.object(restore_cmd.systemd_units, "daemon_reload"), \
             mock.patch.object(restore_cmd.systemd_units, "disable_timer") as disable, \
             mock.patch.object(restore_cmd.systemd_units, "timer_active",
                               return_value="inactive"), \
             mock.patch.object(restore_cmd.systemd_units, "timer_enabled",
                               return_value="disabled"), \
             mock.patch.object(restore_cmd.systemd_units, "enable_timer") as enable:
            restore_cmd._save_restored_config(cfg, "gitlab", dest)

        saved = save.call_args.args[0]
        self.assertEqual(saved["stack_path"], dest)
        self.assertEqual(saved["compose_file"], os.path.join(dest, "docker-compose.yml"))
        self.assertEqual(saved["staging_dir"], os.path.join(dest, ".docker-backup"))
        self.assertEqual(saved["db_scope_version"], config.DB_SCOPE_VERSION)
        self.assertEqual(saved["mount_check"], "/mnt/backups/gitlab")
        self.assertEqual(saved["key_file"], os.path.join(self.tmp, "keys", "gitlab.key"))
        self.assertEqual(saved["env_files"], [os.path.join(dest, ".env")])
        self.assertEqual(saved["schedule"]["input"], "daily 04:00")
        self.assertNotIn("\n", saved["schedule"]["oncalendar"])
        self.assertEqual(set(saved["template"]), {"name", "version", "source"})
        self.assertTrue(saved["hooks"]["pre_backup"])
        self.assertTrue(saved["hooks"]["post_backup"])
        self.assertTrue(saved["hooks"]["restore"])
        hooks.ensure_allowed(saved)
        dropin.assert_called_once()
        self.assertEqual(disable.call_count, 3)
        enable.assert_not_called()

    def test_active_stale_timer_aborts_before_config_publication(self):
        dest = os.path.join(self.tmp, "gitlab-restored")
        os.makedirs(dest)
        cfg = manifest.cfg_from_manifest(
            _gitlab_manifest(), "/mnt/backups/gitlab", self.key_file
        )
        with mock.patch.object(restore_cmd.wizard, "confirm", return_value=True):
            self.assertTrue(restore_cmd._apply_local_template_hooks(cfg, save_config=True))

        cj = {"name": "gitlab", "services": {}, "volumes": {}}
        with mock.patch.object(restore_cmd.config, "exists", return_value=False), \
             mock.patch.object(restore_cmd.compose, "config_json", return_value=cj), \
             mock.patch.object(restore_cmd.systemd_units, "validate_oncalendar",
                               return_value=True), \
             mock.patch.object(restore_cmd.systemd_units, "disable_timer"), \
             mock.patch.object(restore_cmd.systemd_units, "timer_active",
                               return_value="active"), \
             mock.patch.object(restore_cmd.systemd_units, "timer_enabled",
                               return_value="enabled"), \
             mock.patch.object(restore_cmd.systemd_units, "write_schedule_dropin") as dropin, \
             mock.patch.object(restore_cmd.config, "save") as save:
            with self.assertRaises(util.CommandError):
                restore_cmd._save_restored_config(cfg, "gitlab", dest)

        dropin.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
