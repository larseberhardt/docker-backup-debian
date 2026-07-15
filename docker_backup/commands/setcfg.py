"""``set <name>`` — change individual fields of a configured stack.

Safe to change: ``--schedule`` (rewrites the drop-in), ``--retention`` and
``--offsite``. ``--target`` changes the repo path and orphans the old repo — hence
only with an explicit ``--i-know-this-orphans-the-old-repo``.
"""

from __future__ import annotations

from typing import Dict

from .. import compose, config, hooks, restic, systemd_units, util
from . import create as create_cmd


def _parse_retention(spec: str) -> Dict[str, int]:
    parts = (spec or "").split("/")
    if len(parts) != 3:
        raise util.CommandError(["--retention"], 2,
                                "Format: daily/weekly/monthly, e.g. '7/4/6'.")
    try:
        vals = [int(p) for p in parts]
    except ValueError:
        raise util.CommandError(["--retention"], 2, "Retention values must be integers.")
    if any(v < 0 for v in vals):
        raise util.CommandError(["--retention"], 2, "Retention values must not be negative.")
    return {"daily": vals[0], "weekly": vals[1], "monthly": vals[2]}


def _preserve_db_operator_overrides(refreshed: list, old_by_service: dict) -> None:
    """Keep DB settings that may have been set explicitly via ``set``.

    Legacy configs do not record override provenance.  Preserving a differing
    dump role/password pair can make a stale credential fail loudly, while
    silently replacing it can reduce privileges and omit data.  Likewise,
    changing ``dump_globals`` can make a previously portable Postgres backup
    incomplete. Explicit flags supplied in the same command are applied after
    this function and therefore still win.
    """
    for db in refreshed:
        old = old_by_service.get(db.get("service")) or {}
        if old.get("engine") != db.get("engine"):
            continue

        old_user = old.get("auth_user")
        if old_user and old_user != db.get("auth_user"):
            db["auth_user"] = old_user
            if isinstance(old.get("password_source"), str):
                db["password_source"] = old["password_source"]
        elif (db.get("password_source") == "none"
              and old.get("password_source") == "stored"):
            # An older interactive setup may have a password sidecar while the
            # current Compose model still exposes no password environment key.
            db["password_source"] = "stored"
            db["auth_user"] = old_user or db.get("auth_user")

        if (db.get("engine") == "postgres"
                and isinstance(old.get("dump_globals"), bool)):
            db["dump_globals"] = old["dump_globals"]


def _describe_db_plan(db: dict) -> str:
    if db.get("database_scope") == "non-system":
        scope = "all non-system databases (seed: %s)" % (
            ",".join(db.get("databases") or []) or "<none>"
        )
    else:
        scope = "all databases" if db.get("all_databases") else ",".join(
            db.get("databases") or []
        )
    details = "%s=%s; user=%s" % (
        db.get("service"), scope or "<none>", db.get("auth_user") or "<default>",
    )
    if db.get("engine") == "postgres":
        details += "; globals=%s" % ("yes" if db.get("dump_globals") else "no")
    return details


def cmd_set(args) -> int:
    util.require_root()
    name = config.sanitize_name(args.name)
    if not config.exists(name):
        util.error("No config named '%s'." % name)
        return 1
    with util.FileLock(config.mutation_lock_path(name)):
        # Recheck after acquiring the same per-name lock used by create/restore.
        if not config.exists(name):
            util.error("Config '%s' disappeared while waiting for the lock." % name)
            return 1
        return _cmd_set_locked(args, name)


