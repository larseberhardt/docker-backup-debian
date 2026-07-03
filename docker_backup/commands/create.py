"""``create`` (interactive wizard), ``create --all`` (per-stack selection) and
``create --all --auto`` (fully automatic with a shared target)."""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .. import compose, config, detect, hooks, keys, quiesce, restic, systemd_units, templates, util, wizard


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def cmd_create(args) -> int:
    util.require_root()
    interactive = not args.non_interactive

    if getattr(args, "list_templates", False):
        names = templates.list_templates()
        if not names:
            print("No templates available.")
        else:
            print("Available templates (docker-backup create --from-template <name>):")
            for n in names:
                print("  %s" % n)
        return 0

    # --- Validation ---
    if not args.all and not args.path:
        util.error("Specify a path or use --all.")
        return 2
    if args.all and args.path:
        util.error("Either <path> OR --all, not both.")
        return 2
    if args.auto and not args.all:
        util.error("--auto only applies together with --all.")
        return 2
    if getattr(args, "from_template", None) and args.all:
        util.error("--from-template only applies to a single stack, not with --all.")
        return 2

    if not args.all:
        tmpl = templates.load(args.from_template) if getattr(args, "from_template", None) else None
        # Resolution: explicit CLI flag > template > built-in default.
        db_auto = getattr(args, "db_autodetect", None)
        if db_auto is None:
            db_auto = (tmpl or {}).get("db_autodetect", True)
        excludes = list((tmpl or {}).get("exclude_patterns") or []) \
            + list(getattr(args, "exclude", None) or [])
        if tmpl:
            util.info("Applied template '%s': %s" % (tmpl["name"], tmpl.get("description", "")))
        return create_one(
            args.path,
            target_base=args.target,
            schedule_input=args.schedule or (tmpl or {}).get("schedule"),
            offsite=args.offsite,
            name=args.name,
            force=args.force,
            interactive=interactive,
            dump_user=getattr(args, "dump_user", None),
            dump_globals=getattr(args, "dump_globals", None),
            db_autodetect=db_auto,
            no_quiesce=getattr(args, "no_quiesce", False),
            exclude_patterns=excludes,
            keep_within=getattr(args, "keep_within", None),
            pre_cmd=getattr(args, "pre_cmd", None),
            post_cmd=getattr(args, "post_cmd", None),
            restore_cmd=getattr(args, "restore_cmd", None),
            allow_hooks=getattr(args, "allow_hooks", False),
            hooks_override=templates.to_hooks(tmpl) if tmpl else None,
            retention_override=(tmpl or {}).get("retention"),
            template=templates.provenance(tmpl) if tmpl else None,
        )

    if args.auto:
        return _create_all_auto(args, interactive)
    return _create_all_wizard(args, interactive)


