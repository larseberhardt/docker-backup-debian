"""User/template hooks: shell commands before/after the backup and on restore.

SECURITY: Hooks are arbitrary shell commands that run as **root** on an
unattended systemd timer. They therefore NEVER run automatically:

1. A hook only runs once it has been explicitly approved
   (``hooks_allowed=True`` via ``docker-backup set <name> --allow-hooks`` or
   ``create --allow-hooks``). If hooks are present but not approved, the run
   fails HARD (no silent skipping).
2. On approval, a fingerprint (SHA-256 over all command strings) is stored. If
   the on-disk command differs from the approved one at run time, the run
   refuses to execute — closing the "secretly changed after approval" hole.
3. Backend credentials (S3/B2/SFTP …) that ``runtime.load_backend_env`` puts
   into ``os.environ`` are kept out of the hook environment.

Known limitation: once a command is approved, it runs without further prompting
on the timer. There is no sandboxing (stdlib-only). Control = provenance
(in the repo, PR-reviewed) + human approval + fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import util

PHASES = ("pre_backup", "post_backup", "restore")

_DEFAULT_TIMEOUT = {"pre_backup": 3600, "post_backup": 3600, "restore": 7200}
_DEFAULT_ON_FAILURE = {"pre_backup": "abort", "post_backup": "warn", "restore": "abort"}

# Keys that look like backend secrets → do NOT inherit into hooks.
_BACKEND_SECRET_RE = re.compile(
    r"KEY|SECRET|PASSWORD|TOKEN|AWS_|B2_|RESTIC_|AZURE_|GOOGLE_|GCS_|SWIFT_", re.IGNORECASE
)


# --- hook objects / fingerprint --------------------------------------------
def make_hook(cmd: str, *, phase: str, on_failure: Optional[str] = None,
              timeout: Optional[int] = None, cwd: str = "stack",
              name: Optional[str] = None) -> Dict[str, Any]:
    """Builds a structured hook object with sensible per-phase defaults."""
    return {
        "cmd": cmd,
        "on_failure": on_failure or _DEFAULT_ON_FAILURE.get(phase, "abort"),
        "timeout": timeout if timeout is not None else _DEFAULT_TIMEOUT.get(phase, 3600),
        "cwd": cwd,
        "name": name,
    }


def phase_hooks(cfg: Dict[str, Any], phase: str) -> List[Dict[str, Any]]:
    hooks = cfg.get("hooks") or {}
    return [h for h in (hooks.get(phase) or []) if (h or {}).get("cmd")]


def has_commands(cfg: Dict[str, Any]) -> bool:
    return any(phase_hooks(cfg, p) for p in PHASES)


def describe_commands(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(phase, cmd) list in stable order — for display before approval."""
    out = []  # type: List[Tuple[str, str]]
    for phase in PHASES:
        for h in phase_hooks(cfg, phase):
            out.append((phase, h["cmd"]))
    return out


def compute_fingerprint(hooks: Dict[str, Any]) -> str:
    """Deterministic SHA-256 over all command strings (all phases)."""
    items = []  # type: List[str]
    for phase in PHASES:
        for h in (hooks.get(phase) or []):
            cmd = (h or {}).get("cmd")
            if cmd:
                items.append("%s\x00%s" % (phase, cmd))
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


