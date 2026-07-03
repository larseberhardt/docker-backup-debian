"""``logs <name> [-f] [-n N] [--notify]`` — show a stack's journal.

Thin wrapper around ``journalctl -u docker-backup@<name>.service`` (optionally also
the notify unit).
"""

from __future__ import annotations

from typing import List

from .. import config, util


def build_journalctl_argv(name: str, follow: bool, lines: int, notify: bool) -> List[str]:
    argv = ["journalctl", "-u", "docker-backup@%s.service" % name]
    if notify:
        argv += ["-u", "docker-backup-notify@%s.service" % name]
    argv += ["-n", str(lines), "--no-pager"]
    if follow:
        argv.append("-f")
    return argv


def cmd_logs(args) -> int:
    util.require_root()
    name = config.sanitize_name(args.name)
    if not config.exists(name):
        util.error("No config named '%s'." % name)
        return 1
    argv = build_journalctl_argv(name, args.follow, args.lines, getattr(args, "notify", False))
    util.run(argv, capture=False, check=False, mutating=False)
    return 0
