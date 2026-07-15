"""``restore <dest>`` — full restore incl. DB import.

End state: the stack is fully stopped, the data is in place — only
``docker compose up -d`` is still needed.
"""

from __future__ import annotations

import copy
import datetime
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .. import (compose, config, dbdump, detect, hooks, keys, manifest, quiesce,
                restic, runtime, systemd_units, templates, util, volumes, wizard)

_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


@dataclass
class _RestoreScratch:
    path: str
    fd: int
    parent_fd: int
    name: str

    def __fspath__(self) -> str:
        return self.path

    def __str__(self) -> str:
        return self.path


def cmd_restore(args) -> int:
    util.require_root()
    dest = os.path.abspath(args.dest)
    snapshot = args.snapshot or "latest"
    restore_cmd = getattr(args, "restore_cmd", None)
    no_custom_restore = getattr(args, "no_custom_restore", False)
    use_template_hooks = getattr(args, "use_template_hooks", False)
    save_config = getattr(args, "save_config", False)
    from_repo = getattr(args, "from_repo", None)

    selected_modes = sum(bool(v) for v in (restore_cmd, no_custom_restore, use_template_hooks))
    if selected_modes > 1:
        util.error(
            "--restore-cmd, --use-template-hooks and --no-custom-restore are "
            "mutually exclusive."
        )
        return 1
    if use_template_hooks and not from_repo:
        util.error("--use-template-hooks only applies with --from-repo.")
        return 1
    if save_config and not from_repo:
        util.error("--save-config only applies with --from-repo.")
        return 1
    if save_config and restore_cmd:
        util.error(
            "Refusing --save-config with --restore-cmd: a restore-only command "
            "does not reconstruct the pre/post hooks needed for future backups. "
            "Use --use-template-hooks with a bound template instead."
        )
        return 1
    if save_config and not use_template_hooks:
        util.error(
            "--save-config requires --use-template-hooks. A complete unattended "
            "backup config is reconstructed only from an exact local template; "
            "the plaintext repository manifest is not an authentication source."
        )
        return 1

    if from_repo:
        # Bootstrap: without a local config, directly from a repo on the drive.
        cfg, name = _bootstrap_cfg(args, dest)
        if cfg is None:
            return 1
    else:
        # Classic path: load the config from /etc/docker-backup.
        if args.from_name:
            name = config.sanitize_name(args.from_name)
        else:
            name = config.sanitize_name(os.path.basename(dest.rstrip("/")))
        if not config.exists(name):
            util.error("No backup config '%s'. Use --from <name> or --from-repo <path>." % name)
            return 1
        cfg = config.load(name)

    manifest_schema = cfg.get("_manifest_schema_version") if from_repo else None
    if from_repo and (
            (isinstance(manifest_schema, int)
             and not isinstance(manifest_schema, bool)
             and manifest_schema >= manifest.MANIFEST_SCHEMA_VERSION)
            or use_template_hooks or save_config):
        selected = _bound_reconstruction_snapshot(cfg, snapshot)
        if selected is None:
            return 1
        snapshot = selected

    if (from_repo and isinstance(manifest_schema, int)
            and not isinstance(manifest_schema, bool)
            and manifest_schema >= manifest.MANIFEST_SCHEMA_VERSION
            and selected_modes == 0):
        util.error(
            "A v%d cross-server manifest is plaintext and cannot authenticate "
            "whether an application restore hook was required. Choose an "
            "explicit restore policy: --use-template-hooks, a reviewed "
            "--restore-cmd, or --no-custom-restore for the built-in DB/file path."
            % manifest.MANIFEST_SCHEMA_VERSION
        )
        return 1

    if save_config:
        schema = cfg.get("_manifest_schema_version")
        if not isinstance(schema, int) or schema != manifest.MANIFEST_SCHEMA_VERSION:
            util.error(
                "This repository does not have the supported v%d reconstruction "
                "manifest. Restore without --save-config and recreate the config "
                "locally, or take one fresh backup with the updated tool."
                % manifest.MANIFEST_SCHEMA_VERSION
            )
            return 1
        if config.exists(name):
            util.error(
                "Local config '%s' already exists; refusing to overwrite it during "
                "restore. Use --name <new-name> or remove/review the existing config first."
                % name
            )
            return 1
        if cfg.get("_template_descriptor_error"):
            util.error(
                "Refusing --save-config because the manifest template descriptor is "
                "invalid: %s" % cfg["_template_descriptor_error"]
            )
            return 1
        if not _preflight_saved_config(cfg, name):
            return 1

    if use_template_hooks and not _apply_local_template_hooks(cfg, save_config=save_config):
        return 1

    # Pin the exact bytes used by restic before a potentially long restore. The
    # caller-supplied file may otherwise be replaced between repository access
    # and post-restore config publication. An orphaned managed key after a failed
    # application restore is harmless; publishing a config with a different key
    # is not.
    if save_config and not util.DRY_RUN:
        try:
            cfg["key_file"] = keys.install_existing_key(name, cfg["key_file"])
        except OSError as exc:
            util.error("Cannot pin the supplied restic key safely: %s" % exc)
            return 1

    # Custom restore command passed directly on the CLI (mainly for --from-repo, where
    # the manifest deliberately carries NO shell). It is still displayed and confirmed
    # by _custom_restore before execution.
    if restore_cmd:
        cfg.setdefault("hooks", {"pre_backup": [], "post_backup": [], "restore": []})
        cfg["hooks"]["restore"] = [hooks.make_hook(restore_cmd, phase="restore")]
        hooks.approve(cfg)

    if (cfg.get("hooks_present") and not use_template_hooks
            and not restore_cmd and not no_custom_restore):
        util.error(
            "This backup used hooks, but the manifest contains no executable shell. "
            "Choose --use-template-hooks, provide a reviewed --restore-cmd, or "
            "explicitly choose --no-custom-restore."
        )
        return 1

    if (cfg.get("custom_restore_required")
            and not no_custom_restore
            and not hooks.phase_hooks(cfg, "restore")):
        util.error(
            "This backup requires an application-specific restore command, but the "
            "cross-server manifest deliberately contains no shell. Re-run with a "
            "matching local template via --use-template-hooks, provide "
            "--restore-cmd '<reviewed command>', or explicitly use "
            "--no-custom-restore to restore files only."
        )
        return 1

    rc = _run_restore(
        cfg, name, dest, snapshot, args.force,
        no_custom_restore=no_custom_restore, save_config=save_config,
    )
    if rc != 0:
        return rc
    if save_config:
        if util.DRY_RUN:
            util.info("DRY-RUN: would save the reconstructed config '%s' after success." % name)
        else:
            restored_dest_fd = cfg.pop("_restored_dest_fd", None)
            restored_compose_fd = cfg.pop("_restored_compose_fd", None)
            restored_external_fds = cfg.pop("_restored_external_fds", [])
            try:
                _save_restored_config(
                    cfg, name, dest, dest_fd=restored_dest_fd,
                    compose_fd=restored_compose_fd,
                    external_fds=restored_external_fds,
                )
            except (OSError, ValueError, util.CommandError) as exc:
                util.error(
                    "Application restore succeeded, but the backup config was NOT "
                    "saved safely: %s. The restored application data remains at %s."
                    % (exc, dest)
                )
                return 1
            finally:
                if restored_dest_fd is not None:
                    os.close(restored_dest_fd)
                if restored_compose_fd is not None:
                    os.close(restored_compose_fd)
                _close_path_fds(restored_external_fds)
    return 0


def _bound_reconstruction_snapshot(
    cfg: Dict[str, Any], requested: str,
) -> Optional[str]:
    """Select exactly the snapshot described by a reconstruction manifest."""
    error = cfg.get("_manifest_snapshot_id_error")
    bound = cfg.get("_manifest_snapshot_id")
    if error or not bound:
        util.error(
            "This restore needs a manifest bound to one full restic snapshot "
            "id%s. Take a fresh backup with the updated tool or use a legacy "
            "manual restore without manifest-driven metadata."
            % (": " + str(error) if error else "")
        )
        return None
    if requested not in (None, "latest", bound):
        util.error(
            "The manifest describes snapshot %s, but --snapshot requested %s. "
            "Template reconstruction cannot combine metadata from a different "
            "snapshot; use the full bound id or take a fresh backup."
            % (bound, requested)
        )
        return None
    util.info("Using manifest-bound snapshot %s." % bound[:12])
    return bound


def _authenticate_manifest_snapshot(
    cfg: Dict[str, Any], snapshot: str, restore_paths: list,
) -> None:
    """Bind plaintext v5 path metadata to the immutable restic snapshot.

    The sidecar may be edited independently of the repository.  Querying the
    exact full snapshot id authenticates its path/tag metadata with restic
    before a restore target or scratch tree is touched.
    """
    if cfg.get("_manifest_schema_version", 0) < manifest.MANIFEST_SCHEMA_VERSION:
        return
    bound = cfg.get("_manifest_snapshot_id")
    if not isinstance(bound, str) or snapshot != bound:
        raise util.CommandError(
            ["restic", "snapshots", str(snapshot)], 1,
            "Restore snapshot does not match the v5 manifest binding.",
        )
    metadata = restic.snapshot_by_id(cfg["repo"], cfg["key_file"], bound)
    if not isinstance(metadata, dict) or metadata.get("id") != bound:
        raise util.CommandError(
            ["restic", "snapshots", bound], 1,
            "The exact manifest-bound snapshot could not be authenticated.",
        )

    raw_paths = metadata.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise util.CommandError(
            ["restic", "snapshots", bound], 1,
            "The exact snapshot has no valid path metadata.",
        )
    try:
        authenticated_paths = {
            _canonical_absolute_path(path, "snapshot metadata path")
            for path in raw_paths
        }
    except (TypeError, ValueError) as exc:
        raise util.CommandError(
            ["restic", "snapshots", bound], 1,
            "The exact snapshot has unsafe path metadata: %s" % exc,
        )
    expected_paths = set(restore_paths)
    if authenticated_paths != expected_paths:
        raise util.CommandError(
            ["restic", "snapshots", bound], 1,
            "Manifest stack/extra paths do not exactly match the authenticated "
            "snapshot path set.",
        )

    source_name = cfg.get("_manifest_source_name") or cfg.get("name")
    expected_tags = {"docker-backup", "stack:%s" % source_name}
    raw_tags = metadata.get("tags")
    if (not isinstance(raw_tags, list)
            or any(not isinstance(tag, str) for tag in raw_tags)
            or not expected_tags.issubset(set(raw_tags))):
        raise util.CommandError(
            ["restic", "snapshots", bound], 1,
            "The exact snapshot does not carry the expected docker-backup and "
            "stack identity tags.",
        )


def _preflight_saved_config(cfg: Dict[str, Any], name: str) -> bool:
    """Reject known-incomplete save-config inputs before restoring any data."""
    stored = [
        db.get("service", "?") for db in (cfg.get("db_services") or [])
        if isinstance(db, dict) and db.get("password_source") == "stored"
    ]
    if stored:
        util.error(
            "--save-config cannot reconstruct source-server 'stored' DB password "
            "sidecars (services: %s). Move the password into the Compose env and "
            "take a fresh backup, or restore without --save-config and configure "
            "the password locally." % ", ".join(stored)
        )
        return False

    source = cfg.get("key_file")
    if (not isinstance(source, str) or not os.path.isfile(source)
            or os.path.islink(source)):
        util.error(
            "--save-config requires --key-file to be a regular, non-symlink file; "
            "it will be copied into %s." % keys.key_path(name)
        )
        return False
    target = keys.key_path(name)
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target):
            util.error("Managed key path is not a regular file: %s" % target)
            return False
        try:
            with open(source, "rb") as src, open(target, "rb") as dst:
                same = hmac.compare_digest(src.read(16385), dst.read(16385))
        except OSError as exc:
            util.error("Cannot verify the managed restore key: %s" % exc)
            return False
        if not same:
            util.error(
                "Managed key %s already exists with different contents; refusing "
                "to replace it." % target
            )
            return False
    return True


def _apply_local_template_hooks(cfg: Dict[str, Any], *, save_config: bool) -> bool:
    """Resolve an exact local template, preview every hook, then approve it.

    The sidecar descriptor is untrusted and contains no shell. Commands come
    exclusively from the exact local ``builtin``/``operator`` source selected by
    the descriptor; its hash is a compatibility binding, not an authentication
    mechanism. The default-no human preview is therefore mandatory even with
    ``--force``.
    """
    if cfg.get("_manifest_schema_version") != manifest.MANIFEST_SCHEMA_VERSION:
        util.error(
            "Trusted template reconstruction requires a v%d manifest. Take a fresh "
            "backup with the updated tool or use a reviewed --restore-cmd."
            % manifest.MANIFEST_SCHEMA_VERSION
        )
        return False
    descriptor_error = cfg.get("_template_descriptor_error")
    ref = cfg.get("template")
    if descriptor_error:
        util.error(
            "Cannot use local template hooks: %s. Use a reviewed --restore-cmd "
            "instead." % descriptor_error
        )
        return False
    if not isinstance(ref, dict):
        util.error(
            "This manifest has no bound template identity/hash. Take a fresh backup "
            "with the updated tool, or use a reviewed --restore-cmd for this restore."
        )
        return False

    try:
        tmpl = templates.load_exact(ref["name"], ref["source"])
    except (KeyError, OSError, util.CommandError) as exc:
        util.error("Cannot load the exact local template: %s" % exc)
        return False

    local_provenance = templates.provenance(tmpl, source=ref["source"])
    if tmpl.get("name") != ref.get("name"):
        util.error(
            "Local template file identity mismatch: expected '%s', file declares '%s'."
            % (ref.get("name"), tmpl.get("name"))
        )
        return False
    if local_provenance.get("version") != ref.get("version"):
        util.error(
            "Local template version mismatch for '%s' (manifest %s, local %s). "
            "Use a matching template or a reviewed --restore-cmd."
            % (ref.get("name"), ref.get("version"), local_provenance.get("version"))
        )
        return False

    local_hooks = templates.to_hooks(tmpl)
    local_fingerprint = hooks.compute_definition_fingerprint(local_hooks)
    if not hmac.compare_digest(local_fingerprint, ref.get("hooks_fingerprint", "")):
        util.error(
            "Local template hook mismatch for '%s'. The source config was customized "
            "or the template changed. Refusing reconstruction; use the matching "
            "template or a reviewed --restore-cmd."
            % ref.get("name")
        )
        return False
    local_has_hooks = hooks.has_commands({"hooks": local_hooks})
    if local_has_hooks != bool(ref.get("hooks_present")):
        util.error("Template hook marker mismatch; refusing reconstruction.")
        return False
    if cfg.get("custom_restore_required") and not hooks.phase_hooks(
            {"hooks": local_hooks}, "restore"):
        util.error(
            "The manifest requires an application restore hook, but local template "
            "'%s' has none." % ref.get("name")
        )
        return False
    if save_config:
        mismatch = _template_config_mismatch(cfg, tmpl)
        if mismatch:
            util.error(
                "Refusing --save-config because source template-owned settings do "
                "not match the exact local template (%s). Restore without "
                "--save-config and recreate/review the customized config manually."
                % mismatch
            )
            return False

    commands = hooks.describe_commands({"hooks": local_hooks})
    util.warn(
        "Exact local template selected: %s v%s (%s)."
        % (ref["name"], ref["version"], ref["source"])
    )
    if commands:
        util.warn("These locally installed commands can run as ROOT:")
        for phase in hooks.PHASES:
            for hook in hooks.phase_hooks({"hooks": local_hooks}, phase):
                util.warn("    [%s] %s" % (phase, hook["cmd"]))
                util.warn(
                    "        cwd=%s  timeout=%ss  on_failure=%s"
                    % (
                        hook.get("cwd", "stack"),
                        hook.get("timeout") or (7200 if phase == "restore" else 3600),
                        hook.get("on_failure") or (
                            "warn" if phase == "post_backup" else "abort"
                        ),
                    )
                )
        prompt = "Use these template commands for this restore"
        if save_config:
            prompt += " and approve them in the saved backup config"
        if not util.DRY_RUN and not wizard.confirm(prompt + "?", default=False):
            util.warn("Template hook reconstruction not confirmed; nothing was restored.")
            return False

    cfg["hooks"] = local_hooks
    hooks.approve(cfg)
    cfg["template"] = local_provenance
    cfg["hooks_present"] = local_has_hooks
    cfg["_template_hooks_confirmed"] = True
    cfg["_resolved_template"] = copy.deepcopy(tmpl)
    return True


