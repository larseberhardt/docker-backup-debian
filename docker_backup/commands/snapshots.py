"""``snapshots <name>`` — list all restic snapshots of a stack."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .. import config, restic, runtime, util


def cmd_snapshots(args) -> int:
    util.require_root()
    name = config.sanitize_name(args.name)
    if not config.exists(name):
        util.error("No config named '%s'." % name)
        return 1
    cfg = config.load(name)
    repo, key = cfg.get("repo"), cfg.get("key_file")
    runtime.load_backend_env(cfg)

    if not util.DRY_RUN and not restic.repo_initialized(repo, key):
        util.error("restic repo not reachable: %s" % repo)
        return 1
    try:
        proc = util.run(restic.build_snapshots(repo, key, latest=False), capture=True)
        data = json.loads(proc.stdout or "[]")
    except (util.CommandError, ValueError):
        util.error("Snapshots could not be read.")
        return 1

    rows = [_row(s) for s in data if isinstance(s, dict)]  # type: List[Tuple[Any, ...]]
    util.print_table(("ID", "TIME", "HOST", "TAGS", "PATHS"), rows)
    util.info("%d snapshot(s)." % len(rows))
    return 0


def _row(s: Dict[str, Any]) -> Tuple[Any, ...]:
    sid = s.get("short_id") or (s.get("id") or "")[:8]
    when = str(s.get("time", ""))[:19]
    host = s.get("hostname", "")
    tags = ",".join(s.get("tags") or [])
    paths = ",".join(s.get("paths") or [])
    return (sid, when, host, tags, paths)
