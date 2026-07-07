"""docker-backup — back up and restore Docker Compose stacks with restic."""

from __future__ import annotations

# Single source of truth for the version. On a release, bump it and set a
# matching git tag: 'git tag vX.Y.Z' must match __version__
# (the update check compares __version__ against the highest vX.Y.Z tag).
__version__ = "1.0.1"
