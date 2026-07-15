"""``run <name>`` — the actual backup run (called by systemd)."""

from __future__ import annotations

import datetime
import copy
import errno
import fnmatch
import os
import stat
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from .. import compose, config, dbdump, detect, hooks, manifest, notify, quiesce, restic, runtime, status, util, volumes


@dataclass
class _StagingRefs:
    root_path: str
    root_fd: int
    child_fds: Dict[str, int]
    child_names: Dict[str, str]


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def cmd_run(args) -> int:
    util.require_root()
    if getattr(args, "all", False):
        if getattr(args, "name", None):
            util.error("Either <name> OR --all, not both.")
            return 2
        return _run_all()
    if not getattr(args, "name", None):
        util.error("Stack name missing (or use --all).")
        return 2
    return _run_one(config.sanitize_name(args.name))


def _run_one(name: str) -> int:
    """Back up one stack. Writes success/failure status and re-raises on error
    (so the systemd OnFailure hook fires)."""
    if not config.exists(name):
        util.error("No config named '%s'." % name)
        return 1
    cfg = config.load(name)
    started_at = _utcnow()
    t0 = time.monotonic()
    try:
        with util.FileLock("/run/docker-backup/%s.lock" % name):
            summary = _do_run(cfg)
    except Exception as exc:
        status.write_status(
            name, result="failure", started_at=started_at, finished_at=_utcnow(),
            duration_sec=round(time.monotonic() - t0, 1),
            error=util.scrub(str(exc))[:500],
        )
        raise
    status.write_status(
        name, result="success", started_at=started_at, finished_at=_utcnow(),
        duration_sec=round(time.monotonic() - t0, 1), snapshot=_short_id(cfg),
    )
    # Success message (only if enabled in notify.json). Failures are reported via
    # the systemd OnFailure hook (docker-backup-notify@%i.service).
    notify.notify_success(name, summary)
    return 0


def _run_all() -> int:
    """Back up all configured stacks in sequence (continue-on-error)."""
    names = config.list_names()
    if not names:
        util.warn("No stacks configured.")
        return 0
    rows = []  # type: List[Any]
    worst = 0
    for n in names:
        util.info("--- Backing up stack '%s' ---" % n)
        try:
            rc = _run_one(n)
        except Exception as exc:  # keep going robustly per stack
            util.error("Stack '%s' failed: %s" % (n, exc))
            rc = 1
        rows.append((n, "ok" if rc == 0 else "FAILED"))
        worst = max(worst, rc)
    util.print_table(("NAME", "RESULT"), rows)
    return 0 if worst == 0 else 1


def _short_id(cfg: Dict[str, Any]) -> Any:
    """Short id of the last written snapshot; defensive (must not break the run)."""
    # A real _do_run records the exact id emitted by its backup invocation. Do
    # not turn an absent/pruned id into a potentially unrelated `latest` result.
    if "_last_run_snapshot_id" in cfg:
        snapshot_id = cfg.get("_last_run_snapshot_id")
        return snapshot_id[:8] if isinstance(snapshot_id, str) else None
    try:
        snap = restic.last_snapshot(cfg["repo"], cfg["key_file"])
        if snap:
            return snap.get("short_id") or (snap.get("id") or "")[:8] or None
    except Exception:
        pass
    return None