def compute_definition_fingerprint(hooks: Dict[str, Any]) -> str:
    """Portable hash of every execution-relevant hook attribute.

    ``compute_fingerprint`` is the long-standing local approval format and must
    remain stable for existing configs.  Cross-server template reconstruction
    needs a stronger compatibility binding: a changed cwd, timeout or failure
    policy must mismatch just like a changed command.  Cosmetic hook names are
    deliberately omitted.
    """
    definitions = []  # type: List[Dict[str, Any]]
    for phase in PHASES:
        for h in phase_hooks({"hooks": hooks}, phase):
            definitions.append({
                "phase": phase,
                "cmd": h["cmd"],
                "cwd": h.get("cwd", "stack"),
                "timeout": h.get("timeout") or _DEFAULT_TIMEOUT.get(phase, 3600),
                "on_failure": h.get("on_failure") or _DEFAULT_ON_FAILURE.get(phase, "abort"),
            })
    payload = json.dumps(
        definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(b"docker-backup-hook-definition-v1\0" + payload).hexdigest()
    return "sha256-v1:%s" % digest


def approve(cfg: Dict[str, Any]) -> None:
    """Approves the currently configured hook commands (with fingerprint)."""
    cfg["hooks_allowed"] = True
    cfg["hooks_fingerprint"] = compute_fingerprint(cfg.get("hooks") or {})


def revoke(cfg: Dict[str, Any]) -> None:
    """Revokes the approval — hooks no longer run afterwards."""
    cfg["hooks_allowed"] = False
    cfg["hooks_fingerprint"] = None


# --- gate + execution -------------------------------------------------------
def ensure_allowed(cfg: Dict[str, Any]) -> None:
    """Raises if hooks are present but not (validly) approved.

    Call early in the run (fail-fast), so that a post hook in a ``finally`` is
    not the first place where the misconfiguration surfaces.
    """
    if not has_commands(cfg):
        return
    name = cfg.get("name", "<stack>")
    if not cfg.get("hooks_allowed"):
        raise util.CommandError(
            ["hooks"], 1,
            "Hooks are configured but not approved. Review the commands "
            "('docker-backup ls' / config) and approve them: "
            "'docker-backup set %s --allow-hooks'." % name,
        )
    if cfg.get("hooks_fingerprint") != compute_fingerprint(cfg.get("hooks") or {}):
        raise util.CommandError(
            ["hooks"], 1,
            "Hook commands have changed since approval (fingerprint mismatch) "
            "— execution refused. Review the commands and approve again: "
            "'docker-backup set %s --allow-hooks'." % name,
        )


def run_hooks(cfg: Dict[str, Any], phase: str) -> None:
    """Runs all hooks of a phase (after the gate check). No-op without hooks."""
    hooks = phase_hooks(cfg, phase)
    if not hooks:
        return
    ensure_allowed(cfg)  # idempotent; safe even when called directly
    for h in hooks:
        _run_one_hook(cfg, phase, h)


def build_hook_env(cfg: Dict[str, Any], phase: str) -> Dict[str, str]:
    """COMPLETE, secret-free environment for a hook (with env_replace=True)."""
    env = {k: v for k, v in os.environ.items() if not _BACKEND_SECRET_RE.search(k)}
    env["DOCKER_BACKUP_STACK_PATH"] = cfg.get("stack_path", "") or ""
    env["DOCKER_BACKUP_NAME"] = cfg.get("name", "") or ""
    env["DOCKER_BACKUP_COMPOSE_FILE"] = cfg.get("compose_file", "") or ""
    env["DOCKER_BACKUP_PROJECT"] = cfg.get("project_name", "") or ""
    env["DOCKER_BACKUP_PHASE"] = phase
    return env


def _run_one_hook(cfg: Dict[str, Any], phase: str, h: Dict[str, Any]) -> None:
    cmd = (h or {}).get("cmd")
    if not cmd:
        return
    cwd_mode = (h or {}).get("cwd", "stack")
    cwd = cfg.get("stack_path") if cwd_mode == "stack" else cwd_mode
    timeout = (h or {}).get("timeout") or _DEFAULT_TIMEOUT.get(phase, 3600)
    on_failure = (h or {}).get("on_failure") or _DEFAULT_ON_FAILURE.get(phase, "abort")
    util.info("Hook(%s): %s" % (phase, (h or {}).get("name") or cmd))
    try:
        # The argv list holds the contract; config values reach the command only via
        # env variables (DOCKER_BACKUP_*), never via string interpolation → no injection.
        # capture=False → hook output streams into the journal (and is NOT scrubbed).
        util.run(["/bin/sh", "-c", cmd], env=build_hook_env(cfg, phase), env_replace=True,
                 cwd=cwd, timeout=timeout, capture=False, mutating=True)
    except util.CommandError:
        if on_failure == "warn":
            util.warn("Hook(%s) failed — ignored (on_failure=warn)." % phase)
            return
        raise
