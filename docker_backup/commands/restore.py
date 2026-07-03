"""``restore <dest>`` — full restore incl. DB import.

End state: the stack is fully stopped, the data is in place — only
``docker compose up -d`` is still needed.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Optional

from .. import compose, config, dbdump, hooks, manifest, restic, runtime, util, volumes, wizard

_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def cmd_restore(args) -> int:
    util.require_root()
    dest = os.path.abspath(args.dest)
    snapshot = args.snapshot or "latest"

    if getattr(args, "from_repo", None):
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

    # Custom restore command passed directly on the CLI (mainly for --from-repo, where
    # the manifest deliberately carries NO shell). Entering it on the CLI = approval.
    restore_cmd = getattr(args, "restore_cmd", None)
    if restore_cmd:
        cfg.setdefault("hooks", {"pre_backup": [], "post_backup": [], "restore": []})
        cfg["hooks"]["restore"] = [hooks.make_hook(restore_cmd, phase="restore")]
        hooks.approve(cfg)

    return _run_restore(cfg, name, dest, snapshot, args.force,
                        no_custom_restore=getattr(args, "no_custom_restore", False))


def _run_restore(cfg, name: str, dest: str, snapshot: str, force: bool,
                 no_custom_restore: bool = False) -> int:
    """Core of the restore — identical for the classic and the bootstrap path."""
    if os.path.isdir(dest) and os.listdir(dest) and not force:
        util.error("Target %s exists and is not empty (--force to overwrite)." % dest)
        return 1

    runtime.load_backend_env(cfg)
    if not util.DRY_RUN and not restic.repo_initialized(cfg["repo"], cfg["key_file"]):
        util.error("restic repo not reachable: %s" % cfg["repo"])
        return 1

    scratch = "/var/tmp/docker-backup-restore.%s.%d" % (name, os.getpid())
    restic.restore(cfg["repo"], cfg["key_file"], snapshot, scratch, paths=[cfg["stack_path"]])
    src = _locate_restored(scratch, cfg["stack_path"])
    if not src:
        util.error("Restored stack tree not found under %s." % scratch)
        return 1

    _move_tree(src, dest)
    _restore_extra_paths(cfg, scratch, force)
    shutil.rmtree(scratch, ignore_errors=True)

    new_compose = os.path.join(dest, os.path.basename(cfg["compose_file"]))
    cj = {}  # type: Dict[str, Any]
    if not util.DRY_RUN:
        cj = compose.config_json(new_compose, dest)
    new_project = cj.get("name") or os.path.basename(dest.rstrip("/"))

    _restore_named_volumes(cfg, cj, dest, new_project)

    if no_custom_restore or not hooks.phase_hooks(cfg, "restore"):
        _import_databases(cfg, cj, new_compose, dest, new_project)
        util.info("Restore to %s complete." % dest)
        util.info("Check the env files, then start: docker compose -f %s up -d" % new_compose)
    else:
        _custom_restore(cfg, new_compose, dest, new_project, force)
    return 0


def _custom_restore(cfg, new_compose: str, dest: str, new_project: str, force: bool) -> None:
    """Custom restore command (e.g. GitLab) instead of the built-in DB import.

    The built-in restore leaves the stack stopped; a ``docker exec``-based restore
    hook, however, needs a running container → the stack is brought up first.
    Readiness is the hook's own responsibility (no generic probe).
    """
    cmds = [c for (ph, c) in hooks.describe_commands(cfg) if ph == "restore"]
    util.warn("This restore runs custom commands as ROOT:")
    for c in cmds:
        util.warn("    %s" % c)
    if not force and not util.DRY_RUN:
        if not wizard.confirm("Run these restore commands now?", default=False):
            util.warn("Custom restore command skipped (not confirmed). The stack tree was "
                      "restored but NOT imported — import the data manually or rerun "
                      "with --force.")
            return

    # Confirmation (or --force) = approval for this run. Point stack_path/compose at the
    # target so the hook cwd/env are correct on the restore server.
    hook_cfg = dict(cfg)
    hook_cfg["stack_path"] = dest
    hook_cfg["compose_file"] = new_compose
    hook_cfg["project_name"] = new_project
    hooks.approve(hook_cfg)

    util.info("Bringing the stack up and running the restore command…")
    compose.up_all(new_compose, dest, new_project)
    hooks.run_hooks(hook_cfg, "restore")
    util.info("Custom restore command to %s complete." % dest)


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
    name = config.sanitize_name(
        getattr(args, "bootstrap_name", None) or man.get("name")
        or os.path.basename(dest.rstrip("/"))
    )
    key_file = _resolve_bootstrap_key(args, name)
    if key_file is None:
        return None, None
    cfg = manifest.cfg_from_manifest(man, repo, key_file, name=name)
    _warn_stored_password_source(cfg)
    if getattr(args, "save_config", False) and not util.DRY_RUN:
        util.info("Reconstructed config saved: %s" % config.save(cfg))
    return cfg, name


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


def _restore_extra_paths(cfg, scratch: str, force: bool) -> None:
    """External bind-mount paths from the snapshot → back to their ORIGINAL location.

    Only when the path is missing there; an existing path is merged over only with
    ``--force`` (protects a test restore running next to the live stack)."""
    for p in cfg.get("extra_backup_paths") or []:
        if util.DRY_RUN:
            util.info("DRY-RUN: would restore external path %s." % p)
            continue
        restored = scratch.rstrip("/") + p
        if not os.path.exists(restored):
            util.warn("External path %s is not in the snapshot — skipped." % p)
            continue
        if os.path.exists(p):
            if not force:
                util.warn("External path %s already exists — NOT overwritten "
                          "(--force merges the snapshot over it)." % p)
                continue
            util.warn("External path %s exists — merging snapshot over it (--force)." % p)
            if os.path.isdir(restored):
                _move_tree(restored, p)
            else:
                shutil.copy2(restored, p)
        else:
            parent = os.path.dirname(p.rstrip("/")) or "/"
            os.makedirs(parent, exist_ok=True)
            shutil.move(restored, p)
        util.info("External path restored: %s" % p)


def _restore_named_volumes(cfg, cj, dest, new_project) -> None:
    vols_dir = os.path.join(dest, ".docker-backup", "volumes")
    for nv in cfg.get("named_volumes") or []:
        real = compose.real_volume_name(cj.get("volumes") or {}, nv["key"], new_project)
        compose.create_volume(real)
        volumes.restore_named_volume(real, vols_dir, nv["key"])


def _import_databases(cfg, cj, new_compose, dest, new_project) -> None:
    dumps_dir = os.path.join(dest, ".docker-backup", "dumps")
    for db in cfg.get("db_services") or []:
        password = runtime.resolve_password(cfg, db, cj or None)
        util.info("Starting DB service '%s' for the import…" % db["service"])
        compose.up_service(new_compose, dest, db["service"], new_project)
        if not dbdump.wait_ready(db, password, new_compose, dest, new_project, timeout=180):
            util.warn("DB '%s' not ready; import may fail." % db["service"])
        # import_dump handles globals, multi-DB dumps and the legacy fallback.
        dbdump.import_dump(db, password, new_compose, dest, new_project, dumps_dir)
        # Fully stop the stack (data is preserved) → only `up -d` still needed
        compose.rm_service(new_compose, dest, db["service"], new_project)


def _locate_restored(scratch: str, stack_path: str) -> Optional[str]:
    direct = scratch.rstrip("/") + stack_path  # e.g. /var/tmp/…/opt/xibo
    if util.DRY_RUN:
        return direct
    if os.path.isdir(direct):
        return direct
    for root, _dirs, files in os.walk(scratch):
        if any(f in _COMPOSE_NAMES for f in files):
            return root
    return None


def _move_tree(src: str, dest: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: would move %s → %s." % (src, dest))
        return
    parent = os.path.dirname(dest.rstrip("/")) or "/"
    os.makedirs(parent, exist_ok=True)
    if os.path.isdir(dest):
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dest, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    else:
        shutil.copytree(src, dest)
