"""Self-describing repo manifest for cross-server restore.

During a backup, a small, NON-secret JSON manifest is written next to the restic
repo on the (mounted) backup drive. A second server can use it to restore
directly from the repo without a local ``/etc`` config::

    docker-backup restore <target> --from-repo <repo-path> --key-file <key>

The manifest deliberately contains NO secrets: DB passwords appear only as a
source reference (``password_source``), never as a value. The restic key is left
out and must be provided separately (``--key-file``) — this keeps the restic
encryption of the drive effective. It also contains NO shell; portable template
metadata is only an identity/compatibility binding for commands loaded from the
target server's trusted local template installation.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import compose, config, hooks, templates, util

MANIFEST_SCHEMA_VERSION = 5  # v5: shell-free template binding for trusted local reconstruction
MANIFEST_BASENAME = "docker-backup.manifest.json"
_TEMPLATE_DESCRIPTOR_KEYS = {
    "name", "version", "source", "hooks_fingerprint", "hooks_present",
}
_EXTERNAL_BIND_DESCRIPTOR_KEYS = {"service", "target", "source"}


# --- paths ------------------------------------------------------------------
def manifest_path(repo: str) -> str:
    return os.path.join(repo, MANIFEST_BASENAME)


# --- derive / write ---------------------------------------------------------
def validate_snapshot_id(snapshot_id: Any) -> str:
    """Return a canonical full restic snapshot id or reject it.

    The sidecar describes one specific snapshot.  Short ids are deliberately
    not accepted: although restic can currently resolve them, they are not a
    stable, unambiguous binding for a reconstruction manifest.
    """
    if (not isinstance(snapshot_id, str) or len(snapshot_id) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_id)):
        raise ValueError("snapshot_id must be a full 64-character lowercase hex restic id")
    return snapshot_id


def derive(
    cfg: Dict[str, Any], snapshot_id: Any = None,
    external_bind_descriptors: Any = None,
) -> Dict[str, Any]:
    """Non-secret subset of the config for the manifest.

    Deliberately omitted: ``key_file``, ``backend_env_file``, ``repo`` (only as
    ``source_repo`` diagnostics), ``offsite``, ``staging_dir``, ``mount_check``
    and ``env_files`` — all source-server-local, secret, or irrelevant for a
    restore on another server. ``compose_file`` is reduced to its basename
    (restore.py recomputes it against the target). Schedule/retention metadata is
    diagnostic only; safe config reconstruction uses the exact local template.
    """
    # HARD INVARIANT: 'hooks' / restore shell are NEVER written into the manifest.
    # The manifest is a NON-encrypted plaintext file next to the repo; a
    # root-executable command must not end up there (tamperable). For a
    # cross-server restore a reviewed command is passed via --restore-cmd, or an
    # exact local template is selected with --use-template-hooks. The descriptor
    # contains identity + a compatibility hash, never executable text.
    # A regression test guards this.
    snapshot_id = validate_snapshot_id(snapshot_id)
    for db in cfg.get("db_services") or []:
        if isinstance(db, dict) and "database_scope" in db:
            raise ValueError(
                "database_scope must be resolved to an exact database list "
                "before writing the manifest"
            )
    external_bind_descriptors = _copy_external_bind_descriptors(
        cfg, external_bind_descriptors
    )
    template = _derive_template_descriptor(cfg)
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "config_schema_version": cfg.get("schema_version"),
        "name": cfg["name"],
        "stack_path": cfg["stack_path"],
        "compose_file": os.path.basename(cfg["compose_file"]),
        "project_name": cfg.get("project_name"),
        "db_services": cfg.get("db_services") or [],
        "named_volumes": cfg.get("named_volumes") or [],
        "exclude_paths": cfg.get("exclude_paths") or [],
        "extra_backup_paths": cfg.get("extra_backup_paths") or [],
        "external_bind_descriptors": external_bind_descriptors,
        "exclude_patterns": cfg.get("exclude_patterns") or [],
        "db_autodetect": cfg.get("db_autodetect", True),
        "hooks_present": hooks.has_commands(cfg),
        "custom_restore_required": bool(
            ((cfg.get("hooks") or {}).get("restore") or [])
        ),
        "template": template,
        "schedule": cfg.get("schedule"),
        "quiesce_services": cfg.get("quiesce_services") or [],
        "quiesce_disabled": bool(cfg.get("quiesce_disabled", False)),
        "retention": cfg.get("retention") or dict(config.DEFAULT_RETENTION),
        "created_by_host": _hostname(),
        "written_at": _utcnow(),
        "source_repo": cfg.get("repo"),
    }


def _derive_template_descriptor(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Allowlisted, shell-free template identity for the sidecar manifest."""
    raw = cfg.get("template")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    version = raw.get("version")
    source = raw.get("source")
    version_text = str(version) if isinstance(version, (str, int)) else ""
    if (not templates.valid_name(name) or not version_text.isdigit()
            or len(version_text) > 10 or source not in ("builtin", "operator")):
        return None
    return {
        "name": name,
        "version": version_text,
        "source": source,
        # Recomputed from the actual config hooks: a CLI/set override after
        # template creation intentionally makes a local template mismatch.
        "hooks_fingerprint": hooks.compute_definition_fingerprint(
            cfg.get("hooks") or {}
        ),
        "hooks_present": hooks.has_commands(cfg),
    }


