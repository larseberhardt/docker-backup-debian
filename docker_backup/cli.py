"""argparse CLI and dispatch."""

from __future__ import annotations

import argparse
from typing import List, Optional

from . import __version__, config, selfupdate, status, util
from .commands import check as check_cmd
from .commands import create as create_cmd
from .commands import doctor as doctor_cmd
from .commands import key as key_cmd
from .commands import logs as logs_cmd
from .commands import ls as ls_cmd
from .commands import notify as notify_cmd
from .commands import remove as remove_cmd
from .commands import restore as restore_cmd
from .commands import run as run_cmd
from .commands import setcfg as setcfg_cmd
from .commands import snapshots as snapshots_cmd
from .commands import templates as templates_cmd
from .commands import update as update_cmd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docker-backup",
        description="Back up and restore Docker Compose stacks with restic.",
    )
    p.add_argument("--version", action="version", version="docker-backup " + __version__)
    p.add_argument("--dry-run", action="store_true",
                   help="Only show mutating commands, do not run them.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # --- create / create --all [--auto] ---
    c = sub.add_parser(
        "create",
        help="Set up stack(s) for backup ('--all' for all running stacks).",
    )
    c.add_argument("path", nargs="?", help="Path to the stack folder (omitted with --all).")
    c.add_argument("-a", "--all", action="store_true",
                   help="All running stacks (with a prompt per stack).")
    c.add_argument("--auto", action="store_true",
                   help="With --all: one shared target, all stacks without prompting.")
    c.add_argument("--target", dest="target", default=None,
                   help="Backup target (base) — single stack or '--all --auto'.")
    c.add_argument("--offsite", default=None, help="Offsite target (base) for 3-2-1.")
    c.add_argument("--schedule", default=None,
                   help="Frequency, e.g. 'daily 03:00' (default), 'weekly Mon 04:00', 'custom <expr>'.")
    c.add_argument("--from-template", dest="from_template", default=None, metavar="NAME",
                   help="Use an app template as a starting point (e.g. 'gitlab'). "
                        "'--list-templates' shows the available ones.")
    c.add_argument("--list-templates", dest="list_templates", action="store_true",
                   help="List available app templates and exit.")
    c.add_argument("--name", default=None, help="Override the stack name.")
    c.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    c.add_argument("--non-interactive", action="store_true", help="Do not prompt.")
    c.add_argument("--dump-user", dest="dump_user", default=None,
                   help="Force the DB dump role (default: detected; Supabase → supabase_admin).")
    cg = c.add_mutually_exclusive_group()
    cg.add_argument("--dump-globals", dest="dump_globals", action="store_true", default=None,
                    help="Include cluster globals (roles/passwords via pg_dumpall) in the backup.")
    cg.add_argument("--no-dump-globals", dest="dump_globals", action="store_false", default=None,
                    help="Do NOT include globals (overrides auto-detection).")
    c.add_argument("--no-quiesce", dest="no_quiesce", action="store_true",
                   help="Do not freeze mongo/redis for the file capture "
                        "(backup is then only crash-consistent).")
    c.add_argument("--no-db-detect", dest="db_autodetect", action="store_const", const=False,
                   default=None,
                   help="Disable DB auto-detection — no DB is dumped automatically "
                        "(e.g. GitLab, which ships its own dump command as a hook).")
    c.add_argument("--exclude", dest="exclude", action="append", default=None, metavar="PATTERN",
                   help="Exclude a path/glob relative to the stack from the backup (repeatable).")
    c.add_argument("--keep-within", dest="keep_within", default=None, metavar="DAUER",
                   help="Age retention: additionally keep ALL snapshots from the last DAUER, "
                        "e.g. '30d' (restic --keep-within, additive).")
    c.add_argument("--pre-cmd", dest="pre_cmd", default=None,
                   help="Shell command BEFORE the backup (e.g. 'docker exec gitlab gitlab-backup create CRON=1').")
    c.add_argument("--post-cmd", dest="post_cmd", default=None,
                   help="Shell command AFTER the backup (cleanup, runs even on failure).")
    c.add_argument("--restore-cmd", dest="restore_cmd", default=None,
                   help="Custom restore command instead of the built-in DB import.")
    c.add_argument("--allow-hooks", dest="allow_hooks", action="store_true",
                   help="Allow the configured hook commands (otherwise they do NOT run; they run "
                        "as root — check them first!).")
    c.set_defaults(func=create_cmd.cmd_create)

    # --- ls ---
    l = sub.add_parser("ls", help="List configured backups.")
    l.add_argument("--snapshots", action="store_true",
                   help="Show the latest restic snapshot per stack (slower).")
    l.add_argument("--on-repo", dest="on_repo", default=None,
                   help="Instead of local configs: list backed-up stacks on a mounted "
                        "backup drive (scans repo manifests).")
    l.set_defaults(func=ls_cmd.cmd_ls)

    # --- restore ---
    r = sub.add_parser("restore", help="Restore a stack from a backup.")
    r.add_argument("dest", help="Target folder, e.g. /opt/xibo-test.")
    r.add_argument("--from", dest="from_name", default=None,
                   help="Source config (stack name); default: derived from the target base name.")
    r.add_argument("--from-repo", dest="from_repo", default=None,
                   help="Bootstrap restore directly from a repo path on the mounted "
                        "drive (without a local config; requires --key-file).")
    r.add_argument("--key-file", dest="key_file", default=None,
                   help="restic key file (for --from-repo).")
    r.add_argument("--name", dest="bootstrap_name", default=None,
                   help="With --from-repo: set the stack name (default: from the manifest).")
    r.add_argument("--save-config", dest="save_config", action="store_true",
                   help="With --from-repo: save the reconstructed config locally (default: ephemeral).")
    r.add_argument("--snapshot", default="latest", help="restic snapshot (default: latest).")
    r.add_argument("--force", action="store_true", help="Overwrite a non-empty target.")
    r.add_argument("--no-custom-restore", dest="no_custom_restore", action="store_true",
                   help="Ignore the custom restore command, force the built-in DB import.")
    r.add_argument("--restore-cmd", dest="restore_cmd", default=None,
                   help="Provide a custom restore command (mainly for --from-repo, since the "
                        "manifest carries no shell). Providing it = allowing it.")
    r.set_defaults(func=restore_cmd.cmd_restore)

    # --- run (called by systemd) ---
    rn = sub.add_parser("run", help="Run a stack's backup (called by systemd).")
    rn.add_argument("name", nargs="?", help="Stack name (omitted with --all).")
    rn.add_argument("--all", action="store_true", help="Back up all stacks one after another.")
    rn.set_defaults(func=run_cmd.cmd_run)

    # --- doctor ---
    d = sub.add_parser("doctor", help="Health check of configured stacks.")
    d.add_argument("name", nargs="?", help="Check only this stack.")
    d.add_argument("--all", action="store_true", help="All stacks (default without a name).")
    d.set_defaults(func=doctor_cmd.cmd_doctor)

    # --- check ---
    ck = sub.add_parser("check", help="Verify the integrity of the restic repos (restic check).")
    ck.add_argument("name", nargs="?", help="Check only this stack.")
    ck.add_argument("--all", action="store_true", help="All stacks (default without a name).")
    ck.add_argument("--read-data-subset", default=None,
                    help="Read a fraction of the data in full, e.g. '5%%' (slow).")
    ck.add_argument("--refresh-cache", action="store_true",
                    help="(internal) Check all stacks and write the status cache.")
    ck.set_defaults(func=check_cmd.cmd_check)

    # --- snapshots ---
    sn = sub.add_parser("snapshots", help="List all restic snapshots of a stack.")
    sn.add_argument("name", help="Stack name.")
    sn.set_defaults(func=snapshots_cmd.cmd_snapshots)

    # --- logs ---
    lg = sub.add_parser("logs", help="Show a stack's journal.")
    lg.add_argument("name", help="Stack name.")
    lg.add_argument("-f", "--follow", action="store_true", help="Follow the log live.")
    lg.add_argument("-n", "--lines", type=int, default=80, help="Number of lines (default 80).")
    lg.add_argument("--notify", action="store_true", help="Also include the notify unit.")
    lg.set_defaults(func=logs_cmd.cmd_logs)

    # --- rm / remove ---
    rm = sub.add_parser("rm", aliases=["remove"],
                        help="Remove the backup setup (repo & key remain).")
    rm.add_argument("name", help="Stack name.")
    rm.add_argument("--purge-keys", action="store_true",
                    help="Also delete keys/secrets/backend env (IRREVERSIBLE).")
    rm.add_argument("-y", "--yes", action="store_true", help="Without prompting.")
    rm.set_defaults(func=remove_cmd.cmd_remove)

    # --- key ---
    k = sub.add_parser("key", help="Manage restic keys.")
    k.set_defaults(func=key_cmd.cmd_key)
    ksub = k.add_subparsers(dest="key_action", metavar="<action>")
    k_show = ksub.add_parser("show", help="Show the key + escrow note (SECRET!).")
    k_show.add_argument("name", help="Stack name.")
    k_show.set_defaults(func=key_cmd.cmd_show)

    # --- set ---
    stp = sub.add_parser("set", help="Change individual fields of a configured stack.")
    stp.add_argument("name", help="Stack name.")
    stp.add_argument("--schedule", default=None, help="New frequency (e.g. 'weekly Mon 04:00').")
    stp.add_argument("--retention", default=None, help="daily/weekly/monthly, e.g. '7/4/6'.")
    stp.add_argument("--offsite", default=None, help="Offsite base ('' = disable).")
    stp.add_argument("--offsite-retention", dest="offsite_retention", default=None,
                     metavar="D/W/M",
                     help="Own retention for the offsite repo, e.g. '30/12/24' "
                          "(default: same as --retention).")
    so = stp.add_mutually_exclusive_group()
    so.add_argument("--offsite-prune", dest="offsite_prune", action="store_true", default=None,
                    help="Prune the offsite repo after each copy (default).")
    so.add_argument("--no-offsite-prune", dest="offsite_prune", action="store_false", default=None,
                    help="Never prune the offsite repo (keeps everything, grows unbounded).")
    stp.add_argument("--dump-user", dest="dump_user", default=None,
                     help="Set the DB dump role for all DB services (e.g. supabase_admin).")
    sg = stp.add_mutually_exclusive_group()
    sg.add_argument("--dump-globals", dest="dump_globals", action="store_true", default=None,
                    help="Include cluster globals (roles/passwords) for Postgres in the backup.")
    sg.add_argument("--no-dump-globals", dest="dump_globals", action="store_false", default=None,
                    help="Do not include Postgres globals in the backup.")
    stp.add_argument("--exclude", dest="exclude", action="append", default=None, metavar="PATTERN",
                     help="Add an exclude pattern (repeatable).")
    stp.add_argument("--exclude-clear", dest="exclude_clear", action="store_true",
                     help="Remove all user-defined exclude patterns.")
    stp.add_argument("--keep-within", dest="keep_within", default=None, metavar="DAUER",
                     help="Set age retention, e.g. '30d' (restic --keep-within, additive).")
    stp.add_argument("--no-keep-within", dest="no_keep_within", action="store_true",
                     help="Disable age retention (--keep-within).")
    stp.add_argument("--pre-cmd", dest="pre_cmd", default=None,
                     help="Set the pre-backup command ('' deletes it). Resets the approval.")
    stp.add_argument("--post-cmd", dest="post_cmd", default=None,
                     help="Set the post-backup command ('' deletes it). Resets the approval.")
    stp.add_argument("--restore-cmd", dest="restore_cmd", default=None,
                     help="Set a custom restore command ('' deletes it). Resets the approval.")
    stp.add_argument("--clear-hooks", dest="clear_hooks", action="store_true",
                     help="Remove all hooks and revoke the approval.")
    sh = stp.add_mutually_exclusive_group()
    sh.add_argument("--allow-hooks", dest="allow_hooks", action="store_true", default=None,
                    help="Allow the configured hook commands (after review; they run as root).")
    sh.add_argument("--no-allow-hooks", dest="allow_hooks", action="store_false", default=None,
                    help="Revoke the approval — hooks no longer run afterwards.")
    sq = stp.add_mutually_exclusive_group()
    sq.add_argument("--quiesce", dest="quiesce", action="store_true", default=None,
                    help="Re-enable the built-in mongo/redis quiesce (default).")
    sq.add_argument("--no-quiesce", dest="quiesce", action="store_false", default=None,
                    help="Skip the mongo/redis quiesce (backup only crash-consistent).")
    stp.add_argument("--target", default=None, help="DANGEROUS: new repo target.")
    stp.add_argument("--i-know-this-orphans-the-old-repo", dest="ack_dangerous",
                     action="store_true", help="Confirm --target.")
    stp.set_defaults(func=setcfg_cmd.cmd_set)

    # --- notify ---
    n = sub.add_parser("notify", help="Set up/test email notifications.")
    n.set_defaults(func=notify_cmd.cmd_notify)
    nsub = n.add_subparsers(dest="notify_action", metavar="<action>")
    n_setup = nsub.add_parser("setup", help="Set up SMTP notification interactively.")
    n_setup.set_defaults(func=notify_cmd.cmd_setup)
    n_test = nsub.add_parser("test", help="Send a test email.")
    n_test.set_defaults(func=notify_cmd.cmd_test)
    n_show = nsub.add_parser("show", help="Show the current config (password masked).")
    n_show.set_defaults(func=notify_cmd.cmd_show)
    n_fail = nsub.add_parser("failure", help="(internal) Send a failure notification.")
    n_fail.add_argument("name", help="Stack name.")
    n_fail.set_defaults(func=notify_cmd.cmd_failure)

    # --- templates ---
    tp = sub.add_parser("templates", help="List/show app templates.")
    tp.set_defaults(func=templates_cmd.cmd_templates)
    tpsub = tp.add_subparsers(dest="templates_action", metavar="<action>")
    tp_list = tpsub.add_parser("list", help="List available templates.")
    tp_list.set_defaults(func=templates_cmd.cmd_templates)
    tp_show = tpsub.add_parser("show", help="Show a template as JSON.")
    tp_show.add_argument("name", help="Template name.")
    tp_show.set_defaults(func=templates_cmd.cmd_templates)

    # --- update ---
    u = sub.add_parser("update", help="Update docker-backup from the git repo (runs update.sh).")
    u.add_argument("--check", action="store_true",
                   help="Only check whether a newer version is available.")
    u.add_argument("--branch", default=None, help="Override the branch (default from update.conf).")
    u.add_argument("-y", "--yes", action="store_true", help="Update without prompting.")
    u.set_defaults(func=update_cmd.cmd_update)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    util.set_dry_run(getattr(args, "dry_run", False))
    util.set_verbose(getattr(args, "verbose", False))

    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    selfupdate.maybe_print_update_notice(getattr(args, "command", None))
    status.maybe_print_check_notice(getattr(args, "command", None))

    try:
        return args.func(args) or 0
    except util.CommandError as exc:
        util.error(str(exc))
        if exc.stderr:
            util.error(exc.stderr.strip())
        return 1
    except KeyboardInterrupt:
        util.error("Aborted.")
        return 130
