#!/usr/bin/env bash
#
# Installer for docker-backup (Debian/Ubuntu with systemd).
#
#   sudo ./install.sh
#
# Idempotent: can be run again to update. Configs, keys and backend credentials
# under /etc/docker-backup are preserved.
#
set -euo pipefail

PREFIX="/opt/docker-backup"
BIN_LINK="/usr/local/bin/docker-backup"
ETC_DIR="/etc/docker-backup"
SYSTEMD_DIR="/etc/systemd/system"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '[*] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Please run as root (sudo ./install.sh)."

# --- Dependencies -----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 is required."

if ! command -v docker >/dev/null 2>&1; then
  warn "docker not found — please install Docker Engine + Compose plugin."
fi

if ! command -v restic >/dev/null 2>&1; then
  log "restic not found — installing via apt-get…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y restic || die "restic installation failed."
  else
    die "No apt-get available. Please install restic manually (https://restic.net)."
  fi
fi
log "restic: $(restic version 2>/dev/null | head -n1 || echo 'unknown')"

# Offsite (init/copy --from-repo) needs restic >= 0.14; >= 0.16 also removes
# stale repo locks on its own. Distro packages can be older (Ubuntu 22.04: 0.12).
RESTIC_VER="$(restic version 2>/dev/null | awk '{print $2; exit}')"
if [ -n "${RESTIC_VER:-}" ]; then
  if [ "$(printf '%s\n' "$RESTIC_VER" 0.14.0 | sort -V | head -n1)" != "0.14.0" ]; then
    warn "restic $RESTIC_VER is OLDER than 0.14 — offsite backups (3-2-1) will NOT work."
    warn "Install a newer restic (backports or https://restic.net), then rerun install.sh."
  elif [ "$(printf '%s\n' "$RESTIC_VER" 0.16.0 | sort -V | head -n1)" != "0.16.0" ]; then
    warn "restic $RESTIC_VER works; >= 0.16 is recommended (auto-removes stale repo locks)."
  fi
fi

if ! command -v git >/dev/null 2>&1; then
  log "git not found — installing via apt-get… (for 'docker-backup update')"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y git || warn "git installation failed — auto-update unavailable."
  else
    warn "No apt-get available — please install git manually (for auto-update)."
  fi
fi

# --- Preload helper image (for named-volume backups) ------------------------
if command -v docker >/dev/null 2>&1; then
  log "Pulling helper image 'busybox' (for named-volume backups)…"
  docker pull busybox >/dev/null 2>&1 || warn "busybox could not be preloaded (will be pulled on demand)."
fi

# --- Install program --------------------------------------------------------
log "Installing program to $PREFIX"
mkdir -p "$PREFIX"
# Copy only program parts (no tests/plan/.git)
rm -rf "$PREFIX/docker_backup"
cp -a "$SRC_DIR/docker_backup" "$PREFIX/docker_backup"
mkdir -p "$PREFIX/bin"
cp -a "$SRC_DIR/bin/docker-backup" "$PREFIX/bin/docker-backup"
cp -a "$SRC_DIR/README.md" "$PREFIX/README.md" 2>/dev/null || true
cp -a "$SRC_DIR/update.sh" "$PREFIX/update.sh"
cp -a "$SRC_DIR/uninstall.sh" "$PREFIX/uninstall.sh"
chmod 0755 "$PREFIX/bin/docker-backup" "$PREFIX/update.sh" "$PREFIX/uninstall.sh"

ln -sfn "$PREFIX/bin/docker-backup" "$BIN_LINK"
log "Symlink: $BIN_LINK -> $PREFIX/bin/docker-backup"

# --- Configuration directories ----------------------------------------------
log "Creating $ETC_DIR"
mkdir -p "$ETC_DIR/configs" "$ETC_DIR/keys" "$ETC_DIR/backends" "$ETC_DIR/secrets"
chmod 0755 "$ETC_DIR"
chmod 0750 "$ETC_DIR/configs"
chmod 0700 "$ETC_DIR/keys" "$ETC_DIR/backends" "$ETC_DIR/secrets"
chown -R root:root "$ETC_DIR"

