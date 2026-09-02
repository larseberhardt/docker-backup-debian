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
  log "restic not found — bootstrapping via apt-get…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y restic || die "restic installation failed."
  else
    die "No apt-get available. Please install restic manually (https://restic.net)."
  fi
fi

# Debian's stable package can lag several restic releases behind. Use that
# package only as a bootstrap and then pull the latest official binary.
#
# Preferred path: restic's own self-update — it selects the architecture and
# verifies the release's signed SHA256SUMS. Debian/Ubuntu, however, build the
# package *without* that command (restic then reports an unknown command); in
# that case the release is fetched from GitHub and checked against SHA256SUMS
# here.

fetch() {  # fetch <url> <dest|->
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$2" "$1"
  else
    return 127
  fi
}

# True if $1 >= $2 (version sort).
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

install_restic_latest() {
  local arch ver tmp base file want got cur

  case "$(uname -m)" in
    x86_64|amd64)   arch=amd64 ;;
    aarch64|arm64)  arch=arm64 ;;
    armv7l|armv6l)  arch=arm ;;
    i386|i686)      arch=386 ;;
    *) warn "Unknown architecture $(uname -m) — cannot fetch an official restic build."; return 1 ;;
  esac

  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      apt-get install -y curl >/dev/null 2>&1 || true
    fi
    command -v curl >/dev/null 2>&1 || { warn "Neither curl nor wget available."; return 1; }
  fi

  ver="$(fetch https://api.github.com/repos/restic/restic/releases/latest - 2>/dev/null \
         | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' | head -n1)"
  [ -n "$ver" ] || { warn "Could not determine the latest restic release (GitHub unreachable?)."; return 1; }

  cur="$(restic version 2>/dev/null | awk '{print $2; exit}')" || cur=""
  if [ -n "$cur" ] && ver_ge "$cur" "$ver"; then
    log "restic $cur is already current."
    return 0
  fi

  tmp="$(mktemp -d)"
  file="restic_${ver}_linux_${arch}.bz2"
  base="https://github.com/restic/restic/releases/download/v${ver}"

  if ! fetch "$base/$file" "$tmp/$file" || ! fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS"; then
    warn "Download of restic $ver failed."
    rm -rf "$tmp"; return 1
  fi

  want="$(awk -v f="$file" '$2 == f || $2 == "*" f { print $1; exit }' "$tmp/SHA256SUMS")"
  got="$(sha256sum "$tmp/$file" | awk '{print $1}')"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    warn "SHA256 check for $file failed — not installing this download."
    rm -rf "$tmp"; return 1
  fi

  if command -v bunzip2 >/dev/null 2>&1; then
    bunzip2 -c "$tmp/$file" > "$tmp/restic"
  else
    python3 -c 'import bz2,shutil,sys
with bz2.open(sys.argv[1], "rb") as src, open(sys.argv[2], "wb") as dst:
    shutil.copyfileobj(src, dst)' "$tmp/$file" "$tmp/restic"
  fi

  install -m 0755 "$tmp/restic" /usr/local/bin/restic || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  hash -r
  log "restic $ver installed to /usr/local/bin/restic."
}

log "Updating restic to the latest official release…"
if restic self-update --help >/dev/null 2>&1; then
  restic self-update || warn "restic self-update failed (GitHub unreachable?) — keeping the installed version."
else
  install_restic_latest || warn "Could not install the latest restic — keeping the installed version."
fi
hash -r
log "restic: $(restic version 2>/dev/null | head -n1 || echo 'unknown')"

# Safe automatic repository detection needs restic >= 0.17 (dedicated missing-repo
# exit code); that version also supports correct-size sparse restores. A current
# restic is strongly recommended for restore, hard-link and metadata fixes.
RESTIC_VER="$(restic version 2>/dev/null | awk '{print $2; exit}')" || RESTIC_VER=""
if [ -n "${RESTIC_VER:-}" ]; then
  if [ "$(printf '%s\n' "$RESTIC_VER" 0.17.0 | sort -V | head -n1)" != "0.17.0" ]; then
    warn "restic $RESTIC_VER is OLDER than 0.17 — repository detection is ambiguous and safe automatic initialization is unavailable."
    warn "Install a newer restic (backports or https://restic.net), then rerun install.sh."
  elif [ "$(printf '%s\n' "$RESTIC_VER" 0.19.1 | sort -V | head -n1)" != "0.19.1" ]; then
    warn "restic $RESTIC_VER works; >= 0.19.1 is recommended for current restore fixes."
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