def _do_run(cfg: Dict[str, Any]) -> str:
    name = cfg["name"]
    stack = cfg["stack_path"]
    compose_file = cfg["compose_file"]
    project = cfg.get("project_name")
    key = cfg["key_file"]
    repo = cfg["repo"]
    offsite = cfg.get("offsite")
    created_snapshot = None  # type: Any
    cfg["_last_run_snapshot_id"] = None

    # Fail-fast: stored but not (validly) approved hooks abort the run IMMEDIATELY —
    # no silent skipping (e.g. of GitLab's pre-dump).
    hooks.ensure_allowed(cfg)

    runtime.load_backend_env(cfg)
    util.assert_mounted(cfg.get("mount_check"))

    # Bind the cached database exclusions to the Compose model that is live for
    # this run *before* creating plaintext staging data or executing a hook.  A
    # database bind can move after ``create``/``set``; continuing with the old
    # restic exclude would then archive the live raw data directory alongside
    # the logical dump.
    db_services = cfg.get("db_services") or []
    cj = compose.config_json(compose_file, stack, project)
    _verify_mysql_db_scope(cfg, cj)
    _verify_backup_db_excludes(
        cfg.get("exclude_paths") or [], db_services, cj,
    )

    staging = cfg.get("staging_dir") or os.path.join(stack, ".docker-backup")
    dumps_dir = os.path.join(staging, "dumps")
    vols_dir = os.path.join(staging, "volumes")
    _validate_staging_backup_shape(cfg, stack, staging)
    if not util.DRY_RUN:
        staging_users = compose.running_writable_bind_mounts_overlapping(staging)
        if staging_users:
            raise util.CommandError(
                ["backup", "staging"], 1,
                "Running containers have writable bind mounts overlapping the "
                "plaintext staging area: %s"
                % ", ".join(
                    "%s (%s)" % (item["container"], item["source"])
                    for item in staging_users
                ),
            )
    staging_refs = _prepare_staging(staging, [dumps_dir, vols_dir])
    dumps_fd = (staging_refs.child_fds[dumps_dir]
                if staging_refs is not None else None)
    volumes_fd = (staging_refs.child_fds[vols_dir]
                  if staging_refs is not None else None)

    # PRE hook (e.g. 'gitlab-backup create CRON=1') BEFORE any restic calls.
    # Default on_failure=abort → aborts before a half-prepared state is archived.
    try:
        hooks.run_hooks(cfg, "pre_backup")
        configured_named_volumes = cfg.get("named_volumes") or []
        extra_paths = _extra_paths(cfg)
        # Always bind cached backup metadata to the current Compose model. A
        # newly added named volume must not be silently omitted just because the
        # previous config happened to contain no DB/volume/extra entries.
        named_volume_plan = _verified_backup_named_volumes(
            configured_named_volumes, db_services, cj,
        )
        external_bind_descriptors = compose.describe_selected_external_binds(
            cj, extra_paths
        ) if extra_paths else []
        for db in db_services:
            _dump_one(cfg, db, cj, dumps_dir, dumps_fd=dumps_fd)

        # Quiesce (mongo fsyncLock / redis BGSAVE) only around the FILE capture:
        # after the SQL dumps, released again as early as the data allows.
        quiesced = quiesce.begin(cfg, cj)
        try:
            for nv in named_volume_plan:
                volumes.backup_named_volume(
                    nv["real_name"], vols_dir, nv["key"],
                    staging_fd=volumes_fd,
                )
            # Named-volume data is now in staging → those locks can go BEFORE
            # the (possibly long) restic upload.
            quiesce.release(cfg, quiesced, scope="staging")

            restic.ensure_init(repo, key)
            # Clear stale locks (crash/reboot/timeout mid-run) — otherwise every
            # following run fails until a manual 'restic unlock'. Live locks stay.
            restic.unlock(repo, key)
            tags = ["docker-backup", "stack:%s" % name]
            excludes = restic.resolve_excludes(
                stack, cfg.get("exclude_paths") or [], cfg.get("exclude_patterns") or []
            )
            created_snapshot_id = restic.backup(
                repo, key, [stack] + extra_paths, excludes, tags
            )
            # Bind-mounted quiesce data was read live by restic → release now,
            # before prune/manifest/offsite (none of those read the stack files).
            quiesce.release(cfg, quiesced, scope="live")
        finally:
            quiesce.release(cfg, quiesced)  # error paths; no-op when already empty
        restic.forget_prune(repo, key, cfg.get("retention") or config.DEFAULT_RETENTION, ["docker-backup"])

        # Self-describing manifest next to the repo (for restore from another
        # server off the mounted drive). Bind it to the id emitted by this exact
        # backup process. After retention, verify that exact id still exists;
        # never substitute `latest`, which could be a concurrent/manual snapshot.
        try:
            snapshot_id = manifest.validate_snapshot_id(created_snapshot_id)
        except ValueError as exc:
            # The data snapshot itself completed successfully. Keep that success,
            # but never write an unbound v5 sidecar; an older bound manifest is
            # safer than metadata that may describe a different snapshot.
            util.warn("Manifest skipped: restic returned no valid full snapshot id (%s)." % exc)
        else:
            created_snapshot = restic.snapshot_by_id(repo, key, snapshot_id)
            if not created_snapshot:
                util.warn(
                    "Manifest skipped: snapshot %s created by this backup no "
                    "longer exists after retention." % snapshot_id[:8]
                )
            else:
                cfg["_last_run_snapshot_id"] = snapshot_id
                try:
                    manifest.write(cfg, snapshot_id, external_bind_descriptors)
                except ValueError as exc:
                    # Descriptor generation happened before backup and should make
                    # this unreachable. Preserve manifest's best-effort contract if
                    # a defensive write-boundary check still catches a problem.
                    util.warn("Manifest skipped after backup: %s" % exc)

        if offsite:
            restic.ensure_init_offsite(offsite, key, repo)
            restic.unlock(offsite, key)
            restic.copy(offsite, key, repo)
            # Without this, the offsite repo grows unbounded: 'copy' only ever adds
            # snapshots. Default: same retention as primary; offsite_retention
            # overrides, offsite_prune=False keeps everything (deliberate archive).
            if cfg.get("offsite_prune", True):
                restic.forget_prune(
                    offsite, key,
                    cfg.get("offsite_retention") or cfg.get("retention") or config.DEFAULT_RETENTION,
                    ["docker-backup"],
                )
    finally:
        # POST hook (e.g. 'rm -f .../backups/*.tar') ALWAYS runs — even if the backup
        # above fails (cleanup must not be skipped). Default on_failure=warn → a
        # cleanup hiccup never masks the real error.
        try:
            hooks.run_hooks(cfg, "post_backup")
        finally:
            # Staging holds plaintext DB dumps → remove it even when the run failed
            # (previously only on success, leaving dumps behind until the next run).
            _cleanup_staging(staging_refs)

    util.info("Backup of '%s' complete." % name)

    summary = "Repo: %s" % repo
    snap = created_snapshot
    if snap:
        short = snap.get("short_id") or (snap.get("id") or "")[:8]
        summary += "\nSnapshot: %s  Time: %s" % (short, snap.get("time", ""))
    if offsite:
        summary += "\nOffsite: %s" % offsite
    return summary