def _copy_external_bind_descriptors(
    cfg: Dict[str, Any], raw: Any,
) -> List[Dict[str, str]]:
    """Copy only the portable descriptor allowlist into a new manifest.

    ``run`` obtains these records from the trusted, normalized Compose model.
    This boundary still validates their shape and requires every configured
    external snapshot path to be represented, so another caller cannot emit a
    misleading v5 sidecar accidentally.
    """
    selected = cfg.get("extra_backup_paths") or []
    if not isinstance(selected, list):
        raise ValueError("extra_backup_paths must be a list")
    selected_set = set()  # type: set
    for value in selected:
        if (not isinstance(value, str) or not value or "\0" in value
                or not os.path.isabs(value) or value.startswith("//") or value == "/"
                or os.path.normpath(value) != value):
            raise ValueError("extra_backup_paths contains an invalid path")
        selected_set.add(value)
    if raw is None:
        if selected:
            raise ValueError(
                "external_bind_descriptors are required for extra_backup_paths"
            )
        raw = []
    if not isinstance(raw, list):
        raise ValueError("external_bind_descriptors must be a list")

    copied = []  # type: List[Dict[str, str]]
    identities = set()  # type: set
    described_sources = set()  # type: set
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("external bind descriptor #%d must be an object" % index)
        unknown = set(item) - _EXTERNAL_BIND_DESCRIPTOR_KEYS
        missing = _EXTERNAL_BIND_DESCRIPTOR_KEYS - set(item)
        if unknown or missing:
            raise ValueError(
                "external bind descriptor #%d must contain only service, target, source"
                % index
            )
        service = item.get("service")
        target = item.get("target")
        source = item.get("source")
        if not isinstance(service, str) or not service or "\0" in service:
            raise ValueError("external bind descriptor service is invalid")
        for label, value in (("target", target), ("source", source)):
            if (not isinstance(value, str) or not value or "\0" in value
                    or not os.path.isabs(value) or value.startswith("//") or value == "/"
                    or os.path.normpath(value) != value):
                raise ValueError("external bind descriptor %s is invalid" % label)
        identity = (service, target, source)
        if identity in identities:
            raise ValueError("duplicate external bind descriptor")
        identities.add(identity)
        described_sources.add(source)
        copied.append({"service": service, "target": target, "source": source})

    if described_sources != selected_set:
        raise ValueError(
            "external bind descriptor sources do not match extra_backup_paths"
        )
    return copied


