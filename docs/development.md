# Development

```bash
# Syntax check (Python 3.9 compatible)
python3 -m py_compile docker_backup/*.py docker_backup/commands/*.py

# Bash syntax check of the installer/update scripts
bash -n install.sh update.sh uninstall.sh

# Unit/smoke tests (without Docker/restic; use fixtures and argv builders)
python3 -m unittest discover -s tests

# CLI help
python3 -m docker_backup --help
python3 -m docker_backup create --help

# Dry run (shows the commands that would be executed, changes nothing)
sudo docker-backup --dry-run run xibo
```

## Publishing a release

`docker-backup update` installs the highest `vX.Y.Z` tag, and the update check
compares the installed `__version__` against it, so the tag and the version string
must match.

1. Bump `__version__` in `docker_backup/__init__.py` and commit.
2. Tag and push:

```bash
git tag vX.Y.Z && git push origin main --tags   # e.g. v1.1.3
```

## Building the documentation

The docs live in `docs/` (plain Markdown, readable on GitHub) and are published
with [MkDocs](https://www.mkdocs.org) and the Material theme via GitHub Pages.
A push to `main` that touches `docs/` or `mkdocs.yml` triggers the
`docs` workflow, which builds with `--strict` (broken links fail the build) and
deploys.

Preview locally:

```bash
pip install mkdocs-material        # or: uvx --with mkdocs-material mkdocs serve
mkdocs serve                       # http://127.0.0.1:8000
mkdocs build --strict              # what CI runs
```