def _dump_one(cfg, db, cj, dumps_dir, *, dumps_fd=None) -> None:
    compose_file = cfg["compose_file"]
    stack = cfg["stack_path"]
    project = cfg.get("project_name")
    password = runtime.resolve_password(cfg, db, cj)

    started = False
    if not compose.service_running(compose_file, stack, db["service"], project):
        util.info("DB service '%s' is stopped — starting temporarily for the dump." % db["service"])
        compose.up_service(compose_file, stack, db["service"], project)
        started = True
    try:
        if not dbdump.wait_ready(db, password, compose_file, stack, project, timeout=120):
            util.warn("DB '%s' did not become ready in time; dump may fail." % db["service"])
        if (util.DRY_RUN and started and db.get("engine") == "mysql"
                and db.get("database_scope") == "non-system"):
            util.info(
                "DRY-RUN: would enumerate all non-system databases and dump "
                "them after temporarily starting service '%s'." % db["service"]
            )
            return
        dbdump.dump(
            db, password, compose_file, stack, project, dumps_dir,
            dumps_fd=dumps_fd,
        )
    finally:
        if started:
            util.info("Stopping temporarily started DB service '%s'." % db["service"])
            compose.stop_service(compose_file, stack, db["service"], project)


def _extra_paths(cfg: Dict[str, Any]) -> List[str]:
    """External bind-mount paths that go into the snapshot alongside the stack.

    A missing path aborts (fail-loud): it usually means an unmounted share —
    continuing would let the data silently age out of the snapshots."""
    extra = [p for p in (cfg.get("extra_backup_paths") or []) if p]
    if util.DRY_RUN:
        return extra
    missing = [p for p in extra if not os.path.exists(p)]
    if missing:
        raise util.CommandError(
            missing, 1,
            "Extra backup path(s) missing: %s — not mounted? Remove them from the "
            "config ('extra_backup_paths') if they are no longer needed."
            % ", ".join(missing),
        )
    return extra