def write(
    cfg: Dict[str, Any], snapshot_id: Any = None,
    external_bind_descriptors: Any = None,
) -> Optional[str]:
    """Write the snapshot-bound manifest next to the repo.

    Only for local repos (path); skipped for restic URLs (s3:, sftp: …), since no
    side-car file can be placed there. Atomic via tempfile + os.replace
    (like ``config.save``). Filesystem errors stay best-effort, but an invalid
    snapshot binding is a caller error and deliberately fails closed.
    """
    # Validate before all early returns (remote repo / dry-run) so callers can
    # never accidentally treat a short or missing id as a v5 manifest binding.
    snapshot_id = validate_snapshot_id(snapshot_id)
    repo = cfg.get("repo") or ""
    if not compose.is_local_repo(repo):
        util.info("Repo '%s' is not a local path — manifest skipped." % repo)
        return None
    path = manifest_path(repo)
    if util.DRY_RUN:
        util.info("DRY-RUN: would write manifest to %s" % path)
        return None
    try:
        os.makedirs(repo, exist_ok=True)
        data = json.dumps(
            derive(cfg, snapshot_id, external_bind_descriptors),
            indent=2, sort_keys=True,
        ) + "\n"
        fd, tmp = tempfile.mkstemp(dir=repo, prefix=".manifest.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
            tmp = ""  # moved successfully
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        util.info("Manifest written: %s" % path)
        return path
    except OSError as exc:
        # Must not break the backup run (defensive like _short_id).
        util.warn("Manifest could not be written (%s): %s" % (path, exc))
        return None


# --- read -------------------------------------------------------------------
def read(repo: str) -> Optional[Dict[str, Any]]:
    return read_at(manifest_path(repo))


def read_at(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cfg_from_manifest(
    man: Dict[str, Any], repo: str, key_file: str, name: Optional[str] = None
) -> Dict[str, Any]:
    """Builds an in-memory config from a manifest, as restore.py expects it.

    ``repo`` (= --from-repo) and ``key_file`` are set so that the restore works
    regardless of where the drive is mounted on server B.
    ``mount_check``/``backend_env_file`` are source-server-specific → None.
    """
    # v1-v3 did not record whether a restore hook was required. Recognize the
    # shipped GitLab layout so existing GitLab repositories fail safe instead of
    # silently skipping `gitlab-backup restore` on another server.
    patterns = man.get("exclude_patterns") or []
    legacy_gitlab = (
        not man.get("db_autodetect", True)
        and "gitlab/data/gitaly" in patterns
        and "gitlab/data/postgresql" in patterns
    )
    schema_version = man.get("manifest_schema_version", 1)
    snapshot_id, snapshot_id_error = _parse_manifest_snapshot_id(
        man.get("snapshot_id"),
        required=(isinstance(schema_version, int)
                  and not isinstance(schema_version, bool)
                  and schema_version >= MANIFEST_SCHEMA_VERSION),
    )
    template, template_error = _parse_template_descriptor(man.get("template"))
    descriptor_hooks_present = bool(template and template.get("hooks_present"))
    if (template is not None and "hooks_present" in man
            and bool(man.get("hooks_present")) != descriptor_hooks_present):
        template = None
        template_error = "top-level and template hooks_present markers disagree"
    effective_hooks_present = bool(
        man.get("hooks_present", False) or descriptor_hooks_present or legacy_gitlab
    )
    return {
        "schema_version": man.get("config_schema_version"),
        "name": config.sanitize_name(name or man["name"]),
        "stack_path": man["stack_path"],
        "compose_file": man["compose_file"],  # basename; restore.py computes against the target
        "project_name": man.get("project_name"),
        "db_services": man.get("db_services") or [],
        "named_volumes": man.get("named_volumes") or [],
        "exclude_paths": man.get("exclude_paths") or [],
        "extra_backup_paths": man.get("extra_backup_paths") or [],
        # Kept raw: the target Compose resolver validates the untrusted sidecar
        # shape and maps service/container identities to target host paths.
        "external_bind_descriptors": man.get("external_bind_descriptors"),
        "exclude_patterns": man.get("exclude_patterns") or [],
        "db_autodetect": man.get("db_autodetect", True),
        "hooks_present": effective_hooks_present,
        "custom_restore_required": bool(
            man.get("custom_restore_required", False) or legacy_gitlab
        ),
        "template": template,
        "schedule": man.get("schedule"),
        "quiesce_services": man.get("quiesce_services") or [],
        "quiesce_disabled": bool(man.get("quiesce_disabled", False)),
        # The manifest deliberately carries NO shell: empty hooks, not approved.
        # Restore either resolves an exact local template or accepts --restore-cmd.
        "hooks": {"pre_backup": [], "post_backup": [], "restore": []},
        "hooks_allowed": False,
        "hooks_fingerprint": None,
        "repo": repo,
        "key_file": key_file,
        "backend_env_file": None,
        "mount_check": None,
        "retention": man.get("retention") or dict(config.DEFAULT_RETENTION),
        # Internal bootstrap diagnostics; stripped before a config is persisted.
        "_manifest_schema_version": schema_version,
        # Keep the source-side stack identity separate from an operator's
        # cross-server --name override.  v5 restore uses it to authenticate the
        # immutable snapshot's ``stack:<name>`` tag.
        "_manifest_source_name": config.sanitize_name(man["name"]),
        "_manifest_snapshot_id": snapshot_id,
        "_manifest_snapshot_id_error": snapshot_id_error,
        "_template_descriptor_error": template_error,
    }


def _parse_manifest_snapshot_id(
    raw: Any, *, required: bool
) -> Tuple[Optional[str], Optional[str]]:
    """Parse an untrusted binding while leaving pre-v5 manifests readable."""
    if not required:
        return None, None
    try:
        return validate_snapshot_id(raw), None
    except ValueError as exc:
        return None, str(exc)


def _parse_template_descriptor(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate untrusted sidecar metadata without ever accepting shell fields."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "template descriptor is not an object"
    unknown = set(raw.keys()) - _TEMPLATE_DESCRIPTOR_KEYS
    if unknown:
        return None, "template descriptor has unknown field(s): %s" % ", ".join(sorted(unknown))
    missing = _TEMPLATE_DESCRIPTOR_KEYS - set(raw.keys())
    if missing:
        return None, "template descriptor is missing field(s): %s" % ", ".join(sorted(missing))
    name = raw.get("name")
    version = raw.get("version")
    source = raw.get("source")
    fingerprint = raw.get("hooks_fingerprint")
    present = raw.get("hooks_present")
    if not templates.valid_name(name):
        return None, "invalid template name"
    if (not isinstance(version, str) or not version.isdigit()
            or len(version) > 10):
        return None, "invalid template version"
    if source not in ("builtin", "operator"):
        return None, "invalid template source"
    if (not isinstance(fingerprint, str) or not fingerprint.startswith("sha256-v1:")
            or len(fingerprint) != len("sha256-v1:") + 64
            or any(c not in "0123456789abcdef" for c in fingerprint[len("sha256-v1:"):])):
        return None, "invalid template hook fingerprint"
    if not isinstance(present, bool):
        return None, "invalid hooks_present marker"
    return {
        "name": name,
        "version": version,
        "source": source,
        "hooks_fingerprint": fingerprint,
        "hooks_present": present,
    }, None


def find_manifests(base: str, max_depth: int = 2) -> List[Tuple[str, Dict[str, Any]]]:
    """Finds repos with a manifest under ``base`` (for ``ls --on-repo``).

    Scans ``<base>`` up to depth ``max_depth`` for ``docker-backup.manifest.json``.
    A recognized repo is not descended into further (no nested repo) — this saves
    walking the restic internals (data/, index/ …).
    """
    out = []  # type: List[Tuple[str, Dict[str, Any]]]
    base = os.path.abspath(base)
    if os.path.isdir(base):
        _scan(base, max_depth, out)
    out.sort(key=lambda rm: (rm[1].get("name") or "", rm[0]))
    return out


def _scan(d: str, depth: int, out: List[Tuple[str, Dict[str, Any]]]) -> None:
    man = read_at(os.path.join(d, MANIFEST_BASENAME))
    if man is not None:
        out.append((d, man))
        return  # a repo contains no further repo → do not descend
    if depth <= 0:
        return
    try:
        entries = sorted(os.listdir(d))
    except OSError:
        return
    for entry in entries:
        sub = os.path.join(d, entry)
        if os.path.isdir(sub):
            _scan(sub, depth - 1, out)


# --- small helpers ----------------------------------------------------------
def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown-host"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