def _template_config_mismatch(
    cfg: Dict[str, Any], tmpl: Dict[str, Any],
) -> Optional[str]:
    """Prevent silent loss of source overrides during strict reconstruction."""
    actual_patterns = cfg.get("exclude_patterns") or []
    expected_patterns = tmpl.get("exclude_patterns") or []
    if actual_patterns != expected_patterns:
        return "exclude_patterns differ"
    if bool(cfg.get("db_autodetect", True)) != bool(tmpl.get("db_autodetect", True)):
        return "db_autodetect differs"

    schedule = cfg.get("schedule") or {}
    actual_schedule = schedule.get("input") if isinstance(schedule, dict) else None
    expected_schedule = tmpl.get("schedule") or config.DEFAULT_SCHEDULE_INPUT
    if actual_schedule != expected_schedule:
        return "schedule differs"

    def normalized_retention(raw: Any) -> Dict[str, Any]:
        value = raw if isinstance(raw, dict) else {}
        return {
            "daily": value.get("daily", config.DEFAULT_RETENTION["daily"]),
            "weekly": value.get("weekly", config.DEFAULT_RETENTION["weekly"]),
            "monthly": value.get("monthly", config.DEFAULT_RETENTION["monthly"]),
            "keep_within": value.get("keep_within"),
        }

    if normalized_retention(cfg.get("retention")) != normalized_retention(
            tmpl.get("retention") or config.DEFAULT_RETENTION):
        return "retention differs"
    return None


def _enable_target_mysql_scope(
    db_services: list, cj: Dict[str, Any],
) -> None:
    """Rebuild future MySQL scope from the authenticated target Compose model.

    The manifest database list is the exact import plan for one snapshot.  It
    must not become a permanent required-seed list: an additional database may
    legitimately be deleted after the restore.  Only Compose-declared seeds are
    retained; every other visible user database is discovered at backup time.
    """
    detected_db_services = {
        item["service"]: item for item in detect.find_db_services(cj)
    }
    for db in db_services:
        if db.get("engine") != "mysql":
            continue
        current = detected_db_services.get(db.get("service"))
        if not current or current.get("engine") != "mysql":
            raise util.CommandError(
                ["--save-config", "db-detection"], 1,
                "Cannot re-detect MySQL service '%s' in restored Compose."
                % db.get("service"),
            )
        creds = detect.extract_credentials(
            current.get("environment"), "mysql", current.get("flavor"),
        )
        # Rollback-safe persisted representation: current versions resolve the
        # marker to an exact non-system list before dumping; v1.0.3 ignores the
        # marker and therefore over-includes system schemas instead of silently
        # omitting user databases created after this Compose seed.
        db["all_databases"] = True
        db["databases"] = list((creds or {}).get("databases") or [])
        db["database_scope"] = "non-system"


def _save_restored_config(
    cfg: Dict[str, Any], name: str, dest: str, *, dest_fd: Optional[int] = None,
    compose_fd: Optional[int] = None, external_fds: Optional[list] = None,
) -> None:
    """Persist a complete target-local config only after a successful restore."""
    if dest_fd is not None:
        _assert_path_matches_fd(dest, dest_fd)
    for target, target_fd in external_fds or []:
        _assert_path_matches_fd(target, target_fd)
    if config.exists(name):  # race-safe repeat of the preflight check
        raise util.CommandError(
            ["--save-config"], 1,
            "Local config '%s' appeared during restore; refusing to overwrite it." % name,
        )

    operation_dest = _fd_host_path(dest_fd) if dest_fd is not None else dest
    compose_name = os.path.basename(cfg["compose_file"])
    compose_file = os.path.join(dest, compose_name)
    expected_compose_digest = cfg.get("_restored_compose_digest")
    if compose_fd is not None:
        if dest_fd is None:
            raise ValueError("A retained Compose file requires its destination FD.")
        _assert_open_entry(dest_fd, compose_name, compose_fd)
        if (not isinstance(expected_compose_digest, str)
                or not hmac.compare_digest(
                    expected_compose_digest, _fd_sha256(compose_fd))):
            raise ValueError("Restored Compose file contents changed before config save.")
        operation_compose = _fd_host_path(compose_fd)
    else:
        operation_compose = os.path.join(operation_dest, compose_name)
    project_hint = cfg.get("_restored_project_name") or _target_project_name(dest)
    trusted_model = cfg.get("_restored_compose_model")
    if isinstance(trusted_model, dict):
        cj = copy.deepcopy(trusted_model)
    else:
        # Compatibility path for direct/local callers. Cross-server --save-config
        # always supplies the model parsed in the protected scratch tree.
        cj = compose.config_json(
            operation_compose, operation_dest, project_name=project_hint,
        )
    if compose_fd is not None:
        _assert_open_entry(dest_fd, compose_name, compose_fd)
    if operation_dest != dest:
        cj = _rebase_operation_paths(cj, operation_dest, dest)
    project = cj.get("name") or project_hint
    db_services = copy.deepcopy(cfg.get("db_services") or [])
    # These fields describe the source Compose layout. Recompute them against the
    # restored target; setdefault() in collect_volume_backup_plan must not retain a
    # stale source bind after a relative path or volume type changed.
    for db in db_services:
        db.pop("raw_data_exclude", None)
        db.pop("data_dir_target", None)
    exclude_paths, named_volumes = compose.collect_volume_backup_plan(cj, db_services)
    # Only selected source binds that were resolved and explicitly confirmed for
    # this target may enter future backups. Never auto-add every bind Compose sees.
    extra_paths = list(cfg.get("_restored_extra_backup_paths") or [])

    resolved_template = cfg.get("_resolved_template")
    if not isinstance(resolved_template, dict):
        raise util.CommandError(
            ["--save-config"], 1,
            "Exact local template metadata was not retained for config reconstruction.",
        )
    db_autodetect = bool(resolved_template.get("db_autodetect", True))
    if db_autodetect:
        # Re-enable the current local source-side policy for future MySQL
        # backups. Required seeds come only from the restored Compose model
        # authenticated inside the encrypted snapshot, never from the mutable
        # plaintext manifest's exact one-snapshot import list.
        _enable_target_mysql_scope(db_services, cj)
    schedule_input = resolved_template.get("schedule") or config.DEFAULT_SCHEDULE_INPUT
    if (not isinstance(schedule_input, str) or not schedule_input.strip()
            or len(schedule_input) > 200
            or any(c in schedule_input for c in ("\r", "\n", "\0"))):
        schedule_input = config.DEFAULT_SCHEDULE_INPUT
    oncalendar = systemd_units.oncalendar_from(schedule_input)
    if not systemd_units.validate_oncalendar(oncalendar):
        util.warn(
            "Restored schedule %r is not valid locally; using %s instead."
            % (schedule_input, config.DEFAULT_SCHEDULE_INPUT)
        )
        schedule_input = config.DEFAULT_SCHEDULE_INPUT
        oncalendar = systemd_units.oncalendar_from(schedule_input)
    schedule = {
        "input": schedule_input,
        "oncalendar": oncalendar,
        "randomized_delay_sec": 300,
    }

    template_ref = cfg.get("template")
    template_provenance = None
    if isinstance(template_ref, dict) and all(
            isinstance(template_ref.get(k), str) for k in ("name", "version", "source")):
        template_provenance = {
            "name": template_ref["name"],
            "version": template_ref["version"],
            "source": template_ref["source"],
        }

    hooks_dict = copy.deepcopy(
        cfg.get("hooks") or {"pre_backup": [], "post_backup": [], "restore": []}
    )
    saved = {
        "schema_version": config.SCHEMA_VERSION,
        "db_scope_version": config.DB_SCOPE_VERSION,
        "name": name,
        "stack_path": dest,
        "compose_file": compose_file,
        "project_name": project,
        "env_files": [
            _rebase_operation_path(path, operation_dest, dest)
            for path in compose.find_env_files(operation_dest)
        ],
        "db_services": db_services,
        "named_volumes": named_volumes,
        "repo": cfg["repo"],
        "offsite": None,
        "backend_env_file": os.path.join(config.backends_dir(), name + ".env"),
        "key_file": None,  # installed atomically below, before the config is published
        "exclude_paths": exclude_paths,
        "extra_backup_paths": extra_paths,
        "quiesce_services": _target_quiesce_services(cj),
        "quiesce_disabled": False,
        # Template-owned safe defaults come from the reviewed local template,
        # not from the mutable plaintext sidecar.
        "exclude_patterns": list(resolved_template.get("exclude_patterns") or []),
        "db_autodetect": db_autodetect,
        "hooks": hooks_dict,
        "hooks_allowed": bool(cfg.get("hooks_allowed", False)),
        "hooks_fingerprint": cfg.get("hooks_fingerprint"),
        "template": template_provenance,
        "schedule": schedule,
        "retention": copy.deepcopy(
            resolved_template.get("retention") or config.DEFAULT_RETENTION
        ),
        "offsite_retention": None,
        "offsite_prune": True,
        "staging_dir": os.path.join(dest, ".docker-backup"),
        "mount_check": cfg["repo"] if compose.is_local_repo(cfg["repo"]) else None,
        "created": datetime.datetime.now(datetime.timezone.utc).replace(
            microsecond=0
        ).isoformat(),
    }
    hooks.ensure_allowed(saved)
    config.ensure_dirs()
    with util.FileLock(config.mutation_lock_path(name)):
        if config.exists(name):
            raise util.CommandError(
                ["--save-config"], 1,
                "Local config '%s' appeared during restore; refusing to overwrite it."
                % name,
            )
        saved["key_file"] = keys.install_existing_key(name, cfg["key_file"])

        # A stale unit with this name may exist even when no JSON config does.
        # Prove it stopped before publishing, repeat after daemon-reload, and
        # verify once more after atomic no-replace publication. All mutations of
        # this config name share the lock, closing the create/enable race.
        _disable_and_verify_timer(name)
        systemd_units.write_schedule_dropin(
            name, schedule["oncalendar"], schedule["randomized_delay_sec"]
        )
        systemd_units.daemon_reload()
        _disable_and_verify_timer(name)

        published = False
        try:
            path = config.save_new(saved)
            published = True
            _disable_and_verify_timer(name)
            if dest_fd is not None:
                _assert_path_matches_fd(dest, dest_fd)
            if compose_fd is not None:
                _assert_open_entry(dest_fd, compose_name, compose_fd)
                if not hmac.compare_digest(
                        expected_compose_digest, _fd_sha256(compose_fd)):
                    raise OSError(
                        errno.ESTALE, "Restored Compose file contents changed",
                        compose_name,
                    )
            for target, target_fd in external_fds or []:
                _assert_path_matches_fd(target, target_fd)
        except Exception:
            # No approved-hook config may remain if the final disabled-state
            # proof fails. The restored application and managed key stay intact.
            if published:
                config.delete(name)
            raise
    util.info("Reconstructed config saved after successful restore: %s" % path)
    util.info(
        "Schedule installed and timer disabled for review. Enable it when ready: "
        "systemctl enable --now docker-backup@%s.timer" % name
    )


def _rebase_operation_path(value: str, operation_root: str, canonical_root: str) -> str:
    if value == operation_root:
        return canonical_root
    prefix = operation_root.rstrip("/") + "/"
    if value.startswith(prefix):
        return canonical_root.rstrip("/") + "/" + value[len(prefix):]
    return value


def _rebase_operation_paths(value: Any, operation_root: str, canonical_root: str):
    """Replace the temporary /proc FD root in parsed Compose metadata."""
    if isinstance(value, str):
        return _rebase_operation_path(value, operation_root, canonical_root)
    if isinstance(value, list):
        return [
            _rebase_operation_paths(item, operation_root, canonical_root)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _rebase_operation_paths(item, operation_root, canonical_root)
            for key, item in value.items()
        }
    return value


def _rebase_scratch_path(
    value: str, project_root: str, mirror_root: str, canonical_project: str,
    original_project: Optional[str] = None,
) -> str:
    """Map a path in the protected restic mirror to target-relative semantics.

    A Compose source such as ``../shared`` is normalized beneath the scratch-root
    FD, not beneath the stack FD. Relativizing it back from the scratch project and
    applying that same suffix to the selected target preserves parent-relative
    layouts on a different server/path.
    """
    if value == mirror_root or value.startswith(mirror_root.rstrip("/") + "/"):
        relative = os.path.relpath(value, project_root)
        rebased = os.path.normpath(os.path.join(canonical_project, relative))
        return _canonical_absolute_path(rebased, "rebased Compose path")
    if (original_project is not None and (
            value == original_project
            or value.startswith(original_project.rstrip("/") + "/"))):
        return _rebase_operation_path(value, original_project, canonical_project)
    return value


def _rebase_scratch_paths(
    value: Any, project_root: str, mirror_root: str, canonical_project: str,
    original_project: Optional[str] = None,
):
    if isinstance(value, str):
        return _rebase_scratch_path(
            value, project_root, mirror_root, canonical_project, original_project,
        )
    if isinstance(value, list):
        return [
            _rebase_scratch_paths(
                item, project_root, mirror_root, canonical_project, original_project,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _rebase_scratch_paths(
                item, project_root, mirror_root, canonical_project, original_project,
            )
            for key, item in value.items()
        }
    return value


def _protected_compose_environment() -> Dict[str, str]:
    """Environment for scratch Compose normalization: restored files only.

    ``docker compose`` still reads the protected project's own ``.env`` file.
    Returning a new empty mapping prevents every ambient variable (including
    backup credentials) from taking precedence during interpolation.
    """
    return {}


def _pin_compose_file_artifacts(
    compose_model: Dict[str, Any], scratch_fd: int, project_root: str,
    mirror_root: str, canonical_project: str, original_project: str,
) -> list:
    """Copy Compose config/secret files from protected scratch into sealed FDs."""
    retained = []
    pinned = {}  # type: Dict[str, tuple]
    try:
        for section_name in ("configs", "secrets"):
            section = compose_model.get(section_name) or {}
            if not isinstance(section, dict):
                raise ValueError("Compose %s must be an object." % section_name)
            for item in section.values():
                if not isinstance(item, dict) or "file" not in item:
                    continue
                source_path = item.get("file")
                if not isinstance(source_path, str) or not os.path.isabs(source_path):
                    raise ValueError(
                        "Normalized Compose %s file path is not absolute."
                        % section_name[:-1]
                    )
                if (source_path == mirror_root
                        or source_path.startswith(mirror_root.rstrip("/") + "/")):
                    suffix = source_path[len(mirror_root):]
                    snapshot_path = os.path.normpath(os.path.sep + suffix.lstrip("/"))
                else:
                    snapshot_path = _canonical_absolute_path(
                        source_path, "Compose %s snapshot file" % section_name[:-1],
                    )
                target_path = _rebase_scratch_path(
                    source_path, project_root, mirror_root, canonical_project,
                    original_project,
                )
                if target_path in pinned:
                    continue
                parent_fd = source_fd = -1
                try:
                    parent_fd, filename = _open_snapshot_parent_fd(
                        scratch_fd, snapshot_path,
                    )
                    source_fd = _open_regular_at(parent_fd, filename)
                    pinned_fd, digest = _copy_fd_to_memfd(
                        source_fd, "docker-backup-compose-%s" % section_name[:-1],
                    )
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "Compose %s file is not a protected regular file in the "
                        "restored snapshot (%s): %s"
                        % (section_name[:-1], snapshot_path, exc)
                    )
                finally:
                    if source_fd >= 0:
                        os.close(source_fd)
                    if parent_fd >= 0:
                        os.close(parent_fd)
                retained.append((target_path, pinned_fd, digest))
                pinned[target_path] = (pinned_fd, digest)
        return retained
    except Exception:
        _close_trusted_file_fds(retained)
        raise