def _verify_mysql_db_scope(cfg: Dict[str, Any], cj: Dict[str, Any]) -> None:
    """Reject unstamped legacy MySQL auto-detection when the current detector
    can use the portable non-system scope.

    Updating the program cannot safely rewrite an existing config: an operator
    may deliberately have created extra databases that are not declared in
    Compose.  Therefore legacy configs fail before hooks/staging and require the
    explicit, reviewable ``set --refresh-db-detection`` migration.  Configs made
    or refreshed with the current detector carry ``db_scope_version=2``; an
    intentional cluster-wide dump there remains valid.
    """
    if not cfg.get("db_autodetect", True):
        return
    version = cfg.get("db_scope_version")
    if isinstance(version, int) and not isinstance(version, bool) \
            and version >= config.DB_SCOPE_VERSION:
        return

    detected = {
        item.get("service"): item
        for item in detect.find_db_services(cj)
        if isinstance(item, dict) and item.get("service")
    }
    for stored in cfg.get("db_services") or []:
        if not isinstance(stored, dict):
            continue
        if stored.get("engine") != "mysql":
            continue
        # A backported non-system marker is already safe without the top-level
        # version stamp. Every other legacy plan must be reviewed: --all-
        # databases includes system schemas, while a static list can silently
        # omit additional user databases created after initial setup.
        if stored.get("database_scope") == "non-system":
            continue
        current = detected.get(stored.get("service"))
        if not current:
            continue
        creds = detect.extract_credentials(
            current.get("environment"), "mysql", current.get("flavor"),
        )
        if not creds or creds.get("database_scope") != "non-system":
            continue
        name = cfg.get("name") or "<name>"
        seed = ",".join(creds.get("databases") or [])
        detail = (
            " (configured application seed: %s)" % seed if seed else ""
        )
        raise util.CommandError(
            ["backup", "db-scope"], 1,
            "Legacy MySQL scope for service '%s' cannot guarantee a complete, "
            "portable set of user databases%s. "
            "Review and persist the safe scope first: docker-backup set %s "
            "--refresh-db-detection"
            % (stored.get("service"), detail, name),
        )


def _verified_backup_named_volumes(
    configured: list, db_services: list, cj: Any,
) -> list:
    """Bind cached volume metadata to the current normalized Compose model."""
    if cj is None:
        if configured:
            raise util.CommandError(
                ["backup", "named-volumes"], 1,
                "Named volumes require the current Compose model.",
            )
        return []
    db_copy = copy.deepcopy(db_services)
    _exclude_paths, current = compose.collect_volume_backup_plan(cj, db_copy)

    def identity(item):
        if not isinstance(item, dict):
            raise ValueError("named volume entry must be an object")
        return (
            item.get("key"), item.get("service"), item.get("target"),
            item.get("real_name"),
        )

    try:
        configured_ids = {identity(item) for item in configured}
        current_ids = {identity(item) for item in current}
    except (TypeError, ValueError) as exc:
        raise util.CommandError(["backup", "named-volumes"], 1, str(exc))
    if configured_ids != current_ids or len(configured_ids) != len(configured):
        raise util.CommandError(
            ["backup", "named-volumes"], 1,
            "Configured named volumes no longer exactly match Compose. Re-run "
            "'docker-backup set/create' after reviewing the stack; refusing an "
            "incomplete or misdirected archive.",
        )
    # Check every source before creating the first archive. docker run -v would
    # otherwise auto-create a typo/deleted volume and successfully tar emptiness.
    for item in current:
        if compose.volume_identity(item["real_name"]) is None:
            raise util.CommandError(
                ["docker", "volume", "inspect", item["real_name"]], 1,
                "Configured named volume is missing; refusing an empty backup.",
            )
    return current