# --- Configure auto-update --------------------------------------------------
# Installed from a git checkout? Then record the origin URL + branch in
# update.conf so 'docker-backup update' and the daily check work.
# An existing update.conf is NOT overwritten (operator edits are preserved).
if [ ! -f "$ETC_DIR/update.conf" ] \
   && git -C "$SRC_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  REMOTE_URL="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
  BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  case "$BRANCH" in ""|HEAD) BRANCH="main" ;; esac
  if [ -n "$REMOTE_URL" ]; then
    printf 'REMOTE_URL="%s"\nBRANCH="%s"\n' "$REMOTE_URL" "$BRANCH" > "$ETC_DIR/update.conf"
    chmod 0644 "$ETC_DIR/update.conf"
    log "Auto-update configured: $REMOTE_URL (branch $BRANCH)"
  else
    warn "git checkout without an 'origin' remote — auto-update not configured."
  fi
elif [ ! -f "$ETC_DIR/update.conf" ]; then
  warn "Installation not from a git checkout — auto-update is not configured."
  warn "Create $ETC_DIR/update.conf (REMOTE_URL=…, BRANCH=main) or use 'update.sh --repo-url'."
fi

# --- systemd units ----------------------------------------------------------
log "Installing systemd units"
cp -a "$SRC_DIR/systemd/docker-backup@.service"             "$SYSTEMD_DIR/docker-backup@.service"
cp -a "$SRC_DIR/systemd/docker-backup@.timer"               "$SYSTEMD_DIR/docker-backup@.timer"
cp -a "$SRC_DIR/systemd/docker-backup-notify@.service"      "$SYSTEMD_DIR/docker-backup-notify@.service"
cp -a "$SRC_DIR/systemd/docker-backup-update-check.service" "$SYSTEMD_DIR/docker-backup-update-check.service"
cp -a "$SRC_DIR/systemd/docker-backup-update-check.timer"   "$SYSTEMD_DIR/docker-backup-update-check.timer"
cp -a "$SRC_DIR/systemd/docker-backup-check.service"        "$SYSTEMD_DIR/docker-backup-check.service"
cp -a "$SRC_DIR/systemd/docker-backup-check.timer"          "$SYSTEMD_DIR/docker-backup-check.timer"
systemctl daemon-reload

# Enable the daily update check and fill the cache once up front, so the
# notice does not take up to a day to appear.
systemctl enable --now docker-backup-update-check.timer >/dev/null 2>&1 \
  || warn "Update-check timer could not be enabled."
"$PREFIX/update.sh" --refresh-cache || true

# Enable the weekly integrity check (restic check). It first runs at the next
# timer trigger — no run during installation (too slow).
systemctl enable --now docker-backup-check.timer >/dev/null 2>&1 \
  || warn "Integrity-check timer could not be enabled."

# --- Bash completion (optional) ---------------------------------------------
if [ -d /etc/bash_completion.d ]; then
  cp -a "$SRC_DIR/completion/docker-backup.bash" /etc/bash_completion.d/docker-backup 2>/dev/null \
    && chmod 0644 /etc/bash_completion.d/docker-backup \
    || warn "Bash completion could not be installed."
fi

cat <<EOF

============================================================
 docker-backup installed.

 Next steps:
   docker-backup create /opt/<app>                       # set up a single stack (wizard)
   docker-backup create --all                            # select stacks (Y/n per stack)
   docker-backup create --all --auto --target /mnt/backups  # all stacks, no prompts
   docker-backup ls                                      # overview
   docker-backup doctor                                  # health check
   docker-backup restore /opt/<app>-test                 # restore
   docker-backup notify setup                            # email on backup failures (optional)
   docker-backup update                                  # update to the latest release

 IMPORTANT: Back up the restic keys under $ETC_DIR/keys/
 offline as well — without them no restore is possible (see README).
============================================================
EOF
