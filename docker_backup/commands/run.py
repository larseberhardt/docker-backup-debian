"""``run <name>`` — the actual backup run (called by systemd)."""

from __future__ import annotations

import datetime
import os
import shutil
import time
from typing import Any, Dict, List

from .. import compose, config, dbdump, hooks, manifest, notify, quiesce, restic, runtime, status, util, volumes


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

    # Fail-fast: stored but not (validly) approved hooks abort the run IMMEDIATELY —
    # no silent skipping (e.g. of GitLab's pre-dump).
    hooks.ensure_allowed(cfg)

    runtime.load_backend_env(cfg)
    util.assert_mounted(cfg.get("mount_check"))

    staging = cfg.get("staging_dir") or os.path.join(stack, ".docker-backup")
    dumps_dir = os.path.join(staging, "dumps")
    vols_dir = os.path.join(staging, "volumes")
    _prepare_staging(staging, [dumps_dir, vols_dir])

    # PRE hook (e.g. 'gitlab-backup create CRON=1') BEFORE any restic calls.
    # Default on_failure=abort → aborts before a half-prepared state is archived.
    hooks.run_hooks(cfg, "pre_backup")
    try:
        db_services = cfg.get("db_services") or []
        cj = None
        if db_services:
            cj = compose.config_json(compose_file, stack, project)
        for db in db_services:
            _dump_one(cfg, db, cj, dumps_dir)

        # Quiesce (mongo fsyncLock / redis BGSAVE) only around the FILE capture:
        # after the SQL dumps, released again as early as the data allows.
        quiesced = quiesce.begin(cfg, cj)
        try:
            for nv in cfg.get("named_volumes") or []:
                volumes.backup_named_volume(nv["real_name"], vols_dir, nv["key"])
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
            restic.backup(repo, key, [stack] + _extra_paths(cfg), excludes, tags)
            # Bind-mounted quiesce data was read live by restic → release now,
            # before prune/manifest/offsite (none of those read the stack files).
            quiesce.release(cfg, quiesced, scope="live")
        finally:
            quiesce.release(cfg, quiesced)  # error paths; no-op when already empty
        restic.forget_prune(repo, key, cfg.get("retention") or config.DEFAULT_RETENTION, ["docker-backup"])

        # Self-describing manifest next to the repo (for restore from another server
        # off the mounted drive). Best-effort, local repos only.
        manifest.write(cfg)

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
            _cleanup_staging([dumps_dir, vols_dir])

    util.info("Backup of '%s' complete." % name)

    summary = "Repo: %s" % repo
    snap = restic.last_snapshot(repo, key)
    if snap:
        short = snap.get("short_id") or (snap.get("id") or "")[:8]
        summary += "\nSnapshot: %s  Time: %s" % (short, snap.get("time", ""))
    if offsite:
        summary += "\nOffsite: %s" % offsite
    return summary


def _dump_one(cfg, db, cj, dumps_dir) -> None:
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
        dbdump.dump(db, password, compose_file, stack, project, dumps_dir)
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


def _prepare_staging(staging: str, dirs: List[str]) -> None:
    if util.DRY_RUN:
        return
    for d in dirs:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    # Staging holds plaintext DB dumps/volume tars → root-only, not world-readable.
    for d in [staging] + dirs:
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def _cleanup_staging(dirs: List[str]) -> None:
    # After the restic backup, dumps/tars are already in the repo → clean up locally.
    if util.DRY_RUN:
        return
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
