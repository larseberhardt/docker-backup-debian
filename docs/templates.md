# App templates ("pre-configs")

A template is a small JSON file that pre-fills a `docker-backup create` run for a specific
application (GitLab, Supabase, …). The user applies it with `--from-template <name>`, then
refines the result. Templates are meant to be **contributed by the community via PR**.

- **Shipped templates:** [`docker_backup/templates/<app>.json`](../docker_backup/templates/)
  (carried by `install.sh` automatically — no packaging change needed).
- **Operator override:** `/etc/docker-backup/templates/<app>.json` (same name wins over the
  shipped one).

## Security rule: templates never run shell on their own

A template may declare `hooks` (pre/post backup, custom restore). **These commands run as
root**, so they are written into the stack config but stay **disabled** (`hooks_allowed=false`)
until the user explicitly approves them with `--allow-hooks` (or
`docker-backup set <name> --allow-hooks`). A backup with unapproved hooks **fails hard**;
it is never silently skipped. On approval a SHA-256 fingerprint of the commands is stored,
and a later change to a command blocks the run until it is re-approved.

There is no sandboxing. The trust model is: **provenance** (in-repo, PR-reviewed) +
**human approval** + **change-fingerprint**. Review every command in a template before
approving it.

## Trusted reconstruction on another server

A v5 repository manifest is bound to one full restic snapshot ID and stores only an
allowlisted template descriptor: template name, schema version, exact local source
(`builtin` or `operator`), whether hooks are present, and a SHA-256 over the normalized
execution fields (`phase`, command, cwd, timeout and failure policy). Raw hook commands are
never copied into the plaintext sidecar.

`restore --from-repo ... --use-template-hooks` loads only that exact local source (a
builtin descriptor cannot be shadowed by `/etc`), compares the version and hash, displays
every command and its execution policy, and asks for default-no approval independently of
`--force`. Metadata alone never executes anything. `--save-config` requires this mode; all
phases are saved and approved only after the full restore succeeds, target paths and
selected external binds are recalculated, the supplied key is installed in the managed key
directory, and the timer is explicitly stopped/disabled for review. Source overrides of
template-owned excludes, schedule, retention or DB auto-detection are not silently lost:
strict config saving refuses them and asks the operator to recreate/review the config
manually after restoring.

An `operator` template used for reconstruction must be a regular, root-owned file and
must not be group/world-writable or a symlink.

The sidecar is not signed, so its hash detects incompatible/customized hooks but is not
an authenticity proof. The security boundary is that executable bytes come only from a
locally installed template and are visibly confirmed. Legacy manifests and mismatches
must use a reviewed `--restore-cmd` or be replaced by a fresh backup manifest.

## Schema (`template_schema_version: 1`)

```jsonc
{
  "template_schema_version": 1,        // required, must be 1
  "name": "gitlab",                    // required
  "description": "one line shown in `templates list`",
  "db_autodetect": false,              // optional; false => skip DB auto-dump
  "schedule": "daily 04:00",           // optional; same syntax as --schedule
  "retention": {                       // optional; counts + optional age window
    "daily": 7, "weekly": 0, "monthly": 0, "keep_within": "30d"
  },
  "exclude_patterns": [                // optional; RELATIVE to the stack folder
    "gitlab/logs", "gitlab/data/postgresql"
  ],
  "match": {                           // optional; powers `detect_template` suggestions
    "image_tokens": ["gitlab/gitlab-ce", "gitlab/gitlab-ee"]
  },
  "hooks": {                           // optional; commands run as root AFTER approval
    "pre_backup":  [ {"cmd": "...", "on_failure": "abort", "timeout": 3600, "cwd": "stack", "name": "..."} ],
    "post_backup": [ {"cmd": "...", "on_failure": "warn"} ],
    "restore":     [ {"cmd": "...", "on_failure": "abort"} ]
  }
}
```

Field rules (enforced by `templates.validate`, which runs in a test over **every** shipped
template):

- Unknown top-level keys are rejected.
- `exclude_patterns` may not contain `..` (no traversal). With a `/` they are anchored to
  the stack root; a bare name matches that basename anywhere.
- Each hook needs a `cmd`. `on_failure` is `abort` (default for pre/restore) or `warn`
  (default for post). `cwd` is `"stack"` (the stack folder, default) or an absolute path.
  Hooks receive `DOCKER_BACKUP_STACK_PATH`, `DOCKER_BACKUP_NAME`,
  `DOCKER_BACKUP_COMPOSE_FILE`, `DOCKER_BACKUP_PROJECT`, `DOCKER_BACKUP_PHASE`.
- `retention` counts must be integers ≥ 0; `keep_within` is a restic duration like `30d`.

## Precedence when applying

`docker-backup create <path> --from-template <name>` resolves each setting as
**explicit CLI flag > template value > built-in default**. `--exclude` patterns are
**added** to the template's. CLI `--pre-cmd`/`--post-cmd`/`--restore-cmd` override the
template's command for that phase.

## Contributing a new template

1. Add `docker_backup/templates/<app>.json` following the schema above.
2. Prefer the env var or a stack-relative path over hardcoded absolute paths in commands
   (e.g. `rm -f app/dump/*.tar` with `cwd: "stack"`).
3. Double-check that the path where the app writes its own dump is **not** excluded.
4. Run `python3 -m unittest discover -s tests` — the template-validation test must pass.
5. Open a PR. Reviewers verify the commands are safe and the excludes don't drop live data
   the app needs.
