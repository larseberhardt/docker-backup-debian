"""App templates ("pre-configs"): bundled starter templates per application.

A template is a JSON file under ``docker_backup/templates/<app>.json`` (in the
repo, contributable by the community via PR) or as an operator override under
``/etc/docker-backup/templates/<app>.json`` (wins). ``docker-backup create
--from-template <app>`` uses it as a starting point; explicit CLI flags override
template values, after which the user refines via ``docker-backup set`` or a JSON edit.

SECURITY: A template command NEVER runs automatically. The commands are written
into the config but only run after explicit approval (``--allow-hooks``) — the
gate lives in :mod:`docker_backup.hooks`. Control = provenance
(in the repo, PR-reviewed) + human approval + fingerprint.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config, hooks, restic, util

TEMPLATE_SCHEMA_VERSION = 1

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_ALLOWED_KEYS = {
    "template_schema_version", "name", "description", "db_autodetect",
    "exclude_patterns", "schedule", "retention", "hooks", "match",
}
_ALLOWED_HOOK_KEYS = {"cmd", "on_failure", "timeout", "cwd", "name"}
_ALLOWED_RETENTION_KEYS = {"daily", "weekly", "monthly", "keep_within"}


# --- paths / discovery ------------------------------------------------------
def builtin_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def override_dir() -> str:
    return os.path.join(config.etc_dir(), "templates")


def _dirs() -> List[str]:
    # Operator override first (wins), then the bundled templates.
    return [override_dir(), builtin_dir()]


def list_templates() -> List[str]:
    names = set()  # type: set
    for d in _dirs():
        try:
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    names.add(fn[:-5])
        except OSError:
            continue
    return sorted(names)


def _path(name: str) -> Optional[str]:
    if not _NAME_RE.match(name or ""):
        raise util.CommandError(["--from-template"], 2, "Invalid template name: %r" % name)
    for d in _dirs():
        p = os.path.join(d, name + ".json")
        if os.path.exists(p):
            return p
    return None


def load(name: str) -> Dict[str, Any]:
    p = _path(name)
    if p is None:
        raise util.CommandError(
            ["--from-template"], 2,
            "No template '%s'. Available: %s" % (name, ", ".join(list_templates()) or "—"),
        )
    with open(p) as f:
        try:
            t = json.load(f)
        except ValueError as exc:
            raise util.CommandError(["--from-template"], 2,
                                    "Template '%s' is not valid JSON: %s" % (name, exc))
    return validate(t)


# --- validation -------------------------------------------------------------
def validate(t: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(t, dict):
        raise util.CommandError(["template"], 2, "Template must be a JSON object.")
    if t.get("template_schema_version") != TEMPLATE_SCHEMA_VERSION:
        raise util.CommandError(
            ["template"], 2,
            "Template schema version %r not supported (expected %d)."
            % (t.get("template_schema_version"), TEMPLATE_SCHEMA_VERSION),
        )
    if not t.get("name"):
        raise util.CommandError(["template"], 2, "Template needs a 'name' field.")
    unknown = set(t.keys()) - _ALLOWED_KEYS
    if unknown:
        raise util.CommandError(["template"], 2,
                                "Unknown template fields: %s" % ", ".join(sorted(unknown)))
    for p in (t.get("exclude_patterns") or []):
        restic.validate_exclude_pattern(p)  # rejects '..'/empty
    _validate_retention(t.get("retention"))
    _validate_hooks(t.get("hooks"))
    return t


def _validate_retention(ret: Any) -> None:
    if ret is None:
        return
    if not isinstance(ret, dict):
        raise util.CommandError(["template"], 2, "'retention' must be an object.")
    bad = set(ret.keys()) - _ALLOWED_RETENTION_KEYS
    if bad:
        raise util.CommandError(["template"], 2,
                                "Unknown retention fields: %s" % ", ".join(sorted(bad)))
    for k in ("daily", "weekly", "monthly"):
        if k in ret and (not isinstance(ret[k], int) or ret[k] < 0):
            raise util.CommandError(["template"], 2, "retention.%s must be an integer >= 0." % k)


def _validate_hooks(hk: Any) -> None:
    if hk is None:
        return
    if not isinstance(hk, dict):
        raise util.CommandError(["template"], 2, "'hooks' must be an object.")
    for phase, items in hk.items():
        if phase not in hooks.PHASES:
            raise util.CommandError(["template"], 2, "Unknown hook phase: %s" % phase)
        if not isinstance(items, list):
            raise util.CommandError(["template"], 2, "Hook phase '%s' must be a list." % phase)
        for h in items:
            if not isinstance(h, dict) or not h.get("cmd"):
                raise util.CommandError(["template"], 2, "Hook in '%s' needs a 'cmd'." % phase)
            bad = set(h.keys()) - _ALLOWED_HOOK_KEYS
            if bad:
                raise util.CommandError(["template"], 2,
                                        "Unknown hook fields: %s" % ", ".join(sorted(bad)))
            if h.get("on_failure", "abort") not in ("abort", "warn"):
                raise util.CommandError(["template"], 2,
                                        "on_failure must be 'abort' or 'warn'.")


# --- application ------------------------------------------------------------
def to_hooks(t: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Normalizes the hook definitions into full hook objects (per-phase defaults)."""
    out = {"pre_backup": [], "post_backup": [], "restore": []}  # type: Dict[str, List[Dict[str, Any]]]
    hk = t.get("hooks") or {}
    for phase in hooks.PHASES:
        for h in (hk.get(phase) or []):
            out[phase].append(hooks.make_hook(
                h["cmd"], phase=phase, on_failure=h.get("on_failure"),
                timeout=h.get("timeout"), cwd=h.get("cwd", "stack"), name=h.get("name")))
    return out


def proposed_commands(t: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(phase, cmd) list — for display before approval."""
    out = []  # type: List[Tuple[str, str]]
    hk = t.get("hooks") or {}
    for phase in hooks.PHASES:
        for h in (hk.get(phase) or []):
            out.append((phase, h["cmd"]))
    return out


def provenance(t: Dict[str, Any], source: str = "builtin") -> Dict[str, Any]:
    return {"name": t.get("name"), "version": str(t.get("template_schema_version", 1)),
            "source": source}


def detect_template(compose_json: Dict[str, Any]) -> Optional[str]:
    """Suggests a matching template (name) based on the service images, or None.

    Matches each template's ``match.image_tokens`` as a substring against the stack's
    images. The LONGEST matching token wins (most specific template): e.g.
    'nextcloud/all-in-one' (nextcloud-aio) must beat the generic 'nextcloud' —
    the two need completely different backup treatment. Ties keep the
    alphabetically first template (list_templates is sorted).
    """
    images = [
        str((svc or {}).get("image") or "").lower()
        for svc in (compose_json.get("services") or {}).values()
    ]
    best = None  # type: Optional[Tuple[int, str]]
    for name in list_templates():
        try:
            t = load(name)
        except util.CommandError:
            continue
        tokens = (t.get("match") or {}).get("image_tokens") or []
        for tok in tokens:
            tok = str(tok).lower()
            if tok and any(tok in img for img in images):
                if best is None or len(tok) > best[0]:
                    best = (len(tok), name)
    return best[1] if best else None
