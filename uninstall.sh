#!/usr/bin/env bash
#
# Uninstaller for docker-backup (counterpart to install.sh).
#
#   sudo ./uninstall.sh   (or sudo /opt/docker-backup/uninstall.sh)
#
# Idempotent. Removes program, symlink, systemd units and completion.
# /etc/docker-backup (configs + KEYS!) is deleted only after confirmation.
#
set -euo pipefail

PREFIX="/opt/docker-backup"
BIN_LINK="/usr/local/bin/docker-backup"
ETC_DIR="/etc/docker-backup"
SYSTEMD_DIR="/etc/systemd/system"
COMPLETION="/etc/bash_completion.d/docker-backup"

log()  { printf '[*] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Please run as root (sudo ./uninstall.sh)."

# --- Stop + disable per-stack timers ----------------------------------------
log "Disabling timers…"
for t in "$SYSTEMD_DIR"/docker-backup@*.timer; do
  [ -e "$t" ] || continue
  unit="$(basename "$t")"
  case "$unit" in *@.timer) continue ;; esac   # skip the template
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
done
for u in docker-backup-check.timer docker-backup-update-check.timer; do
  systemctl disable --now "$u" >/dev/null 2>&1 || true
done

# --- Remove unit files + drop-ins -------------------------------------------
log "Removing systemd units…"
rm -f "$SYSTEMD_DIR"/docker-backup@.service \
      "$SYSTEMD_DIR"/docker-backup@.timer \
      "$SYSTEMD_DIR"/docker-backup-notify@.service \
      "$SYSTEMD_DIR"/docker-backup-update-check.service \
      "$SYSTEMD_DIR"/docker-backup-update-check.timer \
      "$SYSTEMD_DIR"/docker-backup-check.service \
      "$SYSTEMD_DIR"/docker-backup-check.timer
rm -rf "$SYSTEMD_DIR"/docker-backup@*.timer.d
systemctl daemon-reload
systemctl reset-failed 'docker-backup*' >/dev/null 2>&1 || true

# --- Symlink, program, completion -------------------------------------------
log "Removing program and symlink…"
rm -f "$BIN_LINK"
rm -rf "$PREFIX"
rm -f "$COMPLETION"

# --- /etc/docker-backup: only after confirmation (contains keys!) -----------
if [ -d "$ETC_DIR" ]; then
  warn "$ETC_DIR contains restic keys — without them NO restore is possible."
  printf 'REMOVE configuration and keys under %s? [y/N] ' "$ETC_DIR"
  read -r ANS || ANS=""
  case "$ANS" in
    y|Y|yes|YES) rm -rf "$ETC_DIR"; log "Removed: $ETC_DIR" ;;
    *) log "Kept: $ETC_DIR (keys/configs preserved)." ;;
  esac
fi

log "docker-backup uninstalled."