def _write_runtime_compose(
    compose_model: Dict[str, Any], operation_dest: str, canonical_dest: str,
    external_fds: list, project_name: str, *, dest_fd: Optional[int] = None,
    trusted_file_fds: Optional[list] = None,
):
    """Create an anonymous restore model whose bind sources are held descriptors.

    Containers created during application/DB restore must not resolve mutable host
    pathnames after validation. The Compose model itself stays on a retained memfd,
    so it cannot be replaced by a concurrent rename. Containers are removed before
    any of these descriptors close.
    """
    if not hasattr(os, "memfd_create"):
        raise RuntimeError(
            "Safe restore requires Linux memfd_create for the transient Compose model."
        )
    model = copy.deepcopy(compose_model)
    model["name"] = project_name
    roots = [(canonical_dest, operation_dest, dest_fd)] + [
        (target, _fd_host_path(fd), fd) for target, fd in external_fds
    ]
    roots.sort(key=lambda item: len(item[0]), reverse=True)
    retained_sources = []
    opened_sources = {}  # type: Dict[str, int]
    trusted_files = {
        path: (fd, digest) for path, fd, digest in (trusted_file_fds or [])
    }

    def selected_target_path(value):
        if not isinstance(value, str) or not value:
            return None
        candidate = value if os.path.isabs(value) else os.path.normpath(
            os.path.join(canonical_dest, value)
        )
        for canonical, _stable, _root_fd in roots:
            if candidate == canonical or candidate.startswith(canonical.rstrip("/") + "/"):
                return candidate
        return None

    def stable_source(value, expected=None, create_directory=False):
        if not isinstance(value, str):
            return value
        for canonical, stable, root_fd in roots:
            if value == canonical:
                if root_fd is not None:
                    opened_sources.setdefault(value, root_fd)
                    if expected is not None:
                        opened = os.fstat(root_fd)
                        wanted = (stat.S_ISDIR if expected == "directory"
                                  else stat.S_ISREG)
                        if not wanted(opened.st_mode):
                            raise ValueError(
                                "Runtime Compose source has the wrong type: %s" % value
                            )
                return stable
            prefix = canonical.rstrip("/") + "/"
            if value.startswith(prefix):
                relative = value[len(prefix):]
                if root_fd is None:
                    # Test/dry compatibility. Production always supplies the
                    # retained destination root and therefore takes the exact-FD
                    # branch below.
                    return stable.rstrip("/") + "/" + relative
                source_fd = opened_sources.get(value)
                if source_fd is None:
                    source_fd = _open_relative_entry_fd(
                        root_fd, relative, expected=expected,
                        create_directory=create_directory,
                    )
                    opened_sources[value] = source_fd
                    retained_sources.append((value, source_fd))
                elif expected is not None:
                    opened = os.fstat(source_fd)
                    wanted = stat.S_ISDIR if expected == "directory" else stat.S_ISREG
                    if not wanted(opened.st_mode):
                        raise ValueError(
                            "Runtime Compose source has the wrong type: %s" % value
                        )
                return _fd_host_path(source_fd)
        return value

    try:
        for service in (model.get("services") or {}).values():
            if not isinstance(service, dict):
                continue
            for unresolved in ("env_file", "label_file", "extends"):
                if service.get(unresolved):
                    raise ValueError(
                        "Normalized runtime Compose still contains unsupported %s."
                        % unresolved
                    )
            for volume in service.get("volumes") or []:
                if isinstance(volume, dict) and volume.get("type") == "bind":
                    bind_options = volume.get("bind") or {}
                    if not isinstance(bind_options, dict):
                        raise ValueError("Normalized Compose bind options are invalid.")
                    original_source = volume.get("source")
                    volume["source"] = stable_source(
                        original_source,
                        # Canonical Compose JSON represents short syntax with an
                        # empty bind object; its semantic default is to create a
                        # missing host directory. Only explicit false disables it.
                        create_directory=(
                            bind_options.get("create_host_path", True) is not False
                        ),
                    )
                    if (volume["source"] == original_source
                            and not compose.is_system_bind_source(original_source)):
                        raise ValueError(
                            "Unselected external bind source cannot be used by an "
                            "automatic restore: %s. Include it in the backup or "
                            "remove it from the restore Compose model."
                            % original_source
                        )
            for device in service.get("devices") or []:
                if (isinstance(device, dict)
                        and selected_target_path(device.get("source")) is not None):
                    raise ValueError(
                        "A service device path resolves inside restored data; "
                        "this restore shape is not supported safely."
                    )
            credential_spec = service.get("credential_spec")
            if (isinstance(credential_spec, dict)
                    and selected_target_path(credential_spec.get("file")) is not None):
                raise ValueError(
                    "A credential_spec file resolves inside restored data; "
                    "this restore shape is not supported safely."
                )
            build = service.get("build")
            if isinstance(build, dict) and "context" in build:
                build["context"] = stable_source(
                    build.get("context"), expected="directory",
                )
                additional = build.get("additional_contexts")
                if isinstance(additional, dict):
                    for key, value in list(additional.items()):
                        additional[key] = stable_source(value, expected="directory")
        if model.get("include"):
            raise ValueError("Normalized runtime Compose still contains unsupported include.")
        for volume in (model.get("volumes") or {}).values():
            if not isinstance(volume, dict):
                continue
            opts = volume.get("driver_opts") or {}
            if not isinstance(opts, dict):
                continue
            option_tokens = {
                token.strip().lower()
                for token in str(opts.get("o") or "").split(",") if token.strip()
            }
            if (str(opts.get("type") or "").lower() == "none"
                    or "bind" in option_tokens or "rbind" in option_tokens):
                raise ValueError(
                    "Bind-backed top-level volume driver_opts are not supported "
                    "by safe cross-server restore. Use a service bind mount."
                )
        for section_name in ("configs", "secrets"):
            for item in (model.get(section_name) or {}).values():
                if isinstance(item, dict) and "file" in item:
                    original = item.get("file")
                    trusted = trusted_files.get(original)
                    if trusted is not None:
                        trusted_fd, expected_digest = trusted
                        placed_path = stable_source(original, expected="file")
                        placed_fd = opened_sources.get(original)
                        if (placed_fd is None or not hmac.compare_digest(
                                expected_digest, _fd_sha256(placed_fd))):
                            raise ValueError(
                                "Placed Compose %s file changed before use: %s"
                                % (section_name[:-1], original)
                            )
                        item["file"] = _fd_host_path(trusted_fd)
                    elif dest_fd is not None and selected_target_path(original) is not None:
                        raise ValueError(
                            "Compose %s file was not bound from protected scratch: %s"
                            % (section_name[:-1], original)
                        )
                    else:
                        item["file"] = stable_source(original, expected="file")
    except Exception:
        _close_path_fds(retained_sources)
        raise

    fd = -1
    try:
        flags = (
            getattr(os, "MFD_CLOEXEC", 0)
            | getattr(os, "MFD_ALLOW_SEALING", 0)
        )
        fd = os.memfd_create("docker-backup-runtime-compose", flags)
        payload = json.dumps(
            model, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.lseek(fd, 0, os.SEEK_SET)
        _seal_memfd(fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        _close_path_fds(retained_sources)
        raise
    return fd, _fd_host_path(fd), retained_sources


def _write_project_cleanup_compose(project_name: str):
    """Create a sealed, inert Compose model for project-wide pre-placement down.

    The authenticated project name is the only restored value needed for cleanup.
    Reusing the full restored model here would make ``down`` parse bind/config/build
    sources before placement. A dummy service plus ``--remove-orphans`` removes the
    existing project containers without starting or mounting anything.
    """
    if not hasattr(os, "memfd_create"):
        raise RuntimeError(
            "Safe restore requires Linux memfd_create for project cleanup."
        )
    flags = (
        getattr(os, "MFD_CLOEXEC", 0)
        | getattr(os, "MFD_ALLOW_SEALING", 0)
    )
    fd = os.memfd_create("docker-backup-cleanup-compose", flags)
    try:
        payload = json.dumps(
            {
                "name": project_name,
                "services": {
                    "docker_backup_restore_cleanup": {"image": "scratch"},
                },
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.lseek(fd, 0, os.SEEK_SET)
        _seal_memfd(fd)
        return fd, _fd_host_path(fd)
    except Exception:
        os.close(fd)
        raise


def _target_project_name(dest: str) -> str:
    """Compose-compatible default derived only from the operator-selected target."""
    candidate = os.path.basename(dest.rstrip("/")).lower()
    candidate = re.sub(r"[^a-z0-9_-]+", "", candidate).lstrip("-_")
    if not candidate:
        raise ValueError("Restore target does not yield a valid Compose project name.")
    return candidate


def _authenticated_project_name(
    compose_model: Dict[str, Any], operation_dest: str, canonical_dest: str,
) -> str:
    """Honor a name from restored Compose, otherwise ignore the transient FD name."""
    declared = compose_model.get("name")
    transient_default = os.path.basename(operation_dest.rstrip("/"))
    if (isinstance(declared, str) and declared
            and declared != transient_default):
        return declared
    return _target_project_name(canonical_dest)


def _disable_and_verify_timer(name: str) -> None:
    systemd_units.disable_timer(name)
    active = systemd_units.timer_active(name)
    enabled = systemd_units.timer_enabled(name)
    # Fail closed: missing/unknown output is not evidence that an unattended
    # approved-hook timer is stopped. These are the explicit safe states emitted
    # by systemctl for a stopped, non-enabled unit.
    if active not in ("inactive", "failed") or enabled not in ("disabled", "masked"):
        raise util.CommandError(
            ["systemctl", "disable", "--now", "docker-backup@%s.timer" % name],
            1,
            "Timer could not be confirmed disabled (active=%r, enabled=%r)."
            % (active, enabled),
        )


def _target_quiesce_services(cj: Dict[str, Any]) -> list:
    """Re-detect fixed Mongo/Redis consistency helpers from target Compose."""
    out = []
    for found in detect.find_quiesce_services(cj):
        creds = detect.extract_quiesce_credentials(
            found["environment"], found["engine"]
        )
        entry = {
            "service": found["service"],
            "engine": found["engine"],
            "scope": quiesce.data_scope(found["volumes"], found["engine"]),
        }
        entry.update(creds)
        out.append(entry)
    return out


def _confirm_manifest_database_imports(
    cfg: Dict[str, Any], compose_json: Dict[str, Any], *, will_import: bool = True,
) -> bool:
    """Verify and authorize DB services selected by a plaintext sidecar.

    The encrypted snapshot authenticates the restored Compose file, but the
    adjacent manifest is intentionally plaintext. It may describe dump names and
    credentials, but it must not silently select an arbitrary privileged service
    for ``docker compose up/exec``. Only image-detected DB services with the same
    engine are eligible, and the operator sees the complete import plan.
    """
    dbs = cfg.get("db_services") or []
    if not will_import or not dbs or "_manifest_schema_version" not in cfg:
        return True
    detected = {
        item["service"]: item["engine"]
        for item in detect.find_db_services(compose_json)
    }
    for db in dbs:
        service = db.get("service")
        engine = db.get("engine")
        if detected.get(service) != engine:
            util.error(
                "Manifest DB service %r (%s) is not the same image-detected DB "
                "service in the restored Compose file; refusing Docker execution."
                % (service, engine)
            )
            return False

    util.warn("The plaintext manifest requests these database imports:")
    for db in dbs:
        databases = db.get("databases") or (
            ["ALL"] if db.get("all_databases") else ["default"]
        )
        util.warn(
            "    service=%s  engine=%s  user=%s  databases=%s  password=%s"
            % (
                db.get("service"), db.get("engine"), db.get("auth_user") or "default",
                ",".join(databases), db.get("password_source") or "none",
            )
        )
    if not wizard.confirm("Start these DB services and import their dumps?", default=False):
        util.warn("Database import not confirmed; no database service was started.")
        return False
    cfg["_manifest_db_import_confirmed"] = True
    return True


def _authenticate_manifest_compose_plan(
    cfg: Dict[str, Any], compose_json: Dict[str, Any],
) -> None:
    """Prove a v5 manifest did not omit DBs or archived named volumes.

    Compose comes from the encrypted snapshot; the adjacent manifest does not.
    Re-detect SQL services from authenticated images, then recompute the exact
    non-DB named-volume archive plan from that validated DB selection.
    """
    if cfg.get("_manifest_schema_version", 0) < manifest.MANIFEST_SCHEMA_VERSION:
        return

    db_autodetect = cfg.get("db_autodetect", True)
    if not isinstance(db_autodetect, bool):
        raise ValueError("db_autodetect must be a boolean")
    raw_dbs = cfg.get("db_services") or []
    if not isinstance(raw_dbs, list):
        raise ValueError("db_services must be a list")

    def db_identity(db: Any) -> tuple:
        if not isinstance(db, dict):
            raise ValueError("database service entry must be an object")
        service = db.get("service")
        engine = db.get("engine")
        if (not isinstance(service, str) or not service
                or engine not in ("mysql", "postgres")):
            raise ValueError("database service identity is incomplete")
        return service, engine

    listed_db_ids = [db_identity(db) for db in raw_dbs]
    if len(set(listed_db_ids)) != len(listed_db_ids):
        raise ValueError("database service identities must be unique")
    detected_db_ids = {
        (item["service"], item["engine"])
        for item in detect.find_db_services(compose_json)
    }
    listed_db_set = set(listed_db_ids)
    if db_autodetect:
        if listed_db_set != detected_db_ids:
            raise util.CommandError(
                ["restore", "manifest-compose-plan"], 1,
                "Manifest database services do not exactly match SQL services "
                "detected in the authenticated Compose model.",
            )
    elif not listed_db_set.issubset(detected_db_ids):
        raise util.CommandError(
            ["restore", "manifest-compose-plan"], 1,
            "A manifest database service is not present with the same engine "
            "in the authenticated Compose model.",
        )

    db_copy = copy.deepcopy(raw_dbs)
    for db in db_copy:
        db.pop("raw_data_exclude", None)
        db.pop("data_dir_target", None)
    _raw_db_excludes, expected_named = compose.collect_volume_backup_plan(
        compose_json, db_copy,
    )

    def volume_identity(item: Any) -> tuple:
        if not isinstance(item, dict):
            raise ValueError("named volume entry must be an object")
        identity = tuple(
            item.get(field) for field in ("key", "real_name", "target", "service")
        )
        if any(not isinstance(value, str) or not value for value in identity):
            raise ValueError("named volume identity is incomplete")
        return identity

    configured_named = cfg.get("named_volumes") or []
    if not isinstance(configured_named, list):
        raise ValueError("named_volumes must be a list")
    configured_ids = [volume_identity(item) for item in configured_named]
    expected_ids = [volume_identity(item) for item in expected_named]
    if (len(set(configured_ids)) != len(configured_ids)
            or len(configured_ids) != len(expected_ids)
            or set(configured_ids) != set(expected_ids)):
        raise util.CommandError(
            ["restore", "manifest-compose-plan"], 1,
            "Manifest named volumes do not exactly match the non-database "
            "volume archive plan from the authenticated Compose model.",
        )

    extras = cfg.get("extra_backup_paths") or []
    expected_descriptors = compose.describe_selected_external_binds(
        compose_json, extras,
    )
    configured_descriptors = cfg.get("external_bind_descriptors")
    if not isinstance(configured_descriptors, list):
        raise ValueError("external_bind_descriptors must be a list")

    def descriptor_identity(item: Any) -> tuple:
        if (not isinstance(item, dict)
                or set(item) != {"service", "target", "source"}):
            raise ValueError("external bind descriptor identity is incomplete")
        identity = (
            item.get("service"), item.get("target"), item.get("source"),
        )
        if any(not isinstance(value, str) or not value for value in identity):
            raise ValueError("external bind descriptor identity is incomplete")
        return identity

    configured_descriptor_ids = [
        descriptor_identity(item) for item in configured_descriptors
    ]
    expected_descriptor_ids = [
        descriptor_identity(item) for item in expected_descriptors
    ]
    if (len(set(configured_descriptor_ids)) != len(configured_descriptor_ids)
            or len(configured_descriptor_ids) != len(expected_descriptor_ids)
            or set(configured_descriptor_ids) != set(expected_descriptor_ids)):
        raise util.CommandError(
            ["restore", "manifest-compose-plan"], 1,
            "Manifest external bind descriptors do not exactly match the "
            "authenticated source Compose identities.",
        )


def _authenticate_manifest_artifacts(
    cfg: Dict[str, Any], scratch_fd: int, stack_path: str,
) -> None:
    """Require an exact set of encrypted dump/archive artifacts for v5.

    This is the authenticated evidence that a plaintext sidecar did not turn
    off DB autodetection and then omit the logical DB, or omit/add a named
    volume archive.  Backup staging is recreated empty for every run, so no
    unclaimed file is legitimate in either directory.
    """
    if cfg.get("_manifest_schema_version", 0) < manifest.MANIFEST_SCHEMA_VERSION:
        return

    expected_dumps = []
    for db in cfg.get("db_services") or []:
        service = db.get("service")
        engine = db.get("engine")
        if engine == "mysql":
            expected_dumps.append("%s.sql" % service)
        elif engine == "postgres":
            if db.get("dump_globals"):
                expected_dumps.append(dbdump.globals_filename(service))
            databases = db.get("databases") or [dbdump._MAINT_DB]
            expected_dumps.extend(
                dbdump.db_filename(service, database) for database in databases
            )
        else:
            raise ValueError("unsupported database engine in artifact plan")
    expected_archives = [
        volumes.archive_name(item.get("key"))
        for item in (cfg.get("named_volumes") or [])
    ]
    if (len(set(expected_dumps)) != len(expected_dumps)
            or len(set(expected_archives)) != len(expected_archives)):
        raise ValueError("manifest artifact filenames are not unique")

    parent_fd = stack_fd = staging_fd = dumps_fd = volumes_fd = -1
    try:
        parent_fd, stack_name = _open_snapshot_parent_fd(scratch_fd, stack_path)
        stack_fd = _open_dir_at(parent_fd, stack_name)
        staging_fd = _open_dir_at(stack_fd, ".docker-backup")
        dumps_fd = _open_dir_at(staging_fd, "dumps")
        volumes_fd = _open_dir_at(staging_fd, "volumes")

        def exact_regular_entries(directory_fd: int, expected: list, label: str) -> None:
            actual = set(os.listdir(directory_fd))
            if actual != set(expected):
                raise util.CommandError(
                    ["restore", "manifest-artifacts"], 1,
                    "Authenticated %s artifacts do not exactly match the "
                    "plaintext manifest plan." % label,
                )
            for filename in actual:
                artifact_fd = _open_regular_at(directory_fd, filename)
                os.close(artifact_fd)

        exact_regular_entries(dumps_fd, expected_dumps, "database dump")
        exact_regular_entries(volumes_fd, expected_archives, "named-volume")
    except OSError as exc:
        raise util.CommandError(
            ["restore", "manifest-artifacts"], 1,
            "Authenticated v5 backup staging is incomplete: %s" % exc,
        )
    finally:
        for fd in (volumes_fd, dumps_fd, staging_fd, stack_fd, parent_fd):
            if fd >= 0:
                os.close(fd)


def _authenticate_manifest_compose_filename(
    cfg: Dict[str, Any], stack_fd: int, selected: str,
) -> None:
    """Prevent a plaintext sidecar from selecting an alternate Compose file.

    A stack can contain several conventional Compose filenames.  The encrypted
    snapshot authenticates their bytes but not which one the adjacent sidecar
    intended.  Automatic v5 reconstruction therefore proceeds only when the
    protected stack contains exactly one regular conventional Compose file and
    it is the selected basename.
    """
    if cfg.get("_manifest_schema_version", 0) < manifest.MANIFEST_SCHEMA_VERSION:
        return
    candidates = []
    for name in _COMPOSE_NAMES:
        info = _lstat_at(stack_fd, name)
        if info is not None and stat.S_ISREG(info.st_mode):
            candidates.append(name)
    if candidates != [selected]:
        raise util.CommandError(
            ["restore", "manifest-compose-file"], 1,
            "The plaintext manifest cannot uniquely authenticate its Compose "
            "filename. Expected only %s, found regular candidate(s): %s."
            % (selected, ", ".join(candidates) or "none"),
        )


def _run_restore(cfg, name: str, dest: str, snapshot: str, force: bool,
                 no_custom_restore: bool = False,
                 save_config: bool = False) -> int:
    """Core of the restore — identical for the classic and the bootstrap path."""
    try:
        dest = _canonical_absolute_path(dest, "restore target")
        if not util.DRY_RUN:
            _assert_safe_host_target(dest, "restore target", expected="directory")
    except ValueError as exc:
        util.error("Unsafe restore target: %s" % exc)
        return 1
    runtime.load_backend_env(cfg)
    if not util.DRY_RUN:
        installed = restic.restic_version()
        if installed is not None and installed < restic.MIN_VERSION:
            have = ".".join(str(x) for x in installed)
            need = ".".join(str(x) for x in restic.MIN_VERSION)
            util.error(
                "restic %s is too old for a safe restore (need >= %s for "
                "unambiguous repository detection and restore --sparse). "
                "Upgrade restic before retrying." % (have, need)
            )
            return 1
    if not util.DRY_RUN and not restic.repo_initialized(cfg["repo"], cfg["key_file"]):
        util.error("restic repo not reachable: %s" % cfg["repo"])
        return 1

    try:
        restore_paths = _validated_restore_paths(cfg)
    except ValueError as exc:
        util.error("Unsafe restore metadata: %s" % exc)
        return 1

    if not util.DRY_RUN:
        try:
            _authenticate_manifest_snapshot(cfg, snapshot, restore_paths)
        except (TypeError, ValueError, util.CommandError) as exc:
            util.error("Cannot authenticate manifest snapshot metadata: %s" % exc)
            return 1

    if (not no_custom_restore and hooks.phase_hooks(cfg, "restore")
            and not cfg.get("_restore_hooks_confirmed")):
        if cfg.get("_template_hooks_confirmed"):
            cfg["_restore_hooks_confirmed"] = True
        elif not _confirm_custom_restore(cfg):
            return 1

    # Put the restic scratch tree on the destination filesystem (inside an existing
    # target mount, otherwise beside the not-yet-created target). _move_tree can then
    # rename the restored data into place instead of briefly keeping a second full
    # copy. Always remove scratch on errors too; a failed large restore must not
    # strand hundreds of GB.
    project_hint = _target_project_name(dest)
    initial_dest_fd = -1
    initial_dest_mode = None  # type: Optional[int]
    if not util.DRY_RUN:
        try:
            initial_dest_fd, initial_dest_mode, _dest_created = _reserve_restore_directory(
                dest, force=force,
            )
        except (OSError, ValueError, util.CommandError) as exc:
            util.error("Cannot reserve restore target safely: %s" % exc)
            return 1
        try:
            overlapping = compose.running_writable_bind_mounts_overlapping(dest)
        except (OSError, ValueError, util.CommandError) as exc:
            os.fchmod(
                initial_dest_fd,
                initial_dest_mode if initial_dest_mode is not None else 0o755,
            )
            os.close(initial_dest_fd)
            initial_dest_fd = -1
            util.error(
                "Cannot prove the restore target is quiescent before creating "
                "scratch data: %s" % exc
            )
            return 1
        if overlapping:
            os.fchmod(
                initial_dest_fd,
                initial_dest_mode if initial_dest_mode is not None else 0o755,
            )
            os.close(initial_dest_fd)
            initial_dest_fd = -1
            util.error(
                "Running containers have writable bind mounts overlapping the "
                "restore target. Stop/remove them before restoring: %s"
                % ", ".join(
                    "%s (%s)" % (item["container"], item["source"])
                    for item in overlapping
                )
            )
            return 1
    try:
        scratch_ref = _make_restore_scratch(
            name, dest, dest_fd=initial_dest_fd if initial_dest_fd >= 0 else None,
        )
    except Exception:
        if initial_dest_fd >= 0:
            if initial_dest_mode is not None:
                os.fchmod(initial_dest_fd, initial_dest_mode)
            os.close(initial_dest_fd)
        raise
    scratch = scratch_ref.path
    placed_dest_fd = -1
    restored_compose_fd = -1
    external_target_fds = []
    trusted_runtime_file_fds = []
    placement_phase_complete = False
    main_tree_placed = False
    operation_dest = dest
    new_compose = ""
    cj = {}  # type: Dict[str, Any]
    manifest_compose_model = {}  # type: Dict[str, Any]
    compose_name = ""
    compose_digest = None  # type: Optional[str]
    source_operation_dest = ""
    named_volume_plan = None  # type: Optional[list]
    try:
        restore_kwargs = {"paths": restore_paths}
        if scratch_ref.fd >= 0:
            restore_kwargs["target_fd"] = scratch_ref.fd
        restic.restore(
            cfg["repo"], cfg["key_file"], snapshot, scratch,
            **restore_kwargs,
        )
        src = _locate_restored(scratch, cfg["stack_path"]) if util.DRY_RUN else None
        source_info = (None if util.DRY_RUN else
                       _snapshot_root_info(scratch_ref.fd, cfg["stack_path"]))
        if (util.DRY_RUN and not src) or (not util.DRY_RUN and (
                source_info is None or not stat.S_ISDIR(source_info.st_mode))):
            util.error("Restored stack tree not found safely under %s." % scratch)
            return 1
        compose_name = os.path.basename(cfg["compose_file"])

        # Authenticate and normalize Compose while the restored tree is still in
        # the root-only scratch directory. Opening it only after placement would
        # let a writer in an existing destination replace the file before its
        # first privileged parse.
        if not util.DRY_RUN:
            source_parent_fd = source_stack_fd = source_compose_fd = -1
            try:
                source_parent_fd, source_name = _open_snapshot_parent_fd(
                    scratch_ref.fd, cfg["stack_path"],
                )
                source_stack_fd = _open_dir_at(source_parent_fd, source_name)
                _authenticate_manifest_compose_filename(
                    cfg, source_stack_fd, compose_name,
                )
                source_compose_fd = _open_regular_at(source_stack_fd, compose_name)
                source_mirror_root = _fd_host_path(scratch_ref.fd)
                source_operation_dest = os.path.join(
                    source_mirror_root, cfg["stack_path"].lstrip(os.path.sep),
                )
                source_compose = _fd_host_path(source_compose_fd)
                compose_digest = _fd_sha256(source_compose_fd)
                # Compose interpolation must be determined only by protected
                # restored project inputs (notably its .env file). Ambient root,
                # backend and shell variables could otherwise override ${IMAGE},
                # ${COMMAND}, bind sources, or leak restic credentials into the
                # privileged runtime model. An empty replacement environment also
                # lets a legitimate COMPOSE_PROJECT_NAME from restored .env apply.
                compose_env = _protected_compose_environment()
                compose_probe = compose.config_json(
                    source_compose, source_operation_dest,
                    env=compose_env, env_replace=True,
                )
                # Preserve the source-side project identity for manifest
                # completeness checks.  The second parse intentionally applies
                # the target project name, which changes implicit real volume
                # names during a cross-server restore.
                manifest_compose_model = _rebase_scratch_paths(
                    compose_probe, source_operation_dest, source_mirror_root,
                    cfg["stack_path"], cfg["stack_path"],
                )
                project_hint = _authenticated_project_name(
                    compose_probe, source_operation_dest, dest,
                )
                cj = compose.config_json(
                    source_compose, source_operation_dest,
                    project_name=project_hint,
                    env=compose_env, env_replace=True,
                )
                trusted_runtime_file_fds = _pin_compose_file_artifacts(
                    cj, scratch_ref.fd, source_operation_dest,
                    source_mirror_root, dest, cfg["stack_path"],
                )
                cj = _rebase_scratch_paths(
                    cj, source_operation_dest, source_mirror_root, dest,
                    cfg["stack_path"],
                )
            finally:
                if source_compose_fd >= 0:
                    os.close(source_compose_fd)
                if source_stack_fd >= 0:
                    os.close(source_stack_fd)
                if source_parent_fd >= 0:
                    os.close(source_parent_fd)
            try:
                _authenticate_manifest_compose_plan(cfg, manifest_compose_model)
                _authenticate_manifest_artifacts(
                    cfg, scratch_ref.fd, cfg["stack_path"],
                )
            except (TypeError, ValueError, util.CommandError) as exc:
                util.error(
                    "Cannot authenticate manifest database/volume plan: %s" % exc
                )
                return 1
            will_import = no_custom_restore or not hooks.phase_hooks(cfg, "restore")
            if not _confirm_manifest_database_imports(cfg, cj, will_import=will_import):
                return 1

        try:
            mappings = _external_restore_mappings(cfg, cj)
            _validate_external_mapping_targets(mappings, dest)
        except util.CommandError as exc:
            util.error("Cannot map selected external bind data safely: %s" % exc)
            return 1
        if mappings:
            util.warn("Selected bind data would be placed outside/alongside the stack:")
            for source, target in mappings:
                util.warn("    snapshot %s  ->  target %s" % (source, target))
            if (not util.DRY_RUN
                    and not wizard.confirm("Restore these selected bind paths?", default=False)):
                util.warn(
                    "External bind restore not confirmed; no restored data was placed."
                )
                return 1

        if not util.DRY_RUN:
            cleanup_compose_fd = -1
            try:
                cleanup_compose_fd, cleanup_compose = _write_project_cleanup_compose(
                    project_hint,
                )
                util.info(
                    "Stopping any existing containers for the authenticated "
                    "restore project before placing data…"
                )
                compose.down_all(
                    cleanup_compose, source_operation_dest, project_hint,
                )
            finally:
                if cleanup_compose_fd >= 0:
                    os.close(cleanup_compose_fd)
            try:
                remaining_bind_users = []
                for target in [dest] + [target for _source, target in mappings]:
                    remaining_bind_users.extend(
                        compose.running_writable_bind_mounts_overlapping(target)
                    )
            except (OSError, ValueError, util.CommandError) as exc:
                util.error(
                    "Cannot prove all restore targets are quiescent before "
                    "placement: %s" % exc
                )
                return 1
            if remaining_bind_users:
                unique_bind_users = sorted({
                    (item["container"], item["source"])
                    for item in remaining_bind_users
                })
                util.error(
                    "Running containers still have writable bind mounts "
                    "overlapping restore targets. Stop/remove them before "
                    "restoring: %s"
                    % ", ".join(
                        "%s (%s)" % item for item in unique_bind_users
                    )
                )
                return 1
            try:
                named_volume_plan = _preflight_named_volume_restore(
                    cfg, cj, project_hint, force=force,
                    canonical_dest=dest,
                )
            except (ValueError, util.CommandError) as exc:
                util.error("Cannot restore named volumes safely: %s" % exc)
                return 1

        util.info("Placing restored stack tree at %s…" % dest)
        if (not util.DRY_RUN
                and source_info.st_dev != os.fstat(initial_dest_fd).st_dev):
            util.warn(
                "Scratch and destination are on different filesystems; files "
                "must be copied instead of renamed. With many small files this "
                "can take a long time even after restic has reached 100%."
            )
        if util.DRY_RUN:
            _move_tree(src, dest)
        else:
            placed_dest_fd = _move_tree_from_snapshot(
                scratch_ref.fd, cfg["stack_path"], dest,
                expected_target_fd=initial_dest_fd,
                allow_replace=force,
                allowed_existing={scratch_ref.name},
            )
            main_tree_placed = True
            os.close(initial_dest_fd)
            initial_dest_fd = -1
            operation_dest = _fd_host_path(placed_dest_fd)
            restored_compose_fd = _open_regular_at(placed_dest_fd, compose_name)
            if not hmac.compare_digest(
                    compose_digest or "", _fd_sha256(restored_compose_fd)):
                util.error(
                    "Restored Compose file changed while the stack was being placed; "
                    "refusing privileged restore operations."
                )
                return 1
        util.info("Restored stack tree placed.")

        new_compose = os.path.join(operation_dest, compose_name)
        (cfg["_restored_extra_backup_paths"], external_target_fds) = _restore_extra_paths(
            cfg, scratch, force, mappings=mappings,
            scratch_fd=scratch_ref.fd if scratch_ref.fd >= 0 else None,
            retain_fds=True,
        )
        if len(cfg["_restored_extra_backup_paths"]) != len(mappings):
            util.error(
                "Not every selected external bind was restored. Refusing Docker "
                "restore actions against incomplete or pre-existing live data; "
                "use --force after review or choose a different target layout."
            )
            return 1
        placement_phase_complete = True
    finally:
        pending_external_fds = cfg.pop("_pending_external_fds", [])
        _close_path_fds(pending_external_fds)
        if not util.DRY_RUN:
            try:
                _cleanup_restore_scratch(scratch_ref)
            except Exception:
                if placed_dest_fd >= 0:
                    os.close(placed_dest_fd)
                    placed_dest_fd = -1
                if restored_compose_fd >= 0:
                    os.close(restored_compose_fd)
                    restored_compose_fd = -1
                _close_path_fds(external_target_fds)
                external_target_fds = []
                _close_trusted_file_fds(trusted_runtime_file_fds)
                raise
            finally:
                if initial_dest_fd >= 0:
                    if not main_tree_placed:
                        os.fchmod(
                            initial_dest_fd,
                            initial_dest_mode if initial_dest_mode is not None else 0o755,
                        )
                    os.close(initial_dest_fd)
                    initial_dest_fd = -1
                if not placement_phase_complete and placed_dest_fd >= 0:
                    os.close(placed_dest_fd)
                    placed_dest_fd = -1
                if not placement_phase_complete and restored_compose_fd >= 0:
                    os.close(restored_compose_fd)
                    restored_compose_fd = -1
                if not placement_phase_complete:
                    _close_path_fds(external_target_fds)
                    external_target_fds = []
                    _close_trusted_file_fds(trusted_runtime_file_fds)

    new_project = cj.get("name") or project_hint
    cfg["_restored_project_name"] = new_project
    runtime_compose_fd = -1
    runtime_source_fds = []
    try:
        if placed_dest_fd >= 0:
            runtime_compose_fd, new_compose, runtime_source_fds = _write_runtime_compose(
                cj, operation_dest, dest, external_target_fds, new_project,
                dest_fd=placed_dest_fd,
                trusted_file_fds=trusted_runtime_file_fds,
            )
            util.info("Stopping any existing containers for the restore project…")
            compose.down_all(new_compose, operation_dest, new_project)
        _restore_named_volumes(
            cfg, cj, operation_dest, new_project,
            dest_fd=placed_dest_fd if placed_dest_fd >= 0 else None,
            force=force, plan=named_volume_plan,
        )

        if no_custom_restore or not hooks.phase_hooks(cfg, "restore"):
            _import_databases(
                cfg, cj, new_compose, operation_dest, new_project,
                dest_fd=placed_dest_fd if placed_dest_fd >= 0 else None,
            )
            _cleanup_restore_staging(
                operation_dest,
                dest_fd=placed_dest_fd if placed_dest_fd >= 0 else None,
            )
            util.info("Restore to %s complete." % dest)
            canonical_compose = os.path.join(dest, os.path.basename(cfg["compose_file"]))
            util.info(
                "Check the env files, then start: docker compose -f %s up -d"
                % canonical_compose
            )
        else:
            if not _custom_restore(
                    cfg, new_compose, operation_dest, new_project,
                    already_confirmed=bool(cfg.get("_restore_hooks_confirmed")),
                    display_dest=dest):
                return 1
            _cleanup_restore_staging(
                operation_dest,
                dest_fd=placed_dest_fd if placed_dest_fd >= 0 else None,
            )
        if placed_dest_fd >= 0:
            try:
                _assert_path_matches_fd(dest, placed_dest_fd)
            except (OSError, ValueError) as exc:
                util.error(
                    "Restore destination path changed while the restore was running; "
                    "operations stayed on the original directory, but completion is "
                    "refused: %s" % exc
                )
                return 1
        if restored_compose_fd >= 0:
            try:
                _assert_open_entry(
                    placed_dest_fd, compose_name, restored_compose_fd,
                )
                if not hmac.compare_digest(
                        compose_digest or "", _fd_sha256(restored_compose_fd)):
                    raise OSError(
                        errno.ESTALE, "Restored Compose file contents changed",
                        compose_name,
                    )
            except (OSError, ValueError) as exc:
                util.error(
                    "Restored Compose file changed while the restore was running; "
                    "refusing completion: %s" % exc
                )
                return 1
        for target, target_fd in external_target_fds:
            try:
                _assert_path_matches_fd(target, target_fd)
            except (OSError, ValueError) as exc:
                util.error(
                    "External restore target path changed while the restore was "
                    "running; refusing completion: %s" % exc
                )
                return 1
        for target, target_fd in runtime_source_fds:
            try:
                _assert_path_matches_fd(target, target_fd)
            except (OSError, ValueError) as exc:
                util.error(
                    "Runtime Compose source path changed while the restore was "
                    "running; refusing completion: %s" % exc
                )
                return 1
        retained_by_path = dict(runtime_source_fds + external_target_fds)
        for target, _trusted_fd, expected_digest in trusted_runtime_file_fds:
            target_fd = retained_by_path.get(target)
            if (target_fd is None or not hmac.compare_digest(
                    expected_digest, _fd_sha256(target_fd))):
                util.error(
                    "Restored Compose config/secret file changed while the restore "
                    "was running; refusing completion: %s" % target
                )
                return 1
        if save_config and placed_dest_fd >= 0:
            cfg["_restored_compose_model"] = copy.deepcopy(cj)
            cfg["_restored_compose_digest"] = compose_digest
            cfg["_restored_dest_fd"] = placed_dest_fd
            placed_dest_fd = -1
            cfg["_restored_compose_fd"] = restored_compose_fd
            restored_compose_fd = -1
            cfg["_restored_external_fds"] = external_target_fds
            external_target_fds = []
        return 0
    finally:
        if runtime_compose_fd >= 0:
            os.close(runtime_compose_fd)
        _close_path_fds(runtime_source_fds)
        _close_trusted_file_fds(trusted_runtime_file_fds)
        if placed_dest_fd >= 0:
            os.close(placed_dest_fd)
        if restored_compose_fd >= 0:
            os.close(restored_compose_fd)
        _close_path_fds(external_target_fds)


def _confirm_custom_restore(cfg: Dict[str, Any]) -> bool:
    """Authorize restore hooks before restic, project shutdown, or placement."""
    cmds = [c for (phase, c) in hooks.describe_commands(cfg) if phase == "restore"]
    util.warn("This restore can run these custom commands as ROOT:")
    for command in cmds:
        util.warn("    %s" % command)
    if not util.DRY_RUN and not wizard.confirm(
            "Run these restore commands after placing the data?", default=False):
        util.warn("Custom restore command not confirmed; nothing was restored.")
        return False
    cfg["_restore_hooks_confirmed"] = True
    return True


def _custom_restore(
    cfg, new_compose: str, dest: str, new_project: str,
    already_confirmed: bool = False,
    display_dest: Optional[str] = None,
) -> bool:
    """Custom restore command (e.g. GitLab) instead of the built-in DB import.

    The built-in restore leaves the stack stopped; a ``docker exec``-based restore
    hook, however, needs a running container → the stack is brought up first.
    Readiness is the hook's own responsibility (no generic probe).
    """
    cmds = [c for (ph, c) in hooks.describe_commands(cfg) if ph == "restore"]
    util.warn("This restore runs custom commands as ROOT:")
    for c in cmds:
        util.warn("    %s" % c)
    if not already_confirmed and not util.DRY_RUN:
        if not wizard.confirm("Run these restore commands now?", default=False):
            util.warn("Custom restore command skipped (not confirmed). The stack tree was "
                      "restored but NOT imported — import the data manually or rerun "
                      "and confirm the command.")
            return False

    # Explicit confirmation = approval for this run. Point stack_path/compose at
    # the target so the hook cwd/env are correct on the restore server.
    hook_cfg = dict(cfg)
    hook_cfg["stack_path"] = dest
    hook_cfg["compose_file"] = new_compose
    hook_cfg["project_name"] = new_project
    hooks.approve(hook_cfg)

    util.info("Bringing the stack up and running the restore command…")
    try:
        # Keep startup inside the teardown guard: Compose can create some
        # containers and still return a failure for the overall `up` operation.
        compose.up_all(new_compose, dest, new_project)
        hooks.run_hooks(hook_cfg, "restore")
    finally:
        # Restore-time Compose uses a descriptor-backed project directory so a
        # concurrent path replacement cannot redirect host binds. Never leave
        # containers with that process-lifetime /proc path in HostConfig: remove
        # them before closing the descriptor; data and named volumes are retained.
        compose.down_all(new_compose, dest, new_project)
    util.info("Custom restore command to %s complete." % (display_dest or dest))
    return True


def _bootstrap_cfg(args, dest: str):
    """Reconstruct an in-memory config from the repo manifest on the drive.

    Returns ``(cfg, name)`` or ``(None, None)`` on error (a message has already
    been printed in that case)."""
    raw = args.from_repo
    repo = os.path.abspath(raw) if compose.is_local_repo(raw) else raw
    man = manifest.read(repo)
    if man is None:
        util.error(
            "No manifest found under %s. Is the repo on the mounted drive and does it "
            "come from a docker-backup version with a manifest? "
            "(Otherwise restore the classic way with --from <name> + local config.)" % repo
        )
        return None, None
    try:
        _validate_bootstrap_manifest(man)
        name = config.sanitize_name(
            getattr(args, "bootstrap_name", None) or man.get("name")
            or os.path.basename(dest.rstrip("/"))
        )
    except (KeyError, TypeError, ValueError, util.CommandError) as exc:
        util.error("Unsafe or malformed repository manifest: %s" % exc)
        return None, None
    key_file = _resolve_bootstrap_key(args, name)
    if key_file is None:
        return None, None
    try:
        cfg = manifest.cfg_from_manifest(man, repo, key_file, name=name)
    except (KeyError, TypeError, ValueError) as exc:
        util.error("Unsafe or malformed repository manifest: %s" % exc)
        return None, None
    _warn_stored_password_source(cfg)
    return cfg, name


def _validate_bootstrap_manifest(man: Dict[str, Any]) -> None:
    """Validate plaintext sidecar fields before they reach root-level actions."""
    if not isinstance(man, dict):
        raise ValueError("manifest must be an object")
    _canonical_absolute_path(man.get("stack_path"), "stack_path")
    compose_name = man.get("compose_file")
    if compose_name not in _COMPOSE_NAMES:
        raise ValueError("compose_file must be one of %s" % ", ".join(_COMPOSE_NAMES))
    config.sanitize_name(man.get("name"))

    extras = man.get("extra_backup_paths") or []
    _validated_restore_paths({"stack_path": man.get("stack_path"),
                              "extra_backup_paths": extras})
    descriptors = man.get("external_bind_descriptors")
    if descriptors is not None and (
            not isinstance(descriptors, list) or len(descriptors) > 256):
        raise ValueError("external_bind_descriptors must be a list with at most 256 entries")

    schema = man.get("manifest_schema_version", 1)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise ValueError("manifest_schema_version must be a positive integer")
    if schema >= manifest.MANIFEST_SCHEMA_VERSION:
        for field in ("hooks_present", "custom_restore_required"):
            if not isinstance(man.get(field), bool):
                raise ValueError("%s must be a boolean" % field)
        if not isinstance(man.get("db_autodetect"), bool):
            raise ValueError("db_autodetect must be a boolean")
        if not isinstance(man.get("external_bind_descriptors"), list):
            raise ValueError("external_bind_descriptors must be a list")

    _validate_manifest_db_services(man.get("db_services") or [], strict=schema >= 5)
    _validate_manifest_named_volumes(man.get("named_volumes") or [], strict=schema >= 5)

    patterns = man.get("exclude_patterns") or []
    if (not isinstance(patterns, list) or len(patterns) > 512
            or any(not isinstance(p, str) or not p or len(p) > 2048 or "\0" in p
                   for p in patterns)):
        raise ValueError("exclude_patterns is malformed")


def _manifest_token(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not value[0].isascii() or not value[0].isalnum()
            or any(not c.isascii() or not (c.isalnum() or c in "_.-")
                   for c in value)):
        raise ValueError("invalid %s" % label)
    return value


def _validate_manifest_db_services(raw: Any, *, strict: bool) -> None:
    allowed = {
        "service", "engine", "auth_user", "all_databases", "databases",
        "dump_globals", "password_source", "data_dir_target", "raw_data_exclude",
    }
    if not isinstance(raw, list) or len(raw) > 32:
        raise ValueError("db_services must be a list with at most 32 entries")
    for index, db in enumerate(raw):
        if not isinstance(db, dict):
            raise ValueError("db_services[%d] must be an object" % index)
        if strict and set(db) - allowed:
            raise ValueError("db_services[%d] has unknown fields" % index)
        _manifest_token(db.get("service"), "database service")
        if db.get("engine") not in ("mysql", "postgres"):
            raise ValueError("invalid database engine")
        for field in ("auth_user",):
            value = db.get(field)
            if value is not None and (
                    not isinstance(value, str) or len(value) > 256 or "\0" in value):
                raise ValueError("invalid database %s" % field)
        for field in ("all_databases", "dump_globals"):
            if field in db and not isinstance(db[field], bool):
                raise ValueError("database %s must be a boolean" % field)
        databases = db.get("databases")
        if databases is not None and (
                not isinstance(databases, list) or len(databases) > 64
                or any(not isinstance(v, str) or not v or len(v) > 256
                       or "\0" in v or "/" in v or "\\" in v
                       for v in databases)):
            raise ValueError("invalid database list")
        password_source = db.get("password_source", "none")
        if password_source not in ("none", "stored"):
            if (not isinstance(password_source, str)
                    or not password_source.startswith("env:")
                    or not password_source[4:]
                    or len(password_source) > 132
                    or any(not c.isascii() or not (c.isalnum() or c == "_")
                           for c in password_source[4:])):
                raise ValueError("invalid database password_source")
        for field in ("data_dir_target", "raw_data_exclude"):
            value = db.get(field)
            if value is not None:
                _canonical_absolute_path(value, "database %s" % field)


def _validate_manifest_named_volumes(raw: Any, *, strict: bool) -> None:
    allowed = {"key", "real_name", "target", "service"}
    if not isinstance(raw, list) or len(raw) > 128:
        raise ValueError("named_volumes must be a list with at most 128 entries")
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("named_volumes[%d] must be an object" % index)
        if strict and set(item) - allowed:
            raise ValueError("named_volumes[%d] has unknown fields" % index)
        _manifest_token(item.get("key"), "named volume key")
        if item.get("real_name") is not None:
            _manifest_token(item.get("real_name"), "named volume name")
        if item.get("service") is not None:
            _manifest_token(item.get("service"), "named volume service")
        if item.get("target") is not None:
            _canonical_absolute_path(item.get("target"), "named volume target")


def _resolve_bootstrap_key(args, name: str):
    """Key file for --from-repo. None (+ message) if none given/found."""
    key_file = getattr(args, "key_file", None)
    if not key_file:
        util.error(
            "No restic key given. Copy the key file from server A "
            "(/etc/docker-backup/keys/%s.key) to this server and pass it with "
            "--key-file <path> (or fetch the key via 'docker-backup key show %s')."
            % (name, name)
        )
        return None
    key_file = os.path.abspath(key_file)
    if not util.DRY_RUN and not os.path.exists(key_file):
        util.error("Key file not found: %s" % key_file)
        return None
    return key_file


def _warn_stored_password_source(cfg) -> None:
    """Warn about DB services whose password only exists on the source server."""
    stored = [db.get("service") for db in cfg.get("db_services") or []
              if db.get("password_source") == "stored"]
    if stored:
        util.warn(
            "DB service(s) %s use 'stored' passwords that only exist on the source "
            "server. The DB import may fail here — set the password manually or switch "
            "the stack to an env: source." % ", ".join(stored)
        )


def _external_restore_mappings(
    cfg: Dict[str, Any], target_compose: Dict[str, Any],
) -> list:
    extras = cfg.get("extra_backup_paths") or []
    if not extras:
        return []
    descriptors = cfg.get("external_bind_descriptors")
    if descriptors is not None:
        mappings = compose.resolve_external_bind_descriptors(target_compose, descriptors)
        mapped_sources = {source for source, _target in mappings}
        if mapped_sources != set(extras):
            raise util.CommandError(
                ["external-bind-descriptors"], 1,
                "Descriptor sources do not exactly match selected extra_backup_paths.",
            )
        return mappings
    if cfg.get("_manifest_schema_version", 0) >= manifest.MANIFEST_SCHEMA_VERSION:
        raise util.CommandError(
            ["external-bind-descriptors"], 1,
            "Snapshot has selected external paths but no portable bind descriptors.",
        )
    # Legacy/local configs predate portable service+container-target identities.
    # Preserve their old source location, but the caller still requires a fresh,
    # default-no confirmation before any placement.
    return [(path, path) for path in extras]


def _restore_extra_paths(
    cfg, scratch: str, force: bool, mappings: Optional[list] = None,
    scratch_fd: Optional[int] = None,
    retain_fds: bool = False,
):
    """External bind-mount paths from the snapshot → back to their ORIGINAL location.

    Only when the path is missing there; an existing path is clean-replaced only
    with ``--force`` (protects a test restore running next to the live stack)."""
    restored_targets = []
    retained = []
    if retain_fds:
        cfg["_pending_external_fds"] = retained
    if mappings is None:
        mappings = [(p, p) for p in (cfg.get("extra_backup_paths") or [])]
    for p, target in mappings:
        target = _canonical_absolute_path(target, "external restore target")
        if util.DRY_RUN:
            util.info("DRY-RUN: would restore external path %s to %s." % (p, target))
            restored_targets.append(target)
            continue
        restored = None
        try:
            if scratch_fd is not None:
                source_info = _snapshot_root_info(scratch_fd, p)
            else:
                restored = _path_in_scratch(scratch, p)
                source_info = os.lstat(restored) if os.path.lexists(restored) else None
        except (OSError, ValueError) as exc:
            raise util.CommandError(["restore", p], 1, str(exc))
        if source_info is None:
            if cfg.get("_manifest_schema_version", 0) >= manifest.MANIFEST_SCHEMA_VERSION:
                raise util.CommandError(
                    ["restore", p], 1,
                    "Selected external path is missing from the bound snapshot: %s" % p,
                )
            util.warn("External path %s is not in the snapshot — skipped." % p)
            continue
        if stat.S_ISLNK(source_info.st_mode):
            raise util.CommandError(
                ["restore", p], 1,
                "Refusing restored external path because its root is a symlink: %s" % p,
            )
        if stat.S_ISDIR(source_info.st_mode):
            expected = "directory"
        elif stat.S_ISREG(source_info.st_mode):
            expected = "file"
        else:
            raise util.CommandError(
                ["restore", p], 1,
                "Refusing special-file external snapshot root: %s" % p,
            )
        try:
            _assert_safe_host_target(target, "external restore target", expected=expected)
        except ValueError as exc:
            raise util.CommandError(["restore", target], 1, str(exc))
        placed_fd = -1
        if expected == "directory" and scratch_fd is not None:
            # Reserve the exact target inode just like the main stack. This closes
            # the exists-then-place race and lets no-force placement reject any
            # child that appears after reservation instead of merging into it.
            guard_fd = -1
            guard_mode = None  # type: Optional[int]
            guard_created = False
            placed = False
            try:
                try:
                    guard_fd, guard_mode, guard_created = _reserve_restore_directory(
                        target, force=force,
                    )
                except util.CommandError:
                    if not force:
                        util.warn(
                            "External target %s already exists — NOT overwritten "
                            "(--force clean-replaces it)." % target
                        )
                        continue
                    raise
                if not force and not guard_created:
                    util.warn(
                        "External target %s already exists — NOT overwritten "
                        "(--force clean-replaces it)." % target
                    )
                    continue
                if not guard_created:
                    util.warn(
                        "External target %s exists — clean-replacing it from the snapshot "
                        "(--force)." % target
                    )
                placed_fd = _move_tree_from_snapshot(
                    scratch_fd, p, target,
                    expected_target_fd=guard_fd,
                    allow_replace=force,
                    allowed_existing=set(),
                )
                placed = True
            finally:
                if guard_fd >= 0:
                    if not placed:
                        os.fchmod(
                            guard_fd,
                            guard_mode if guard_mode is not None else 0o755,
                        )
                    os.close(guard_fd)
        else:
            target_exists = os.path.lexists(target)
            if target_exists and not force:
                util.warn(
                    "External target %s already exists — NOT overwritten "
                    "(--force clean-replaces it)." % target
                )
                continue
            if target_exists:
                util.warn(
                    "External target %s exists — clean-replacing it from the snapshot "
                    "(--force)." % target
                )
            if expected == "directory":
                # Compatibility path for direct callers without a retained scratch
                # descriptor. Production restore always takes the guarded branch.
                _move_tree(restored, target)
                if retain_fds:
                    placed_fd = _open_target_fd(target, expected="directory")
            elif scratch_fd is None:
                _move_regular_file(restored, target, replace=force)
                if retain_fds:
                    placed_fd = _open_target_fd(target, expected="file")
            else:
                placed_fd = _move_regular_file_from_snapshot(
                    scratch_fd, p, target, replace=force,
                )
        if placed_fd >= 0:
            if retain_fds:
                retained.append((target, placed_fd))
            else:
                os.close(placed_fd)
        restored_targets.append(target)
        util.info("External path restored: %s -> %s" % (p, target))
    if retain_fds:
        cfg.pop("_pending_external_fds", None)
        return restored_targets, retained
    return restored_targets


def _preflight_named_volume_restore(
    cfg: Dict[str, Any], cj: Dict[str, Any], new_project: str, *, force: bool,
    canonical_dest: Optional[str] = None,
) -> list:
    """Authenticate archived and logical-DB volume targets before placement."""
    top_volumes = cj.get("volumes") or {}
    services = cj.get("services") or {}
    if not isinstance(top_volumes, dict) or not isinstance(services, dict):
        raise ValueError("Normalized Compose volumes/services must be objects.")
    plan = []
    seen_keys = {}  # type: Dict[str, str]
    seen_real_names = set()

    def add_named_volume(key: str, *, archive_key: Optional[str]) -> None:
        purpose = "archive" if archive_key is not None else "database"
        previous = seen_keys.get(key)
        if previous is not None:
            raise ValueError(
                "Named volume %s is selected more than once (%s and %s); "
                "archived application data and logical DB storage must never "
                "share a restore target." % (key, previous, purpose)
            )
        seen_keys[key] = purpose
        entry = top_volumes.get(key) or {}
        if not isinstance(entry, dict):
            raise ValueError("Compose volume %s definition is invalid." % key)
        if entry.get("external"):
            raise util.CommandError(
                ["restore", "named-volume", key], 1,
                "Compose volume %s is externally managed/shared. Automatic "
                "clean replacement is refused; restore it manually after "
                "quiescing every consumer." % key,
            )
        driver = entry.get("driver") or "local"
        driver_opts = entry.get("driver_opts") or {}
        if driver != "local" or driver_opts:
            raise util.CommandError(
                ["restore", "named-volume", key], 1,
                "Compose volume %s uses a custom driver or driver_opts. "
                "Automatic recreation could change its storage; restore it "
                "manually." % key,
            )
        raw_labels = entry.get("labels") or {}
        if not isinstance(raw_labels, dict) or any(
                not isinstance(label, str) or not label or "\0" in label
                or value is not None and not isinstance(value, (str, int, float, bool))
                for label, value in raw_labels.items()):
            raise ValueError("Compose volume %s labels are invalid." % key)
        labels = {
            label: "" if value is None else str(value)
            for label, value in raw_labels.items()
        }
        real = compose.real_volume_name(top_volumes, key, new_project)
        if real in seen_real_names:
            raise ValueError(
                "Several Compose volume keys resolve to the same target: %s" % real
            )
        seen_real_names.add(real)
        identity = compose.volume_identity(real)
        if identity is not None:
            users = compose.volume_container_ids(real)
            if users:
                raise util.CommandError(
                    ["restore", "named-volume", real], 1,
                    "Named volume %s is still referenced by container(s): %s. "
                    "Remove them before restoring."
                    % (real, ", ".join(value[:12] for value in users)),
                )
            if not force:
                raise util.CommandError(
                    ["restore", "named-volume", real], 1,
                    "Named volume %s already exists. Re-run with --force only "
                    "after review; it will be deleted and recreated." % real,
                )
        plan.append({
            "key": key,
            "real_name": real,
            "archive_key": archive_key,
            "driver": driver,
            "labels": labels,
            "expected_identity": identity,
        })

    for index, nv in enumerate(cfg.get("named_volumes") or []):
        if not isinstance(nv, dict):
            raise ValueError("named_volumes[%d] must be an object." % index)
        key = nv.get("key")
        service_name = nv.get("service")
        target = nv.get("target")
        if (not isinstance(key, str) or not key
                or not isinstance(service_name, str) or not service_name
                or not isinstance(target, str) or not target):
            raise ValueError("named_volumes[%d] identity is incomplete." % index)
        service = services.get(service_name)
        if not isinstance(service, dict) or not any(
                isinstance(volume, dict)
                and volume.get("type") == "volume"
                and volume.get("source") == key
                and volume.get("target") == target
                for volume in (service.get("volumes") or [])):
            raise ValueError(
                "Named volume %s is not the same service/target mapping in the "
                "authenticated Compose model." % key
            )
        add_named_volume(key, archive_key=key)

    # Raw DB named volumes are intentionally absent from cfg.named_volumes: the
    # logical SQL dump replaces them. They nevertheless need an authenticated,
    # empty target before the isolated DB service starts. Bind-backed DB data is
    # safe only below the clean-replaced stack target.
    for db in cfg.get("db_services") or []:
        service_name = db.get("service")
        engine = db.get("engine")
        service = services.get(service_name)
        if not isinstance(service, dict):
            raise ValueError("Database service is missing from Compose: %s" % service_name)
        data_targets = compose.DB_DATA_DIRS.get(engine) or ()
        mounts = [
            volume for volume in (service.get("volumes") or [])
            if isinstance(volume, dict) and volume.get("target") in data_targets
        ]
        if len(mounts) != 1:
            raise util.CommandError(
                ["restore", "database-volume", str(service_name)], 1,
                "Database service %s must have exactly one explicit data mount "
                "at %s for a durable logical restore."
                % (service_name, " or ".join(data_targets)),
            )
        mount = mounts[0]
        if mount.get("type") == "volume":
            key = mount.get("source")
            if not isinstance(key, str) or not key:
                raise util.CommandError(
                    ["restore", "database-volume", str(service_name)], 1,
                    "Anonymous database volumes are not safely reusable after "
                    "restore; declare a named volume.",
                )
            add_named_volume(key, archive_key=None)
        elif mount.get("type") == "bind":
            source = mount.get("source")
            if (not isinstance(source, str) or not canonical_dest
                    or not (source.startswith(canonical_dest.rstrip("/") + "/"))):
                raise util.CommandError(
                    ["restore", "database-bind", str(service_name)], 1,
                    "Logical DB bind source %r is outside the clean-replaced "
                    "stack target. Restore it manually or move it below %s."
                    % (source, canonical_dest or "the stack"),
                )
        else:
            raise util.CommandError(
                ["restore", "database-volume", str(service_name)], 1,
                "Database data mount has unsupported type %r."
                % mount.get("type"),
            )
    return plan


def _restore_named_volumes(
    cfg, cj, dest, new_project, *, dest_fd: Optional[int] = None,
    force: bool = False, plan: Optional[list] = None,
) -> None:
    vols_dir = os.path.join(dest, ".docker-backup", "volumes")
    restore_plan = plan
    if restore_plan is None:
        # Dry-run/direct-call compatibility. Production computes and validates
        # the complete plan before placing any stack or external data.
        restore_plan = [
            {
                "key": nv["key"],
                "real_name": compose.real_volume_name(
                    cj.get("volumes") or {}, nv["key"], new_project,
                ),
                "archive_key": nv["key"],
                "driver": "local",
                "labels": {},
            }
            for nv in (cfg.get("named_volumes") or [])
        ]
    staging_fd = volumes_fd = -1
    pinned_archives = {}  # type: Dict[str, tuple]
    if any(item["archive_key"] is not None for item in restore_plan) and not util.DRY_RUN:
        try:
            root_fd = os.dup(dest_fd) if dest_fd is not None else _open_absolute_dir_fd(
                dest, create=False,
            )
            try:
                staging_fd = _open_dir_at(root_fd, ".docker-backup")
            finally:
                os.close(root_fd)
            volumes_fd = _open_dir_at(staging_fd, "volumes")
            # Validate and pin *every* archive before deleting/recreating the
            # first destination volume. A missing/corrupt later tar must not
            # leave earlier volumes empty or partially restored.
            for item in restore_plan:
                archive_key = item["archive_key"]
                if archive_key is None:
                    continue
                archive, archive_fd = volumes._open_archive(
                    volumes_fd, archive_key,
                )
                try:
                    volumes.validate_archive_fd(archive_fd, archive)
                except Exception:
                    os.close(archive_fd)
                    raise
                pinned_archives[archive_key] = (archive, archive_fd)
        except (OSError, ValueError, util.CommandError) as exc:
            for _archive, archive_fd in pinned_archives.values():
                os.close(archive_fd)
            pinned_archives.clear()
            if staging_fd >= 0:
                os.close(staging_fd)
            raise util.CommandError(["restore", vols_dir], 1, str(exc))
    try:
        for item in restore_plan:
            real = item["real_name"]
            if util.DRY_RUN:
                util.info(
                    "DRY-RUN: would require an empty named volume %s%s."
                    % (real, " (replace with --force)" if force else "")
                )
            else:
                compose.prepare_volume_for_restore(
                    real, force=force, driver=item["driver"],
                    labels=item["labels"], project_name=new_project,
                    volume_key=item["key"],
                    expected_identity=item.get("expected_identity"),
                )
            if item["archive_key"] is not None:
                pinned = pinned_archives.get(item["archive_key"])
                volumes.restore_named_volume(
                    real, vols_dir, item["archive_key"],
                    staging_fd=volumes_fd if volumes_fd >= 0 else None,
                    archive=pinned[0] if pinned is not None else "",
                    archive_fd=pinned[1] if pinned is not None else None,
                )
    finally:
        for _archive, archive_fd in pinned_archives.values():
            os.close(archive_fd)
        if volumes_fd >= 0:
            os.close(volumes_fd)
        if staging_fd >= 0:
            os.close(staging_fd)


def _cleanup_restore_staging(dest: str, *, dest_fd: Optional[int] = None) -> None:
    """Remove dump/tar staging after every restore phase completed successfully.

    Named-volume archives are uncompressed and can be as large as the application
    data they just restored.  Keeping them in the stack would permanently double
    its disk use.  On a failed import this function is never reached, deliberately
    leaving the artifacts available for diagnosis or a manual retry.
    """
    staging = os.path.join(dest, ".docker-backup")
    if util.DRY_RUN:
        util.info("DRY-RUN: would remove restored dump and volume staging from %s." % staging)
        return
    root_fd = staging_fd = -1
    try:
        root_fd = (os.dup(dest_fd) if dest_fd is not None
                   else _open_absolute_dir_fd(dest, create=False))
        staging_info = _lstat_at(root_fd, ".docker-backup")
        if staging_info is None:
            return
        if not stat.S_ISDIR(staging_info.st_mode):
            raise ValueError("Restored staging root is not a real directory: %s" % staging)
        staging_fd = _open_dir_at(root_fd, ".docker-backup")
        for name in ("dumps", "volumes"):
            info = _lstat_at(staging_fd, name)
            if info is None:
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Refusing unsafe restored staging entry: %s"
                                 % os.path.join(staging, name))
            _remove_entry_at(staging_fd, name)
        if not os.listdir(staging_fd):
            _assert_open_entry(root_fd, ".docker-backup", staging_fd)
            os.close(staging_fd)
            staging_fd = -1
            os.rmdir(".docker-backup", dir_fd=root_fd)
    except (OSError, ValueError) as exc:
        raise util.CommandError(["restore-cleanup", staging], 1, str(exc))
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _import_databases(
    cfg, cj, new_compose, dest, new_project, *, dest_fd: Optional[int] = None,
) -> None:
    dumps_dir = os.path.join(dest, ".docker-backup", "dumps")
    staging_fd = dumps_fd = -1
    if cfg.get("db_services") and not util.DRY_RUN:
        try:
            root_fd = os.dup(dest_fd) if dest_fd is not None else _open_absolute_dir_fd(
                dest, create=False,
            )
            try:
                staging_fd = _open_dir_at(root_fd, ".docker-backup")
            finally:
                os.close(root_fd)
            dumps_fd = _open_dir_at(staging_fd, "dumps")
        except (OSError, ValueError) as exc:
            if staging_fd >= 0:
                os.close(staging_fd)
            raise util.CommandError(["restore", dumps_dir], 1, str(exc))
    try:
        for db in cfg.get("db_services") or []:
            # A dry-run never places/parses the restored Compose tree, so ``cj``
            # is intentionally empty here.  Falling back to cfg's source-side
            # compose_file/stack_path would make a cross-server dry-run inspect a
            # path that does not exist on the target (and stored credentials must
            # not be read merely to print a plan).  The real restore has the
            # authenticated target Compose model and resolves the password below
            # as before.
            password = (
                None if util.DRY_RUN
                else runtime.resolve_password(cfg, db, cj or None)
            )
            util.info("Starting DB service '%s' for the import…" % db["service"])
            try:
                # Do not start `depends_on` services: only the isolated database is
                # needed, and every container using transient FD-backed binds must
                # be removed before those descriptors close. Keep startup under the
                # cleanup guard because Compose may partially create before failing.
                compose.up_service(
                    new_compose, dest, db["service"], new_project, no_deps=True,
                )
                if not dbdump.wait_ready(
                        db, password, new_compose, dest, new_project, timeout=180):
                    util.warn("DB '%s' not ready; import may fail." % db["service"])
                # import_dump handles globals, multi-DB dumps and the legacy fallback.
                dbdump.import_dump(
                    db, password, new_compose, dest, new_project, dumps_dir,
                    dumps_fd=dumps_fd if dumps_fd >= 0 else None,
                )
            finally:
                # Fully stop the stack even when import fails (data is preserved). A
                # failed restore must not leave a partially imported DB serving traffic.
                compose.rm_service(new_compose, dest, db["service"], new_project)
    finally:
        if dumps_fd >= 0:
            os.close(dumps_fd)
        if staging_fd >= 0:
            os.close(staging_fd)


def _locate_restored(scratch: str, stack_path: str) -> Optional[str]:
    try:
        direct = _path_in_scratch(scratch, stack_path)
    except ValueError:
        return None
    if util.DRY_RUN:
        return direct
    if os.path.isdir(direct) and not os.path.islink(direct):
        return direct
    return None


def _canonical_absolute_path(value: Any, label: str = "path") -> str:
    """Validate a restic source path before it can influence a host path.

    Restic stores absolute backup roots beneath the restore target.  Requiring
    their canonical absolute spelling keeps ``..``, duplicate separators and
    the filesystem root itself out of both restic selectors and placement code.
    """
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("%s must be a non-empty absolute path" % label)
    if not os.path.isabs(value) or value.startswith("//"):
        raise ValueError("%s must be absolute: %r" % (label, value))
    normalized = os.path.normpath(value)
    if value != normalized:
        raise ValueError("%s must be canonical (no '..', '.', or duplicate '/'): %r"
                         % (label, value))
    if normalized == os.path.sep:
        raise ValueError("%s must not be the filesystem root" % label)
    return normalized


def _path_in_scratch(scratch: str, original_path: str) -> str:
    """Map an absolute snapshot root beneath ``scratch`` and prove containment.

    ``realpath`` is intentional: a malicious restored parent symlink must not
    make a seemingly contained lexical path point at (for example) ``/etc``.
    The final root symlink is rejected by the callers before traversal/moving.
    """
    original = _canonical_absolute_path(original_path, "snapshot path")
    root = os.path.realpath(os.path.abspath(scratch))
    candidate = os.path.join(root, original.lstrip(os.path.sep))
    resolved = os.path.realpath(candidate)
    try:
        contained = os.path.commonpath((root, resolved)) == root
    except ValueError:
        contained = False
    if not contained:
        raise ValueError(
            "snapshot path escapes the restore staging directory: %r" % original_path
        )
    return candidate


def _validated_restore_paths(cfg: Dict[str, Any]) -> list:
    stack = _canonical_absolute_path(cfg.get("stack_path"), "stack_path")
    extras = cfg.get("extra_backup_paths") or []
    if not isinstance(extras, list) or len(extras) > 64:
        raise ValueError("extra_backup_paths must be a list with at most 64 entries")
    out = [stack]
    seen = {stack}
    for index, raw in enumerate(extras):
        path = _canonical_absolute_path(raw, "extra_backup_paths[%d]" % index)
        if path == stack or path.startswith(stack + os.path.sep):
            raise ValueError("extra backup path is already inside stack_path: %s" % path)
        if path not in seen:
            seen.add(path)
            out.append(path)
    ordered = sorted(out)
    for index, path in enumerate(ordered):
        for other in ordered[index + 1:]:
            if other.startswith(path.rstrip("/") + os.path.sep):
                raise ValueError(
                    "snapshot roots overlap: %s contains %s" % (path, other)
                )
    return out


def _assert_safe_host_target(path: str, label: str, *, expected: str) -> None:
    """Reject symlink/special-file redirection at a root write destination.

    Every existing component is inspected with ``lstat``. Missing suffixes are
    allowed and created only after their nearest ancestor was proven to be a real
    directory. Callers repeat this immediately before placement to narrow races.
    """
    path = _canonical_absolute_path(path, label)
    parts = [part for part in path.split(os.path.sep) if part]
    current = os.path.sep
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("%s contains a symlink component: %s" % (label, current))
        leaf = index == len(parts) - 1
        if not leaf and not stat.S_ISDIR(info.st_mode):
            raise ValueError("%s ancestor is not a directory: %s" % (label, current))
        if leaf:
            if expected == "directory" and not stat.S_ISDIR(info.st_mode):
                raise ValueError("%s exists but is not a directory: %s" % (label, path))
            if expected == "file" and not stat.S_ISREG(info.st_mode):
                raise ValueError("%s exists but is not a regular file: %s" % (label, path))


def _validate_external_mapping_targets(mappings: list, dest: str) -> None:
    """Reject external placements that collide with the stack or each other."""
    targets = []
    sources = []
    try:
        for source, target in mappings:
            source = _canonical_absolute_path(source, "external snapshot source")
            target = _canonical_absolute_path(target, "external restore target")
            if (target == dest
                    or target.startswith(dest.rstrip("/") + os.path.sep)
                    or dest.startswith(target.rstrip("/") + os.path.sep)):
                raise ValueError(
                    "external target %s overlaps restored stack %s" % (target, dest)
                )
            sources.append(source)
            targets.append(target)
        for label, paths in (("snapshot sources", sources), ("restore targets", targets)):
            ordered = sorted(set(paths))
            for index, path in enumerate(ordered):
                for other in ordered[index + 1:]:
                    if other.startswith(path.rstrip("/") + os.path.sep):
                        raise ValueError("external %s overlap: %s contains %s"
                                         % (label, path, other))
    except ValueError as exc:
        raise util.CommandError(["external-bind-descriptors"], 1, str(exc))


def _reserve_restore_directory(dest: str, *, force: bool):
    """Atomically reserve and lock the exact target directory for this run."""
    parent_fd = target_fd = -1
    try:
        parent_fd, target_name = _open_parent_dir_fd(dest, create=True)
        info = _lstat_at(parent_fd, target_name)
        original_mode = None  # type: Optional[int]
        if info is None:
            os.mkdir(target_name, 0o700, dir_fd=parent_fd)
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Restore target is not a directory: %s" % dest)
            original_mode = stat.S_IMODE(info.st_mode)
        target_fd = _open_dir_at(parent_fd, target_name)
        entries = os.listdir(target_fd)
        if entries and not force:
            raise util.CommandError(
                ["restore", dest], 1,
                "Target exists and is not empty (--force to overwrite).",
            )
        # Keep an existing world/app-writable empty directory from gaining new
        # entries during the long restic run. Source-root metadata is applied
        # after placement; on an early failure the caller restores this mode.
        os.fchmod(target_fd, 0o700)
        return target_fd, original_mode, original_mode is None
    except Exception:
        if target_fd >= 0:
            os.close(target_fd)
        raise
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _make_restore_scratch(
    name: str, dest: str, *, dest_fd: Optional[int] = None,
) -> _RestoreScratch:
    """Create root-only restic scratch on the destination filesystem.

    An existing destination can itself be a mountpoint, so its parent is not
    necessarily on the same device. In that case create scratch inside the target;
    for a missing target use its parent so the whole restored tree can be renamed.
    The directory and its parent remain open: restic receives the directory FD and
    cleanup uses unlinkat-style operations, so a later path swap cannot redirect
    privileged writes or deletion.
    """
    prefix = ".docker-backup-restore.%s." % name
    if util.DRY_RUN:
        parent = os.path.dirname(dest.rstrip("/")) or "/"
        base = dest if os.path.isdir(dest) else parent
        scratch_name = prefix + str(os.getpid())
        return _RestoreScratch(os.path.join(base, scratch_name), -1, -1, scratch_name)
    # Production reserves ``dest`` first and supplies its descriptor. Keep the
    # descriptor-less branch for direct callers/tests: a missing destination is
    # not created merely to hold scratch, so scratch lives beside it as before.
    if dest_fd is not None:
        base = dest
        base_fd = os.dup(dest_fd)
    else:
        base = dest if os.path.isdir(dest) else (
            os.path.dirname(dest.rstrip("/")) or "/"
        )
        base_fd = _open_absolute_dir_fd(base, create=True)
    scratch_fd = -1
    scratch_name = ""
    try:
        for _attempt in range(128):
            scratch_name = prefix + secrets.token_hex(8)
            try:
                os.mkdir(scratch_name, 0o700, dir_fd=base_fd)
                break
            except FileExistsError:
                continue
        else:
            raise OSError(errno.EEXIST, "Could not allocate unique restore scratch")
        scratch_fd = _open_dir_at(base_fd, scratch_name)
        return _RestoreScratch(
            os.path.join(base, scratch_name), scratch_fd, base_fd, scratch_name,
        )
    except Exception:
        if scratch_fd >= 0:
            os.close(scratch_fd)
        if scratch_name:
            try:
                os.rmdir(scratch_name, dir_fd=base_fd)
            except OSError:
                pass
        os.close(base_fd)
        raise


def _cleanup_restore_scratch(scratch: _RestoreScratch) -> None:
    if scratch.fd < 0:
        return
    try:
        for child in os.listdir(scratch.fd):
            _remove_entry_at(scratch.fd, child)
        _assert_open_entry(scratch.parent_fd, scratch.name, scratch.fd)
        os.close(scratch.fd)
        scratch.fd = -1
        os.rmdir(scratch.name, dir_fd=scratch.parent_fd)
    finally:
        if scratch.fd >= 0:
            os.close(scratch.fd)
            scratch.fd = -1
        if scratch.parent_fd >= 0:
            os.close(scratch.parent_fd)
            scratch.parent_fd = -1


def _move_tree(src: str, dest: str) -> None:
    """Move/merge a restored directory without re-resolving host ancestors.

    The command runs as root, so an lstat-then-path-write sequence is unsafe: a
    writable ancestor could be exchanged for a symlink between those operations.
    Both roots and every child operation below them are therefore anchored to
    directory descriptors opened with O_NOFOLLOW.
    """
    if util.DRY_RUN:
        util.info("DRY-RUN: would move %s → %s." % (src, dest))
        return
    _assert_safe_host_target(dest, "restore destination", expected="directory")
    source_parent_fd = target_parent_fd = -1
    try:
        source_parent_fd, source_name = _open_parent_dir_fd(src, create=False)
        target_parent_fd, target_name = _open_parent_dir_fd(dest, create=True)
        placed_fd = _move_tree_at(
            source_parent_fd, source_name, target_parent_fd, target_name, dest,
        )
        os.close(placed_fd)
    finally:
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        if target_parent_fd >= 0:
            os.close(target_parent_fd)


def _move_tree_from_snapshot(
    scratch_fd: int, snapshot_path: str, dest: str, *,
    expected_target_fd: Optional[int] = None, allow_replace: bool = True,
    allowed_existing: Optional[set] = None,
) -> int:
    """Place one absolute snapshot root while keeping its scratch root anchored."""
    _assert_safe_host_target(dest, "restore destination", expected="directory")
    source_parent_fd = target_parent_fd = -1
    try:
        source_parent_fd, source_name = _open_snapshot_parent_fd(
            scratch_fd, snapshot_path,
        )
        target_parent_fd, target_name = _open_parent_dir_fd(dest, create=True)
        return _move_tree_at(
            source_parent_fd, source_name, target_parent_fd, target_name, dest,
            expected_target_fd=expected_target_fd,
            allow_replace=allow_replace,
            allowed_existing=allowed_existing,
        )
    finally:
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        if target_parent_fd >= 0:
            os.close(target_parent_fd)


def _move_tree_at(
    source_parent_fd: int, source_name: str,
    target_parent_fd: int, target_name: str, dest_label: str,
    *, expected_target_fd: Optional[int] = None, allow_replace: bool = True,
    allowed_existing: Optional[set] = None,
) -> int:
    placed_fd = -1
    source_fd = _open_dir_at(source_parent_fd, source_name)
    try:
        target_info = _lstat_at(target_parent_fd, target_name)
        if target_info is None:
            try:
                os.rename(
                    source_name, target_name,
                    src_dir_fd=source_parent_fd, dst_dir_fd=target_parent_fd,
                )
                return os.dup(source_fd)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
            os.mkdir(target_name, 0o700, dir_fd=target_parent_fd)
            target_fd = _open_dir_at(target_parent_fd, target_name)
            try:
                _move_tree_fds(source_fd, target_fd)
                _copy_fd_metadata(source_fd, target_fd)
                placed_fd = os.dup(target_fd)
            finally:
                os.close(target_fd)
        else:
            if not stat.S_ISDIR(target_info.st_mode):
                raise ValueError(
                    "Restore destination is no longer a directory: %s" % dest_label
                )
            target_fd = _open_dir_at(target_parent_fd, target_name)
            try:
                if expected_target_fd is not None:
                    expected = os.fstat(expected_target_fd)
                    opened = os.fstat(target_fd)
                    if ((expected.st_dev, expected.st_ino)
                            != (opened.st_dev, opened.st_ino)):
                        raise OSError(
                            errno.ESTALE, "Restore target directory changed", dest_label,
                        )
                clean_replace = allow_replace and expected_target_fd is not None
                if not allow_replace:
                    unexpected = set(os.listdir(target_fd)) - set(allowed_existing or ())
                    if unexpected:
                        raise FileExistsError(
                            errno.EEXIST,
                            "Restore target gained entries without --force: %s"
                            % ", ".join(sorted(unexpected)),
                            dest_label,
                        )
                elif clean_replace:
                    # --force means a clean replacement, not a merge that keeps
                    # stale files absent from the snapshot (especially excluded
                    # raw DB/GitLab data). Keep only the in-target scratch tree
                    # itself until cleanup. A mount below the destination could
                    # point at unrelated host data (including a same-device bind
                    # mount), so prove every recursively removed entry stays on
                    # its parent's exact Linux mount first. Subsequent appearances
                    # still abort.
                    for existing in os.listdir(target_fd):
                        if existing not in set(allowed_existing or ()):
                            _remove_entry_at(
                                target_fd, existing, refuse_mountpoint=True,
                            )
                # Descriptor-less callers are legacy helpers/tests and retain
                # their historical merge semantics. Production placement pins
                # expected_target_fd and therefore always takes clean_replace.
                _move_tree_fds(
                    source_fd, target_fd,
                    replace=allow_replace and not clean_replace,
                )
                _copy_fd_metadata(source_fd, target_fd)
                _assert_open_entry(target_parent_fd, target_name, target_fd)
                placed_fd = os.dup(target_fd)
            finally:
                os.close(target_fd)
        _assert_open_entry(source_parent_fd, source_name, source_fd)
    except Exception:
        if placed_fd >= 0:
            os.close(placed_fd)
        raise
    finally:
        os.close(source_fd)
    try:
        os.rmdir(source_name, dir_fd=source_parent_fd)
    except Exception:
        if placed_fd >= 0:
            os.close(placed_fd)
        raise
    return placed_fd


def _required_dir_open_flags() -> int:
    missing = [name for name in ("O_DIRECTORY", "O_NOFOLLOW") if not hasattr(os, name)]
    if missing:
        raise RuntimeError("Safe restore requires %s support." % ", ".join(missing))
    return (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0))


def _fd_host_path(fd: int) -> str:
    """Stable Linux path to a directory held open by this restore process."""
    if not os.path.isdir("/proc/self/fd"):
        raise RuntimeError("Safe restore lifecycle requires Linux /proc file descriptors.")
    return "/proc/%d/fd/%d" % (os.getpid(), fd)


def _fd_sha256(fd: int) -> str:
    """Hash a held regular file without changing its shared file offset."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Expected a regular file descriptor.")
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _seal_memfd(fd: int) -> None:
    required = (
        "F_ADD_SEALS", "F_SEAL_SEAL", "F_SEAL_SHRINK",
        "F_SEAL_GROW", "F_SEAL_WRITE",
    )
    if (not hasattr(os, "MFD_ALLOW_SEALING")
            or not all(hasattr(fcntl, name) for name in required)):
        return
    seals = (
        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    )
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)


def _copy_fd_to_memfd(source_fd: int, label: str):
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("Safe restore requires Linux memfd_create.")
    flags = (
        getattr(os, "MFD_CLOEXEC", 0)
        | getattr(os, "MFD_ALLOW_SEALING", 0)
    )
    target_fd = os.memfd_create(label, flags)
    digest = hashlib.sha256()
    offset = 0
    try:
        while True:
            chunk = os.pread(source_fd, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
            offset += len(chunk)
        os.fsync(target_fd)
        os.fchmod(target_fd, 0o400)
        os.lseek(target_fd, 0, os.SEEK_SET)
        _seal_memfd(target_fd)
        return target_fd, digest.hexdigest()
    except Exception:
        os.close(target_fd)
        raise


def _assert_path_matches_fd(path: str, opened_fd: int) -> None:
    parent_fd = -1
    try:
        parent_fd, name = _open_parent_dir_fd(path, create=False)
        _assert_open_entry(parent_fd, name, opened_fd)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _open_target_fd(path: str, *, expected: str) -> int:
    parent_fd = -1
    try:
        parent_fd, name = _open_parent_dir_fd(path, create=False)
        before = _lstat_at(parent_fd, name)
        wanted = stat.S_ISDIR if expected == "directory" else stat.S_ISREG
        if before is None or not wanted(before.st_mode):
            raise ValueError("Placed target changed type: %s" % path)
        flags = (_required_dir_open_flags() if expected == "directory"
                 else os.O_RDONLY | os.O_NOFOLLOW)
        fd = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(fd)
            raise OSError(errno.ESTALE, "Placed target changed", path)
        return fd
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _close_path_fds(items: list) -> None:
    while items:
        _path, fd = items.pop()
        try:
            os.close(fd)
        except OSError:
            pass


def _close_trusted_file_fds(items: list) -> None:
    while items:
        _path, fd, _digest = items.pop()
        try:
            os.close(fd)
        except OSError:
            pass


def _open_absolute_dir_fd(path: str, *, create: bool) -> int:
    """Open an absolute directory by walking from / with no symlink traversal."""
    if (not isinstance(path, str) or not os.path.isabs(path) or path.startswith("//")
            or os.path.normpath(path) != path):
        raise ValueError("Directory path must be canonical and absolute: %r" % path)
    fd = os.open(os.path.sep, _required_dir_open_flags())
    try:
        for part in (part for part in path.split(os.path.sep) if part):
            try:
                next_fd = os.open(part, _required_dir_open_flags(), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                next_fd = os.open(part, _required_dir_open_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_parent_dir_fd(path: str, *, create: bool):
    """Open an absolute path's parent one component at a time, never following links."""
    path = _canonical_absolute_path(path, "placement path")
    parent = os.path.dirname(path) or os.path.sep
    return _open_absolute_dir_fd(parent, create=create), os.path.basename(path)


def _open_snapshot_parent_fd(scratch_fd: int, snapshot_path: str):
    """Open an absolute-in-snapshot path relative to the held scratch root."""
    snapshot_path = _canonical_absolute_path(snapshot_path, "snapshot path")
    parts = [part for part in snapshot_path.split(os.path.sep) if part]
    fd = os.dup(scratch_fd)
    try:
        for part in parts[:-1]:
            next_fd = _open_dir_at(fd, part)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except Exception:
        os.close(fd)
        raise


def _snapshot_root_info(scratch_fd: int, snapshot_path: str):
    parent_fd = -1
    try:
        parent_fd, name = _open_snapshot_parent_fd(scratch_fd, snapshot_path)
        return _lstat_at(parent_fd, name)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _lstat_at(parent_fd: int, name: str):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_dir_at(parent_fd: int, name: str) -> int:
    before = _lstat_at(parent_fd, name)
    if before is None or not stat.S_ISDIR(before.st_mode):
        raise ValueError("Restored directory entry is missing or unsafe: %s" % name)
    fd = os.open(name, _required_dir_open_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(errno.ESTALE, "Directory entry changed while opening", name)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_regular_at(parent_fd: int, name: str) -> int:
    """Open one restored regular file and bind its exact inode for this run."""
    before = _lstat_at(parent_fd, name)
    if before is None or not stat.S_ISREG(before.st_mode):
        raise ValueError("Restored regular file is missing or unsafe: %s" % name)
    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(errno.ESTALE, "File entry changed while opening", name)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_relative_entry_fd(
    root_fd: int, relative: str, *, expected: Optional[str] = None,
    create_directory: bool = False,
) -> int:
    """Open one exact path below a retained root without following symlinks."""
    if (not isinstance(relative, str) or not relative or "\0" in relative
            or os.path.isabs(relative) or os.path.normpath(relative) != relative):
        raise ValueError("Unsafe runtime Compose source suffix: %r" % relative)
    parts = relative.split(os.path.sep)
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if _lstat_at(parent_fd, part) is None and create_directory:
                try:
                    os.mkdir(part, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = _open_dir_at(parent_fd, part)
            os.close(parent_fd)
            parent_fd = next_fd
        info = _lstat_at(parent_fd, parts[-1])
        if info is None and create_directory:
            try:
                os.mkdir(parts[-1], 0o755, dir_fd=parent_fd)
            except FileExistsError:
                pass
            info = _lstat_at(parent_fd, parts[-1])
        if info is None:
            raise ValueError("Runtime Compose source is missing: %s" % relative)
        if stat.S_ISDIR(info.st_mode):
            opened_fd = _open_dir_at(parent_fd, parts[-1])
            actual = "directory"
        elif stat.S_ISREG(info.st_mode):
            opened_fd = _open_regular_at(parent_fd, parts[-1])
            actual = "file"
        else:
            raise ValueError(
                "Runtime Compose source is a symlink or special file: %s" % relative
            )
        if expected is not None and actual != expected:
            os.close(opened_fd)
            raise ValueError(
                "Runtime Compose source %s must be a %s." % (relative, expected)
            )
        return opened_fd
    finally:
        os.close(parent_fd)


def _assert_open_entry(parent_fd: int, name: str, opened_fd: int) -> None:
    current = _lstat_at(parent_fd, name)
    opened = os.fstat(opened_fd)
    if (current is None
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
        raise OSError(errno.ESTALE, "Directory entry changed during restore", name)


def _move_tree_fds(source_fd: int, target_fd: int, *, replace: bool = True) -> None:
    """Merge two already-open directories using only *at operations."""
    for name in os.listdir(source_fd):
        source_info = _lstat_at(source_fd, name)
        if source_info is None:
            raise OSError(errno.ESTALE, "Restored entry disappeared", name)
        if not (stat.S_ISDIR(source_info.st_mode)
                or stat.S_ISREG(source_info.st_mode)
                or stat.S_ISLNK(source_info.st_mode)):
            raise ValueError("Refusing to place special restored entry: %s" % name)
        target_info = _lstat_at(target_fd, name)
        # With no replacement permission, *any* child that appears after the
        # target's emptiness check aborts. In particular, do not recursively
        # merge a same-named directory and preserve attacker-created children.
        if target_info is not None and not replace:
            raise FileExistsError(
                errno.EEXIST, "Restore target entry appeared", name,
            )
        if (stat.S_ISDIR(source_info.st_mode) and target_info is not None
                and stat.S_ISDIR(target_info.st_mode)):
            source_child_fd = _open_dir_at(source_fd, name)
            target_child_fd = -1
            try:
                target_child_fd = _open_dir_at(target_fd, name)
                _move_tree_fds(source_child_fd, target_child_fd, replace=replace)
                _assert_open_entry(target_fd, name, target_child_fd)
                _assert_open_entry(source_fd, name, source_child_fd)
            finally:
                os.close(source_child_fd)
                if target_child_fd >= 0:
                    os.close(target_child_fd)
            os.rmdir(name, dir_fd=source_fd)
            continue
        if target_info is not None:
            _remove_entry_at(target_fd, name)
        _move_entry_at(source_fd, name, target_fd, name, replace=replace)


def _mount_id_for_fd(fd: int) -> int:
    """Return Linux's mount identity for one retained descriptor.

    ``st_dev`` cannot identify bind mounts because a bind of the same filesystem
    keeps the same device number. Linux exposes the descriptor's exact mount ID
    in ``/proc/self/fdinfo``; using the already-open FD avoids re-resolving a
    mutable destination pathname. If procfs/the field is unavailable, callers
    fail closed instead of guessing from device numbers.
    """
    path = "/proc/self/fdinfo/%d" % fd
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        info_fd = os.open(path, flags)
    except OSError as exc:
        raise OSError(
            errno.EOPNOTSUPP,
            "cannot read the retained entry's Linux mount identity",
            path,
        ) from exc
    try:
        with os.fdopen(info_fd, "r", encoding="ascii", errors="strict") as stream:
            mount_ids = []
            for line in stream:
                key, separator, value = line.partition(":")
                if key != "mnt_id" or not separator:
                    continue
                value = value.strip()
                if not value.isdigit() or int(value) <= 0:
                    raise OSError(
                        errno.EIO, "invalid Linux mount identity", path,
                    )
                mount_ids.append(int(value))
    except Exception:
        # fdopen owns info_fd after construction, including on parse/read errors.
        raise
    if len(mount_ids) != 1:
        raise OSError(
            errno.EOPNOTSUPP,
            "Linux mount identity is unavailable or ambiguous",
            path,
        )
    return mount_ids[0]


def _assert_not_mountpoint_fds(parent_fd: int, child_fd: int, name: str) -> None:
    """Fail unless an exact child descriptor belongs to its parent's mount."""
    try:
        parent_mount = _mount_id_for_fd(parent_fd)
        child_mount = _mount_id_for_fd(child_fd)
    except (OSError, UnicodeError) as exc:
        raise util.CommandError(
            ["restore", "clean-replace", name], 1,
            "Cannot prove that destination entry %r is not a mountpoint; "
            "refusing recursive deletion: %s" % (name, exc),
        ) from exc
    if parent_mount != child_mount:
        raise util.CommandError(
            ["restore", "clean-replace", name], 1,
            "Refusing to clean-replace mounted destination entry: %s" % name,
        )
    # Catch a pathname swap (including a mount attached after open) before the
    # first recursive operation. The retained child FD still cannot be redirected.
    _assert_open_entry(parent_fd, name, child_fd)


def _remove_entry_at(
    parent_fd: int, name: str, *, refuse_mountpoint: bool = False,
) -> None:
    """Remove one exact entry recursively without following a symlink."""
    info = _lstat_at(parent_fd, name)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode):
        child_fd = _open_dir_at(parent_fd, name)
        try:
            if refuse_mountpoint:
                _assert_not_mountpoint_fds(parent_fd, child_fd, name)
            for child in os.listdir(child_fd):
                _remove_entry_at(
                    child_fd, child, refuse_mountpoint=refuse_mountpoint,
                )
            _assert_open_entry(parent_fd, name, child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    elif stat.S_ISREG(info.st_mode):
        if refuse_mountpoint:
            child_fd = _open_regular_at(parent_fd, name)
            try:
                _assert_not_mountpoint_fds(parent_fd, child_fd, name)
            finally:
                os.close(child_fd)
        os.unlink(name, dir_fd=parent_fd)
    elif stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise ValueError("Refusing to replace special target entry: %s" % name)


def _move_entry_at(
    source_parent_fd: int, source_name: str,
    target_parent_fd: int, target_name: str, *, replace: bool = True,
) -> None:
    """Move one exact source entry between anchored directory descriptors."""
    if not replace:
        source_info = _lstat_at(source_parent_fd, source_name)
        if source_info is None:
            raise OSError(errno.ESTALE, "Restored entry disappeared", source_name)
        if stat.S_ISDIR(source_info.st_mode):
            os.mkdir(target_name, 0o700, dir_fd=target_parent_fd)
            source_fd = _open_dir_at(source_parent_fd, source_name)
            target_fd = -1
            try:
                target_fd = _open_dir_at(target_parent_fd, target_name)
                _move_tree_fds(source_fd, target_fd, replace=False)
                _copy_fd_metadata(source_fd, target_fd)
                _assert_open_entry(source_parent_fd, source_name, source_fd)
                _assert_open_entry(target_parent_fd, target_name, target_fd)
            finally:
                os.close(source_fd)
                if target_fd >= 0:
                    os.close(target_fd)
            os.rmdir(source_name, dir_fd=source_parent_fd)
            return
        if stat.S_ISREG(source_info.st_mode):
            try:
                os.link(
                    source_name, target_name,
                    src_dir_fd=source_parent_fd, dst_dir_fd=target_parent_fd,
                    follow_symlinks=False,
                )
                os.unlink(source_name, dir_fd=source_parent_fd)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
            _copy_sparse_file_at(
                source_parent_fd, source_name, target_parent_fd, target_name,
            )
            return
        if stat.S_ISLNK(source_info.st_mode):
            link_target = os.readlink(source_name, dir_fd=source_parent_fd)
            os.symlink(link_target, target_name, dir_fd=target_parent_fd)
            os.unlink(source_name, dir_fd=source_parent_fd)
            return
        raise ValueError("Refusing to place special restored entry: %s" % source_name)
    try:
        os.rename(
            source_name, target_name,
            src_dir_fd=source_parent_fd, dst_dir_fd=target_parent_fd,
        )
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    source_info = _lstat_at(source_parent_fd, source_name)
    if source_info is None:
        raise OSError(errno.ESTALE, "Restored entry disappeared", source_name)
    if stat.S_ISDIR(source_info.st_mode):
        os.mkdir(target_name, 0o700, dir_fd=target_parent_fd)
        source_fd = _open_dir_at(source_parent_fd, source_name)
        target_fd = -1
        try:
            target_fd = _open_dir_at(target_parent_fd, target_name)
            _move_tree_fds(source_fd, target_fd)
            _copy_fd_metadata(source_fd, target_fd)
            _assert_open_entry(source_parent_fd, source_name, source_fd)
            _assert_open_entry(target_parent_fd, target_name, target_fd)
        finally:
            os.close(source_fd)
            if target_fd >= 0:
                os.close(target_fd)
        os.rmdir(source_name, dir_fd=source_parent_fd)
    elif stat.S_ISREG(source_info.st_mode):
        _copy_sparse_file_at(
            source_parent_fd, source_name, target_parent_fd, target_name,
        )
    elif stat.S_ISLNK(source_info.st_mode):
        link_target = os.readlink(source_name, dir_fd=source_parent_fd)
        os.symlink(link_target, target_name, dir_fd=target_parent_fd)
        os.unlink(source_name, dir_fd=source_parent_fd)
    else:
        raise ValueError("Refusing to place special restored entry: %s" % source_name)


def _move_regular_file(src: str, dest: str, *, replace: bool) -> None:
    """Place an external regular file through anchored parent descriptors."""
    source_parent_fd = target_parent_fd = -1
    try:
        source_parent_fd, source_name = _open_parent_dir_fd(src, create=False)
        target_parent_fd, target_name = _open_parent_dir_fd(dest, create=True)
        placed_fd = _move_regular_file_at(
            source_parent_fd, source_name, target_parent_fd, target_name,
            dest, replace=replace,
        )
        os.close(placed_fd)
    finally:
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        if target_parent_fd >= 0:
            os.close(target_parent_fd)


def _move_regular_file_from_snapshot(
    scratch_fd: int, snapshot_path: str, dest: str, *, replace: bool,
) -> int:
    source_parent_fd = target_parent_fd = -1
    try:
        source_parent_fd, source_name = _open_snapshot_parent_fd(
            scratch_fd, snapshot_path,
        )
        target_parent_fd, target_name = _open_parent_dir_fd(dest, create=True)
        return _move_regular_file_at(
            source_parent_fd, source_name, target_parent_fd, target_name,
            dest, replace=replace,
        )
    finally:
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        if target_parent_fd >= 0:
            os.close(target_parent_fd)


def _move_regular_file_at(
    source_parent_fd: int, source_name: str,
    target_parent_fd: int, target_name: str,
    dest_label: str, *, replace: bool,
) -> int:
    source_info = _lstat_at(source_parent_fd, source_name)
    if source_info is None or not stat.S_ISREG(source_info.st_mode):
        raise ValueError("Restored source is not a regular file: %s" % source_name)
    target_info = _lstat_at(target_parent_fd, target_name)
    if target_info is not None:
        if not replace:
            raise FileExistsError(errno.EEXIST, "External target appeared", dest_label)
        if stat.S_ISDIR(target_info.st_mode):
            raise ValueError("External file target became a directory: %s" % dest_label)
        if not (stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode)):
            raise ValueError("Refusing to replace special external target: %s" % dest_label)
        os.unlink(target_name, dir_fd=target_parent_fd)
    _move_entry_at(
        source_parent_fd, source_name, target_parent_fd, target_name,
        replace=replace,
    )
    placed_info = _lstat_at(target_parent_fd, target_name)
    if placed_info is None or not stat.S_ISREG(placed_info.st_mode):
        raise OSError(errno.ESTALE, "Placed external file changed", dest_label)
    placed_fd = os.open(
        target_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=target_parent_fd,
    )
    opened = os.fstat(placed_fd)
    if (opened.st_dev, opened.st_ino) != (placed_info.st_dev, placed_info.st_ino):
        os.close(placed_fd)
        raise OSError(errno.ESTALE, "Placed external file changed", dest_label)
    return placed_fd


def _copy_fd_metadata(source_fd: int, target_fd: int) -> None:
    info = os.fstat(source_fd)
    if hasattr(os, "fchown") and hasattr(os, "geteuid") and os.geteuid() == 0:
        os.fchown(target_fd, info.st_uid, info.st_gid)
    os.fchmod(target_fd, stat.S_IMODE(info.st_mode))
    if all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")):
        for attribute in os.listxattr(source_fd):
            os.setxattr(target_fd, attribute, os.getxattr(source_fd, attribute))
    os.utime(target_fd, ns=(info.st_atime_ns, info.st_mtime_ns))


def _copy_sparse_file_at(
    source_parent_fd: int, source_name: str,
    target_parent_fd: int, target_name: str,
) -> None:
    """Sparse cross-filesystem copy using no-follow, exact-parent descriptors."""
    source_fd = os.open(
        source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent_fd,
    )
    target_fd = -1
    target_info = None
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError("Restored source is not a regular file: %s" % source_name)
        target_fd = os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=target_parent_fd,
        )
        target_info = os.fstat(target_fd)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            if not chunk.strip(b"\0"):
                os.lseek(target_fd, len(chunk), os.SEEK_CUR)
            else:
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
        os.ftruncate(target_fd, source_info.st_size)
        _copy_fd_metadata(source_fd, target_fd)
        os.fsync(target_fd)
        current_source = _lstat_at(source_parent_fd, source_name)
        if (current_source is None or (current_source.st_dev, current_source.st_ino)
                != (source_info.st_dev, source_info.st_ino)):
            raise OSError(errno.ESTALE, "Source changed during sparse copy", source_name)
    except Exception:
        if target_fd >= 0:
            os.close(target_fd)
            target_fd = -1
        current_target = _lstat_at(target_parent_fd, target_name)
        if (target_info is not None and current_target is not None
                and (current_target.st_dev, current_target.st_ino)
                == (target_info.st_dev, target_info.st_ino)):
            os.unlink(target_name, dir_fd=target_parent_fd)
        raise
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)
    os.unlink(source_name, dir_fd=source_parent_fd)
