"""Runtime helpers needed by several commands:
loading backend credentials and resolving DB passwords.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import compose, config, util


def load_backend_env(cfg: Dict[str, Any]) -> None:
    """Loads backend credentials (S3/B2/SFTP …) from the EnvironmentFile into os.environ.

    For systemd runs ``EnvironmentFile=`` handles this; for manual invocations
    we do it here.
    """
    path = cfg.get("backend_env_file")
    if not path or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ[k] = v
        if any(t in k.upper() for t in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
            util.register_secret(v)


def resolve_password(
    cfg: Dict[str, Any], db: Dict[str, Any], compose_json: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Resolves the DB password at run time.

    - ``env:<KEY>`` → fresh from ``docker compose config`` (picks up rotations)
    - ``stored``    → sidecar under /etc/docker-backup/secrets/
    - ``none``      → no password
    """
    src = db.get("password_source", "none")
    if src.startswith("env:"):
        key = src.split(":", 1)[1]
        if compose_json is None:
            compose_json = compose.config_json(
                cfg["compose_file"], cfg["stack_path"], cfg.get("project_name")
            )
        env = ((compose_json.get("services") or {}).get(db["service"]) or {}).get("environment") or {}
        val = env.get(key)
        util.register_secret(val)
        return val
    if src == "stored":
        val = config.read_secret(cfg["name"], db["service"])
        util.register_secret(val)
        return val
    return None