# --- Single stack -----------------------------------------------------------
def create_one(
    stack_path: str,
    *,
    target_base: Optional[str],
    schedule_input: Optional[str],
    offsite: Optional[str],
    name: Optional[str],
    force: bool,
    interactive: bool,
    dump_user: Optional[str] = None,
    dump_globals: Optional[bool] = None,
    db_autodetect: bool = True,
    no_quiesce: bool = False,
    exclude_patterns: Optional[List[str]] = None,
    keep_within: Optional[str] = None,
    pre_cmd: Optional[str] = None,
    post_cmd: Optional[str] = None,
    restore_cmd: Optional[str] = None,
    allow_hooks: bool = False,
    hooks_override: Optional[Dict[str, Any]] = None,
    retention_override: Optional[Dict[str, Any]] = None,
    template: Optional[Dict[str, Any]] = None,
) -> int:
    stack_path = os.path.abspath(stack_path)
    if not os.path.isdir(stack_path):
        util.error("Stack folder not found: %s" % stack_path)
        return 1
    compose_file = compose.find_compose_file(stack_path)
    if not compose_file:
        util.error("No docker-compose.yml found in %s." % stack_path)
        return 1

    name = config.sanitize_name(name or os.path.basename(stack_path.rstrip("/")))

    if config.exists(name) and not force:
        if interactive:
            if not wizard.confirm("Config '%s' already exists. Overwrite?" % name, default=False):
                util.info("Aborted.")
                return 0
        else:
            util.warn("Config '%s' exists; use --force to overwrite. Skipped." % name)
            return 0

    util.info("Reading stack configuration: %s" % compose_file)
    cj = compose.config_json(compose_file, stack_path)
    project_name = cj.get("name") or name

    # Hint at a matching template if none was applied.
    if interactive and hooks_override is None and not (pre_cmd or post_cmd or restore_cmd):
        suggested = templates.detect_template(cj)
        if suggested:
            util.info("Tip: this stack matches the template '%s'. To recreate it with "
                      "matching excludes/hooks, run: 'docker-backup create %s "
                      "--from-template %s'." % (suggested, stack_path, suggested))

    if db_autodetect:
        db_services = _build_db_services(cj, name, interactive, dump_user, dump_globals)
    else:
        util.info("DB auto-detection disabled — no database is dumped automatically "
                  "(e.g. when the app ships its own dump command as a hook).")
        db_services = []
    exclude_paths, named_volumes = compose.collect_volume_backup_plan(cj, db_services)
    if named_volumes:
        util.info("Named volumes detected: %s" % ", ".join(nv["key"] for nv in named_volumes))

    extra_backup_paths = _resolve_external_binds(cj, stack_path, exclude_paths, interactive)

    quiesce_services = _build_quiesce_services(cj, no_quiesce)
    quiesced_names = {q["service"] for q in quiesce_services}
    for st in detect.find_undumpable_stateful(cj):
        if st["service"] in quiesced_names:
            continue  # covered by the built-in quiesce → info instead of warning
        util.warn(
            "Service '%s' (%s) is stateful but has no dump support — its data is "
            "captured as a live file copy only (crash-consistent). For guaranteed "
            "consistency add a pre-backup dump hook (e.g. mongodump) or stop the "
            "service during the backup window." % (st["service"], st["engine"])
        )

    user_excludes = [restic.validate_exclude_pattern(p) for p in (exclude_patterns or [])]

    retention = dict(retention_override or config.DEFAULT_RETENTION)
    retention.setdefault("keep_within", None)
    if keep_within:
        retention["keep_within"] = keep_within

    hooks_dict = {"pre_backup": [], "post_backup": [], "restore": []}
    if hooks_override:  # from a template
        for ph in hooks_dict:
            hooks_dict[ph] = list(hooks_override.get(ph) or [])
    if pre_cmd:  # explicit CLI flags override the template per phase
        hooks_dict["pre_backup"] = [hooks.make_hook(pre_cmd, phase="pre_backup")]
    if post_cmd:
        hooks_dict["post_backup"] = [hooks.make_hook(post_cmd, phase="post_backup")]
    if restore_cmd:
        hooks_dict["restore"] = [hooks.make_hook(restore_cmd, phase="restore")]
    hooks_present = any(hooks_dict.values())
    hooks_allowed = False
    hooks_fingerprint = None
    if hooks_present and allow_hooks:
        if interactive and not util.DRY_RUN:
            util.warn("The following commands will run as ROOT on the timer from now on:")
            for ph, c in hooks.describe_commands({"hooks": hooks_dict}):
                util.warn("    [%s] %s" % (ph, c))
        hooks_allowed = True
        hooks_fingerprint = hooks.compute_fingerprint(hooks_dict)
        util.info("Hooks allowed.")
    elif hooks_present:
        util.warn("Hook commands stored but NOT allowed — the backup will fail until "
                  "'docker-backup set %s --allow-hooks' (after review) is run." % name)

    # --- Backup target ---
    if not target_base and interactive:
        target_base = wizard.prompt("Backup target (path or restic URL, base)")
    if not target_base:
        util.error("No backup target given.")
        return 1
    repo = compose.repo_for(target_base, name)
    mount_check = target_base if compose.is_local_repo(target_base) else None

    # --- Offsite (3-2-1) ---
    if offsite is None and interactive:
        off = wizard.prompt("Offsite target for 3-2-1 (empty = none)", default="")
        offsite = off or None
    offsite_repo = compose.repo_for(offsite, name) if offsite else None

    # --- Frequency ---
    if interactive:
        schedule_input = wizard.prompt(
            "Frequency (e.g. 'daily 03:00', 'weekly Mon 04:00', 'custom <expr>')",
            default=schedule_input or config.DEFAULT_SCHEDULE_INPUT,
        )
    schedule_input = schedule_input or config.DEFAULT_SCHEDULE_INPUT
    oncalendar = systemd_units.oncalendar_from(schedule_input)
    if not util.DRY_RUN and not systemd_units.validate_oncalendar(oncalendar):
        util.warn("OnCalendar '%s' could not be validated (systemd-analyze)." % oncalendar)

    cfg = _assemble_config(
        name=name,
        stack_path=stack_path,
        compose_file=compose_file,
        project_name=project_name,
        env_files=compose.find_env_files(stack_path),
        db_services=db_services,
        named_volumes=named_volumes,
        repo=repo,
        offsite=offsite_repo,
        exclude_paths=exclude_paths,
        extra_backup_paths=extra_backup_paths,
        quiesce_services=quiesce_services,
        exclude_patterns=user_excludes,
        db_autodetect=db_autodetect,
        hooks=hooks_dict,
        hooks_allowed=hooks_allowed,
        hooks_fingerprint=hooks_fingerprint,
        template=template,
        retention=retention,
        schedule_input=schedule_input,
        oncalendar=oncalendar,
        mount_check=mount_check,
    )

    cfg["key_file"] = keys.ensure_key(name)
    path = config.save(cfg)
    util.info("Config written: %s" % path)

    _install_timer(name, oncalendar)

    if not util.DRY_RUN:
        _print_escrow(name)
    util.info("Stack '%s' configured. Repo: %s%s" %
              (name, repo, (" (+ offsite: %s)" % offsite_repo) if offsite_repo else ""))
    return 0