def _verify_backup_db_excludes(
    configured_exclude_paths: list, db_services: list, cj: Any,
) -> None:
    """Authenticate cached raw-DB bind exclusions against current Compose.

    ``exclude_paths`` is generated from the database services' raw bind mounts
    at config-creation time.  Both that aggregate list and every service's
    ``raw_data_exclude`` annotation must still be an exact match.  Otherwise an
    old path could remain excluded while restic reads a newly moved live data
    directory.
    """
    if cj is None:
        raise util.CommandError(
            ["backup", "db-excludes"], 1,
            "Database exclusions require the current Compose model.",
        )

    current_dbs = copy.deepcopy(db_services)
    # collect_volume_backup_plan annotates in place.  Remove cached annotations
    # first so a removed bind cannot survive through setdefault().
    for db in current_dbs:
        db.pop("raw_data_exclude", None)
        db.pop("data_dir_target", None)
    current_exclude_paths, _named = compose.collect_volume_backup_plan(cj, current_dbs)

    def valid_paths(value: Any) -> bool:
        return isinstance(value, list) and all(
            isinstance(path, str) and path for path in value
        )

    cached_paths = configured_exclude_paths
    if not valid_paths(cached_paths) or not valid_paths(current_exclude_paths):
        raise util.CommandError(
            ["backup", "db-excludes"], 1,
            "Database raw-data exclude metadata is invalid; re-run "
            "'docker-backup set/create' after reviewing the stack.",
        )

    cached_by_service = {
        db.get("service"): db.get("raw_data_exclude")
        for db in db_services if isinstance(db, dict)
    }
    current_by_service = {
        db.get("service"): db.get("raw_data_exclude")
        for db in current_dbs if isinstance(db, dict)
    }
    paths_match = (
        len(cached_paths) == len(current_exclude_paths)
        and set(cached_paths) == set(current_exclude_paths)
    )
    services_match = cached_by_service == current_by_service
    if not paths_match or not services_match:
        raise util.CommandError(
            ["backup", "db-excludes"], 1,
            "Configured database raw-data excludes no longer exactly match "
            "Compose. Re-run 'docker-backup set/create' after reviewing the "
            "stack; refusing to archive a live raw database data directory.",
        )


def _validate_staging_backup_shape(
    cfg: Dict[str, Any], stack: str, staging: str,
) -> None:
    """Keep logical artifacts in-snapshot and outside every configured exclude."""
    expected = os.path.join(stack, ".docker-backup")
    if staging != expected:
        raise util.CommandError(
            ["backup", "staging"], 1,
            "staging_dir must be exactly %s so logical dumps/volume archives "
            "are part of the stack snapshot; got %s." % (expected, staging),
        )
    required = []
    dumps = os.path.join(staging, "dumps")
    for db in cfg.get("db_services") or []:
        service = db.get("service")
        if db.get("engine") == "mysql":
            required.append(os.path.join(dumps, "%s.sql" % service))
        elif db.get("engine") == "postgres":
            if db.get("dump_globals"):
                required.append(os.path.join(dumps, dbdump.globals_filename(service)))
            databases = db.get("databases") or [dbdump._MAINT_DB]
            required.extend(
                os.path.join(dumps, dbdump.db_filename(service, database))
                for database in databases
            )
    required.extend(
        os.path.join(staging, "volumes", volumes.archive_name(item.get("key")))
        for item in (cfg.get("named_volumes") or [])
    )
    if not required:
        return
    excludes = restic.resolve_excludes(
        stack, cfg.get("exclude_paths") or [], cfg.get("exclude_patterns") or [],
    )

    def matches(exclude: str, artifact: str) -> bool:
        if os.path.isabs(exclude):
            plain = not any(char in exclude for char in "*?[")
            return (fnmatch.fnmatchcase(artifact, exclude)
                    or plain and (
                        artifact == exclude
                        or artifact.startswith(exclude.rstrip("/") + "/")
                    ))
        relative = os.path.relpath(artifact, stack)
        return any(
            fnmatch.fnmatchcase(component, exclude)
            for component in relative.split(os.path.sep)
        )

    conflicts = [
        (artifact, exclude)
        for artifact in required for exclude in excludes
        if matches(exclude, artifact)
    ]
    if conflicts:
        artifact, exclude = conflicts[0]
        raise util.CommandError(
            ["backup", "staging"], 1,
            "Exclude %r would omit required logical backup artifact %s. "
            "Remove/narrow that exclude before backing up." % (exclude, artifact),
        )


def _staging_dir_flags() -> int:
    missing = [name for name in ("O_DIRECTORY", "O_NOFOLLOW") if not hasattr(os, name)]
    if missing:
        raise RuntimeError("Safe staging requires %s support." % ", ".join(missing))
    return (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0))