def _cmd_set_locked(args, name: str) -> int:
    cfg = config.load(name)
    changed = False
    reschedule = False

    if args.schedule is not None:
        oncal = systemd_units.oncalendar_from(args.schedule)
        if not util.DRY_RUN and not systemd_units.validate_oncalendar(oncal):
            util.warn("OnCalendar '%s' could not be validated (systemd-analyze)." % oncal)
        cfg.setdefault("schedule", {})
        cfg["schedule"]["input"] = args.schedule
        cfg["schedule"]["oncalendar"] = oncal
        changed = True
        reschedule = True

    if args.retention is not None:
        new_ret = _parse_retention(args.retention)
        # preserve the existing age window (--keep-within) across a count change
        new_ret["keep_within"] = (cfg.get("retention") or {}).get("keep_within")
        cfg["retention"] = new_ret
        changed = True

    if getattr(args, "offsite_retention", None) is not None:
        cfg["offsite_retention"] = _parse_retention(args.offsite_retention)
        util.info("Offsite retention set (default is the primary retention).")
        changed = True

    q = getattr(args, "quiesce", None)
    if q is not None:
        cfg["quiesce_disabled"] = not q
        if q:
            if not (cfg.get("quiesce_services") or []):
                util.warn("No quiesce services recorded — detection happens at "
                          "'create' (recreate the stack with --force to re-detect).")
            util.info("Quiesce enabled.")
        else:
            util.warn("Quiesce disabled — mongo/redis are captured as a live file "
                      "copy only (crash-consistent).")
        changed = True

    offsite_prune = getattr(args, "offsite_prune", None)
    if offsite_prune is not None:
        cfg["offsite_prune"] = bool(offsite_prune)
        if offsite_prune:
            util.info("Offsite pruning enabled.")
        else:
            util.warn("Offsite pruning disabled — the offsite repo keeps EVERY copied "
                      "snapshot and grows unbounded. Watch its disk usage.")
        changed = True

    if getattr(args, "no_keep_within", False):
        cfg.setdefault("retention", dict(config.DEFAULT_RETENTION))["keep_within"] = None
        changed = True
    keep_within = getattr(args, "keep_within", None)
    if keep_within is not None:
        cfg.setdefault("retention", dict(config.DEFAULT_RETENTION))["keep_within"] = keep_within
        changed = True

    if getattr(args, "exclude_clear", False):
        cfg["exclude_patterns"] = []
        util.info("Custom exclude patterns removed.")
        changed = True
    new_excludes = getattr(args, "exclude", None)
    if new_excludes:
        pats = cfg.setdefault("exclude_patterns", [])
        for p in new_excludes:
            p = restic.validate_exclude_pattern(p)
            if p not in pats:
                pats.append(p)
        changed = True

    # --- Hooks ---
    hooks_modified = False
    for argname, phase in (("pre_cmd", "pre_backup"), ("post_cmd", "post_backup"),
                           ("restore_cmd", "restore")):
        val = getattr(args, argname, None)
        if val is None:
            continue
        h = cfg.setdefault("hooks", {"pre_backup": [], "post_backup": [], "restore": []})
        h[phase] = [] if val == "" else [hooks.make_hook(val, phase=phase)]
        util.info("Hook '%s' %s." % (phase, "removed" if val == "" else "set"))
        hooks_modified = True

    if getattr(args, "clear_hooks", False):
        cfg["hooks"] = {"pre_backup": [], "post_backup": [], "restore": []}
        util.info("All hooks removed.")
        hooks_modified = True

    if hooks_modified:
        # Changed commands must be re-approved (fingerprint is now stale).
        hooks.revoke(cfg)
        changed = True

    allow = getattr(args, "allow_hooks", None)
    if allow is True:
        cmds = hooks.describe_commands(cfg)
        if not cmds:
            util.warn("No hook commands stored — --allow-hooks has no effect.")
        else:
            util.warn("These commands will run as ROOT on the timer from now on:")
            for ph, c in cmds:
                util.warn("    [%s] %s" % (ph, c))
            hooks.approve(cfg)
            util.info("Hooks allowed.")
            changed = True
    elif allow is False:
        if cfg.get("hooks_allowed") or cfg.get("hooks_fingerprint"):
            hooks.revoke(cfg)
            util.info("Hook approval revoked.")
            changed = True

    if args.offsite is not None:
        cfg["offsite"] = compose.repo_for(args.offsite, name) if args.offsite else None
        changed = True

    if getattr(args, "refresh_db_detection", False):
        stack_path = cfg.get("stack_path")
        compose_file = cfg.get("compose_file")
        if not stack_path or not compose_file:
            raise util.CommandError(
                ["set", name, "--refresh-db-detection"], 2,
                "The stored config has no stack_path/compose_file; repair or review "
                "those fields before refreshing. No configuration was changed.",
            )
        cj = compose.config_json(compose_file, stack_path, cfg.get("project_name"))
        refreshed = create_cmd._build_db_services(cj, name, interactive=False)
        if not refreshed:
            raise util.CommandError(
                ["set", name, "--refresh-db-detection"], 1,
                "No supported database was detected; the existing DB configuration "
                "was left unchanged.",
            )

        old_by_service = {
            db.get("service"): db for db in (cfg.get("db_services") or [])
            if isinstance(db, dict) and db.get("service")
        }
        _preserve_db_operator_overrides(refreshed, old_by_service)

        exclude_paths, named_volumes = compose.collect_volume_backup_plan(cj, refreshed)
        cfg["db_services"] = refreshed
        cfg["exclude_paths"] = exclude_paths
        cfg["named_volumes"] = named_volumes
        cfg["db_autodetect"] = True
        cfg["db_scope_version"] = config.DB_SCOPE_VERSION
        old_scopes = [_describe_db_plan(db) for db in old_by_service.values()]
        scopes = [_describe_db_plan(db) for db in refreshed]
        util.info("DB detection refreshed: %s -> %s." % (
            "; ".join(old_scopes) or "<none>", "; ".join(scopes),
        ))
        changed = True

    if getattr(args, "dump_user", None) is not None:
        dbsvcs = cfg.get("db_services") or []
        if not dbsvcs:
            util.warn("No DB services in '%s' — --dump-user has no effect." % name)
        for db in dbsvcs:
            db["auth_user"] = args.dump_user
        if dbsvcs:
            util.info("DB dump role set to '%s' (%d service(s))."
                      % (args.dump_user, len(dbsvcs)))
            changed = True

    if getattr(args, "dump_globals", None) is not None:
        pg = [db for db in (cfg.get("db_services") or []) if db.get("engine") == "postgres"]
        if not pg:
            util.warn("No Postgres services in '%s' — --dump-globals has no effect." % name)
        for db in pg:
            db["dump_globals"] = bool(args.dump_globals)
        if pg:
            util.info("dump_globals=%s for %d Postgres service(s)."
                      % (bool(args.dump_globals), len(pg)))
            changed = True

    if args.target is not None:
        if not args.ack_dangerous:
            util.error(
                "--target changes the repo path and orphans the old repo (data + "
                "previous snapshots are left behind there). To confirm, pass "
                "--i-know-this-orphans-the-old-repo."
            )
            return 2
        cfg["repo"] = compose.repo_for(args.target, name)
        cfg["mount_check"] = args.target if compose.is_local_repo(args.target) else None
        changed = True
        util.warn("Repo target changed to %s. The old repo is left untouched; a new one "
                  "is initialized on the next run." % cfg["repo"])

    if not changed:
        util.info("Nothing to change.")
        return 0

    if util.DRY_RUN:
        util.info("DRY-RUN: would update config '%s'." % name)
    else:
        config.save(cfg)
        util.info("Config '%s' updated." % name)

    if reschedule:
        delay = (cfg.get("schedule") or {}).get("randomized_delay_sec", 300)
        if util.DRY_RUN:
            util.info("DRY-RUN: would rewrite the drop-in (OnCalendar=%s)."
                      % cfg["schedule"]["oncalendar"])
        else:
            systemd_units.write_schedule_dropin(name, cfg["schedule"]["oncalendar"], delay)
            systemd_units.daemon_reload()
    return 0