def _build_quiesce_services(cj: Dict[str, Any], no_quiesce: bool) -> List[Dict[str, Any]]:
    """Config entries for the built-in quiesce (mongo: fsyncLock, redis: BGSAVE).

    Only the ENV KEYS of credentials are persisted (resolved freshly at run
    time); the lock scope is derived from where the data dir lives."""
    if no_quiesce:
        return []
    out = []  # type: List[Dict[str, Any]]
    for q in detect.find_quiesce_services(cj):
        creds = detect.extract_quiesce_credentials(q["environment"], q["engine"])
        entry = {
            "service": q["service"],
            "engine": q["engine"],
            "scope": quiesce.data_scope(q["volumes"], q["engine"]),
        }
        entry.update(creds)
        out.append(entry)
        method = ("write freeze via db.fsyncLock during the file capture"
                  if q["engine"] == "mongo" else "RDB checkpoint via BGSAVE beforehand")
        util.info("Quiesce enabled: '%s' (%s) — %s. Opt out with --no-quiesce."
                  % (q["service"], q["engine"], method))
    return out


def _resolve_external_binds(
    cj: Dict[str, Any], stack_path: str, exclude_paths: List[str], interactive: bool,
) -> List[str]:
    """External bind mounts → extra backup paths (with prompt/warning).

    Without this, a bind like ``/srv/appdata:/data`` would silently be missing
    from the backup: the file backup only covers the stack folder.
    """
    extra = compose.find_external_binds(cj, stack_path, exclude_paths)
    if not extra:
        return []
    util.warn("Bind mounts OUTSIDE the stack folder detected (not covered by the "
              "stack folder backup):")
    for p in extra:
        util.warn("    %s" % p)
    if interactive:
        if not wizard.confirm("Include these paths in the backup?", default=True):
            util.warn("External paths will NOT be backed up — make sure they are "
                      "covered by another backup.")
            return []
    else:
        util.info("They are included in the backup (config field 'extra_backup_paths').")
    return extra


def _print_escrow(name: str) -> None:
    """Escrow notice after create. On a TTY the key itself is printed; without a
    TTY (``--auto``/scripts) only a pointer, so the key does not leak into logs."""
    try:
        if sys.stdout.isatty():
            print(keys.escrow_notice(name, keys.read_key(name)))
        else:
            util.warn(
                "Key escrow: back up the restic key for '%s' OFFLINE now — "
                "'docker-backup key show %s' prints it. Without the key the "
                "backups are NOT restorable." % (name, name)
            )
    except OSError:
        pass


