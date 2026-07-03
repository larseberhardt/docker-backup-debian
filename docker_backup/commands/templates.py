"""``templates list`` / ``templates show <name>`` — inspect app templates."""

from __future__ import annotations

import json

from .. import templates, util


def cmd_templates(args) -> int:
    if getattr(args, "templates_action", None) == "show":
        return _show(args.name)
    return _list()


def _list() -> int:
    names = templates.list_templates()
    if not names:
        print("No templates available.")
        return 0
    rows = []
    for n in names:
        try:
            desc = templates.load(n).get("description", "")
        except util.CommandError as exc:
            desc = "(invalid: %s)" % exc
        rows.append((n, desc[:70]))
    util.print_table(("TEMPLATE", "DESCRIPTION"), rows)
    print("\nApply: docker-backup create <path> --from-template <name>")
    return 0


def _show(name: str) -> int:
    t = templates.load(name)
    print(json.dumps(t, indent=2, ensure_ascii=False))
    cmds = templates.proposed_commands(t)
    if cmds:
        util.warn("This template contains commands that run as ROOT — they are only "
                  "executed after explicit approval (--allow-hooks):")
        for ph, c in cmds:
            util.warn("    [%s] %s" % (ph, c))
    return 0