def _open_staging_path(path: str, *, create: bool) -> int:
    absolute = os.path.abspath(path)
    if absolute != path or os.path.normpath(path) != path or path == os.path.sep:
        raise ValueError("Staging path must be canonical and absolute: %r" % path)
    flags = _staging_dir_flags()
    fd = os.open(os.path.sep, flags)
    try:
        for part in (part for part in path.split(os.path.sep) if part):
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _staging_lstat(parent_fd: int, name: str):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_staging_child(parent_fd: int, name: str) -> int:
    before = _staging_lstat(parent_fd, name)
    if before is None or not stat.S_ISDIR(before.st_mode):
        raise ValueError("Staging child is not a directory: %s" % name)
    fd = os.open(name, _staging_dir_flags(), dir_fd=parent_fd)
    opened = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        raise OSError(errno.ESTALE, "Staging directory changed", name)
    return fd


def _remove_staging_entry(parent_fd: int, name: str) -> None:
    info = _staging_lstat(parent_fd, name)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode):
        child_fd = _open_staging_child(parent_fd, name)
        try:
            for child in os.listdir(child_fd):
                _remove_staging_entry(child_fd, child)
            current = _staging_lstat(parent_fd, name)
            opened = os.fstat(child_fd)
            if (current is None or (current.st_dev, current.st_ino)
                    != (opened.st_dev, opened.st_ino)):
                raise OSError(errno.ESTALE, "Staging directory changed", name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        # unlinkat never follows a symlink and is safe for regular/special entries.
        os.unlink(name, dir_fd=parent_fd)


def _prepare_staging(staging: str, dirs: List[str]):
    if util.DRY_RUN:
        return None
    root_fd = -1
    child_fds = {}  # type: Dict[str, int]
    child_names = {}  # type: Dict[str, str]
    try:
        root_fd = _open_staging_path(staging, create=True)
        os.fchmod(root_fd, 0o700)
        for path in dirs:
            if (os.path.dirname(path) != staging or os.path.abspath(path) != path
                    or os.path.normpath(path) != path):
                raise ValueError(
                    "Staging child must be directly below %s: %s" % (staging, path)
                )
            name = os.path.basename(path)
            if not name or name in (".", "..") or name in child_names.values():
                raise ValueError("Invalid/duplicate staging child: %s" % path)
            _remove_staging_entry(root_fd, name)
            os.mkdir(name, 0o700, dir_fd=root_fd)
            child_fd = _open_staging_child(root_fd, name)
            os.fchmod(child_fd, 0o700)
            child_fds[path] = child_fd
            child_names[path] = name
        return _StagingRefs(staging, root_fd, child_fds, child_names)
    except Exception:
        for fd in child_fds.values():
            os.close(fd)
        if root_fd >= 0:
            os.close(root_fd)
        raise


def _cleanup_staging(refs: Any) -> None:
    """Remove only held, exact staging children; never follow path replacements."""
    if util.DRY_RUN or refs is None:
        return
    if not isinstance(refs, _StagingRefs):
        raise ValueError("Safe staging cleanup requires retained descriptors.")
    errors = []
    for path, fd in list(refs.child_fds.items()):
        try:
            name = refs.child_names[path]
            for child in os.listdir(fd):
                _remove_staging_entry(fd, child)
            current = _staging_lstat(refs.root_fd, name)
            opened = os.fstat(fd)
            if (current is None or (current.st_dev, current.st_ino)
                    != (opened.st_dev, opened.st_ino)):
                raise OSError(errno.ESTALE, "Staging child changed", path)
            os.close(fd)
            refs.child_fds.pop(path, None)
            os.rmdir(name, dir_fd=refs.root_fd)
        except Exception as exc:
            errors.append("%s: %s" % (path, exc))
    for fd in refs.child_fds.values():
        try:
            os.close(fd)
        except OSError:
            pass
    refs.child_fds.clear()
    if refs.root_fd >= 0:
        os.close(refs.root_fd)
        refs.root_fd = -1
    if errors:
        raise util.CommandError(
            ["staging-cleanup"], 1, "; ".join(errors),
        )