def _build_db_services(
    cj: Dict[str, Any], name: str, interactive: bool,
    dump_user: Optional[str] = None, dump_globals: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    for dbs in detect.find_db_services(cj):
        creds = detect.extract_credentials(dbs["environment"], dbs["engine"], dbs.get("flavor"))
        if creds is None:
            continue
        auth_user = dump_user or creds["user"]
        do_globals = creds.get("dump_globals", False) if dump_globals is None else dump_globals
        password_source = _resolve_password_source(name, dbs, creds, interactive)
        out.append({
            "service": dbs["service"],
            "engine": dbs["engine"],
            "auth_user": auth_user,
            "all_databases": creds["all_databases"],
            "databases": creds["databases"],
            "dump_globals": bool(do_globals),
            "password_source": password_source,
            "data_dir_target": compose.primary_db_data_dir(dbs["engine"]),
            "raw_data_exclude": None,
        })
        extra = ""
        if dbs.get("flavor"):
            extra += ", flavor: %s" % dbs["flavor"]
        if len(creds["databases"]) > 1 or do_globals:
            extra += ", DBs: %s%s" % (
                "+".join(creds["databases"]),
                " +globals" if do_globals else "",
            )
        util.info("DB detected: %s (%s), user '%s', password source: %s%s" %
                  (dbs["service"], dbs["engine"], auth_user, password_source, extra))
    if not out:
        util.warn("No database detected — only the filesystem is backed up.")
    return out


def _resolve_password_source(name, dbs, creds, interactive) -> str:
    if creds.get("password_env_key"):
        return "env:%s" % creds["password_env_key"]
    if interactive:
        util.warn("DB password for service '%s' could not be detected automatically." % dbs["service"])
        pw = wizard.prompt_secret(
            "DB password for %s (user %s, empty = skip)" % (dbs["service"], creds["user"])
        )
        if pw:
            config.save_secret(name, dbs["service"], pw)
            return "stored"
    else:
        util.warn("DB password for '%s' unknown; backup will attempt login without a password." % dbs["service"])
    return "none"


def _assemble_config(**k) -> Dict[str, Any]:
    return {
        "schema_version": config.SCHEMA_VERSION,
        "name": k["name"],
        "stack_path": k["stack_path"],
        "compose_file": k["compose_file"],
        "project_name": k["project_name"],
        "env_files": k["env_files"],
        "db_services": k["db_services"],
        "named_volumes": k["named_volumes"],
        "repo": k["repo"],
        "offsite": k["offsite"],
        "backend_env_file": os.path.join(config.backends_dir(), k["name"] + ".env"),
        "key_file": None,
        "exclude_paths": k["exclude_paths"],
        "extra_backup_paths": k.get("extra_backup_paths") or [],
        "quiesce_services": k.get("quiesce_services") or [],
        "quiesce_disabled": False,
        "exclude_patterns": k.get("exclude_patterns") or [],
        "db_autodetect": k.get("db_autodetect", True),
        "hooks": k.get("hooks") or {"pre_backup": [], "post_backup": [], "restore": []},
        "hooks_allowed": k.get("hooks_allowed", False),
        "hooks_fingerprint": k.get("hooks_fingerprint"),
        "template": k.get("template"),
        "schedule": {
            "input": k["schedule_input"],
            "oncalendar": k["oncalendar"],
            "randomized_delay_sec": 300,
        },
        "retention": k.get("retention") or dict(config.DEFAULT_RETENTION),
        "offsite_retention": None,
        "offsite_prune": True,
        "staging_dir": os.path.join(k["stack_path"], ".docker-backup"),
        "mount_check": k["mount_check"],
        "created": _utcnow(),
    }


def _install_timer(name: str, oncalendar: str) -> None:
    if util.DRY_RUN:
        util.info("DRY-RUN: would write drop-in (OnCalendar=%s) and enable the timer." % oncalendar)
        return
    systemd_units.write_schedule_dropin(name, oncalendar)
    systemd_units.daemon_reload()
    systemd_units.enable_timer(name)
    util.info("systemd timer active: docker-backup@%s.timer (%s)" % (name, oncalendar))


# --- Multiple stacks (--all) ------------------------------------------------
def _resolve_stack(st: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
    """(name, stack_dir, compose_file) from a 'docker compose ls' entry.

    compose_file is None when the compose file was not found."""
    name_hint = st.get("Name") or "?"
    config_files = (st.get("ConfigFiles") or "").split(",")
    compose_file = config_files[0].strip() if config_files else ""
    if not compose_file or not os.path.exists(compose_file):
        return (name_hint, "", None)
    stack_dir = os.path.dirname(compose_file)
    name = st.get("Name") or os.path.basename(stack_dir)
    return (name, stack_dir, compose_file)


def _list_running_stacks() -> List[Dict[str, Any]]:
    util.info("Reading running stacks (docker compose ls)…")
    return compose.ls_json(all_stacks=False)


def _summary(ok: List[str], skipped: List[str], failed: List[str]) -> int:
    util.info("Done. Configured: %s | Skipped: %s | Failed: %s" %
              (", ".join(ok) or "—", ", ".join(skipped) or "—", ", ".join(failed) or "—"))
    return 0 if not failed else 1


def _create_all_wizard(args, interactive: bool) -> int:
    """``create --all``: ask y/n per stack, run the full wizard on yes."""
    stacks = _list_running_stacks()
    if not stacks:
        util.warn("No running stacks found.")
        return 0
    ok = []  # type: List[str]
    skipped = []  # type: List[str]
    failed = []  # type: List[str]
    for st in stacks:
        name, stack_dir, compose_file = _resolve_stack(st)
        if not compose_file:
            util.warn("Stack '%s': compose file not found; skipped." % name)
            failed.append(name)
            continue
        if interactive and not wizard.confirm(
            "Back up stack '%s' (%s)?" % (name, stack_dir), default=True
        ):
            skipped.append(name)
            continue
        try:
            rc = create_one(stack_dir, target_base=None, schedule_input=None, offsite=None,
                            name=name, force=args.force, interactive=True,
                            dump_user=getattr(args, "dump_user", None),
                            dump_globals=getattr(args, "dump_globals", None),
                            db_autodetect=(getattr(args, "db_autodetect", None) is not False),
                            no_quiesce=getattr(args, "no_quiesce", False),
                            exclude_patterns=getattr(args, "exclude", None),
                            keep_within=getattr(args, "keep_within", None))
            (ok if rc == 0 else failed).append(name)
        except Exception as exc:
            util.error("Stack '%s' failed: %s" % (name, exc))
            failed.append(name)
    return _summary(ok, skipped, failed)


def _create_all_auto(args, interactive: bool) -> int:
    """``create --all --auto``: one shared target, all stacks, no prompts."""
    target = args.target
    if not target:
        if interactive and sys.stdin.isatty():
            target = wizard.prompt("Backup target (base) for ALL stacks")
        if not target:
            util.error("No target given. In auto mode, specify '--target <base>'.")
            return 2
    schedule = args.schedule or config.DEFAULT_SCHEDULE_INPUT

    stacks = _list_running_stacks()
    if not stacks:
        util.warn("No running stacks found.")
        return 0
    ok = []  # type: List[str]
    failed = []  # type: List[str]
    for st in stacks:
        name, stack_dir, compose_file = _resolve_stack(st)
        if not compose_file:
            util.warn("Stack '%s': compose file not found; skipped." % name)
            failed.append(name)
            continue
        util.info("--- Configuring stack '%s' (%s) ---" % (name, stack_dir))
        try:
            rc = create_one(stack_dir, target_base=target, schedule_input=schedule,
                            offsite=args.offsite, name=name, force=args.force,
                            interactive=False,
                            dump_user=getattr(args, "dump_user", None),
                            dump_globals=getattr(args, "dump_globals", None),
                            db_autodetect=(getattr(args, "db_autodetect", None) is not False),
                            no_quiesce=getattr(args, "no_quiesce", False),
                            exclude_patterns=getattr(args, "exclude", None),
                            keep_within=getattr(args, "keep_within", None))
            (ok if rc == 0 else failed).append(name)
        except Exception as exc:
            util.error("Stack '%s' failed: %s" % (name, exc))
            failed.append(name)
    return _summary(ok, [], failed)
