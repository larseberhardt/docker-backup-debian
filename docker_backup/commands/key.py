"""``key show <name>`` — show the restic key + escrow notice again.

Nested command (like ``notify``) so that further actions (e.g. ``key rotate``)
can be added later.
"""

from __future__ import annotations

import os

from .. import config, keys, util


def cmd_key(args) -> int:
    util.error("Please specify an action: show <name>")
    return 1


def cmd_show(args) -> int:
    util.require_root()
    name = config.sanitize_name(args.name)
    if not os.path.exists(keys.key_path(name)):
        util.error("No key found for '%s'." % name)
        return 1
    util.warn("Warning: a SECRET will be shown in cleartext in the terminal.")
    print(keys.escrow_notice(name, keys.read_key(name)))
    return 0
