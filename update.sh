#!/usr/bin/env bash
#
# Update mechanism for docker-backup.
#
#   sudo docker-backup update          # or directly:  sudo /opt/docker-backup/update.sh
#
# Fetches the latest release (git tag vX.Y.Z) from the configured public repo
# and reinstalls it (via install.sh). Configs/keys under /etc/docker-backup are
# preserved.
#
# Modes:
#   (none)            Fetch the latest release + reinstall.
#   --check           Only check whether a newer release exists (writes cache).
#   --refresh-cache   Refresh the cache silently (used by the systemd timer).
#   -y | --yes        Update without prompting.
#   --branch <b>      Override the branch (default from update.conf).
#   --repo-url <url>  Override the repo URL (default from update.conf).
#   -h | --help       This help.
#
# The "latest" version comes from git release tags vX.Y.Z. A release = bump
# __version__ in docker_backup/__init__.py, commit, then
# 'git tag vX.Y.Z && git push --tags'.
#
set -euo pipefail

ETC_DIR="${DOCKER_BACKUP_ETC:-/etc/docker-backup}"
PREFIX="${DOCKER_BACKUP_PREFIX:-/opt/docker-backup}"
REPO_DIR="${DOCKER_BACKUP_REPO_DIR:-/opt/docker-backup/repo}"
UPDATE_CONF="$ETC_DIR/update.conf"
CACHE="$ETC_DIR/.update-check.json"
LOCK="$ETC_DIR/.update-check.lock"

log()  { printf '[*] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'
}

# --- Dependencies -----------------------------------------------------------
ensure_git() {
  command -v git >/dev/null 2>&1 && return 0
  log "git not found — installing via apt-get…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y git || die "git installation failed."
  else
    die "No apt-get available. Please install git manually."
  fi
}

# --- Version helpers --------------------------------------------------------
version_from_file() {
  grep -E '^__version__' "$1" 2>/dev/null | head -n1 \
    | sed -E 's/.*["'"'"']([^"'"'"']+)["'"'"'].*/\1/'
}

installed_version() {
  local f="$PREFIX/docker_backup/__init__.py" v=""
  [ -f "$f" ] && v="$(version_from_file "$f" || true)"
  printf '%s' "${v:-0}"
}

# Tag names (with an optional leading v), semver only, one per line.
tag_names_from() {
  printf '%s\n' "$1" \
    | awk -F/ '/refs\/tags\// {print $NF}' \
    | grep -E '^v?[0-9]+(\.[0-9]+)*$'
}

highest_tag_from() {
  tag_names_from "$1" | sort -V | tail -n1
}

# True (rc=0) if version $1 is strictly greater than $2.
ver_gt() {
  [ "$1" = "$2" ] && return 1
  local smallest
  smallest="$(printf '%s\n%s\n' "${1#v}" "${2#v}" | sort -V | head -n1)"
  [ "$smallest" = "${2#v}" ]
}

count_behind() {  # $1=tags_output  $2=installed_version
  local n=0 ver
  while read -r ver; do
    [ -n "$ver" ] || continue
    if ver_gt "$ver" "$2"; then n=$((n + 1)); fi
  done < <(tag_names_from "$1" | sed 's/^v//')
  printf '%s' "$n"
}

# --- Read/write cache (JSON) via python3 (robust escaping) ------------------
cache_get() {  # $1=key
  [ -f "$CACHE" ] || return 0
  python3 - "$CACHE" "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    v = d.get(sys.argv[2])
    if v is not None:
        print(v)
except Exception:
    pass
PY
}

write_cache() {  # $1=latest  $2=behind  $3=error  $4=source_version
  mkdir -p "$ETC_DIR"
  DBP_CACHE="$CACHE" DBP_LATEST="$1" DBP_BEHIND="$2" DBP_ERR="$3" DBP_SRC="$4" \
  python3 - <<'PY'
import json, os, tempfile, datetime
cache = os.environ["DBP_CACHE"]
data = {
    "schema": 1,
    "latest_version": os.environ.get("DBP_LATEST") or None,
    "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "releases_behind": int(os.environ.get("DBP_BEHIND") or 0),
    "source_version": os.environ.get("DBP_SRC") or None,
    "error": os.environ.get("DBP_ERR") or None,
}
d = os.path.dirname(cache) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".update-check.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, cache)
    tmp = ""
finally:
    if tmp and os.path.exists(tmp):
        os.unlink(tmp)
PY
}

# --- Refresh the cache (network) --------------------------------------------
do_refresh() {
  local installed tags_output latest_tag latestver behind prev
  installed="$(installed_version)"
  if ! tags_output="$(git ls-remote --tags --refs "$REMOTE_URL" 2>/dev/null)"; then
    prev="$(cache_get latest_version)"
    write_cache "${prev:-}" 0 "fetch_failed" "$installed"
    return 1
  fi
  latest_tag="$(highest_tag_from "$tags_output" || true)"
  if [ -z "$latest_tag" ]; then
    write_cache "$installed" 0 "no_tags" "$installed"
    return 0
  fi
  latestver="${latest_tag#v}"
  behind="$(count_behind "$tags_output" "$installed")"
  write_cache "$latestver" "$behind" "" "$installed"
  return 0
}

# Refresh with a non-blocking lock — timer and manual run do not collide.
locked_refresh() {
  mkdir -p "$ETC_DIR" 2>/dev/null || true
  if command -v flock >/dev/null 2>&1 && exec 9>"$LOCK" 2>/dev/null; then
    if flock -n 9; then
      do_refresh
    else
      log "Another update check is running — skipped."
      return 0
    fi
  else
    do_refresh
  fi
}

print_check_status() {
  local installed latest behind err
  installed="$(installed_version)"
  latest="$(cache_get latest_version)"
  behind="$(cache_get releases_behind)"
  err="$(cache_get error)"
  [ -n "$err" ] && warn "Check failed with error '$err' — showing last known state."
  if [ -z "$latest" ]; then
    log "No version information available."
    return 0
  fi
  if ver_gt "$latest" "$installed"; then
    log "Update available: docker-backup $installed -> $latest (${behind:-?} release(s) behind)."
    log "Update with: sudo docker-backup update"
  else
    log "docker-backup is up to date (version $installed)."
  fi
}

ensure_repo() {
  if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" remote set-url origin "$REMOTE_URL" 2>/dev/null || true
    # --force + --prune-tags: a re-set or moved tag of the same name (a
    # corrected release) would otherwise NOT be pulled into the local clone —
    # git rejects existing tags without --force.
    git -C "$REPO_DIR" fetch --tags --prune --prune-tags --force --quiet origin
  else
    rm -rf "$REPO_DIR"
    mkdir -p "$(dirname "$REPO_DIR")"
    log "Cloning $REMOTE_URL to $REPO_DIR…"
    git clone --quiet "$REMOTE_URL" "$REPO_DIR"
    git -C "$REPO_DIR" fetch --tags --force --quiet origin || true
  fi
}

# --- Arguments --------------------------------------------------------------
MODE="update"
ASSUME_YES=0
BRANCH_OVERRIDE=""
URL_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check)          MODE="check" ;;
    --refresh-cache)  MODE="refresh" ;;
    -y|--yes)         ASSUME_YES=1 ;;
    --branch)         shift; BRANCH_OVERRIDE="${1:-}" ;;
    --branch=*)       BRANCH_OVERRIDE="${1#*=}" ;;
    --repo-url)       shift; URL_OVERRIDE="${1:-}" ;;
    --repo-url=*)     URL_OVERRIDE="${1#*=}" ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "Unknown option: $1 (see --help)." ;;
  esac
  shift
done

# --- Load configuration -----------------------------------------------------
REMOTE_URL=""
BRANCH="main"
if [ -f "$UPDATE_CONF" ]; then
  # shellcheck disable=SC1090
  . "$UPDATE_CONF"
fi
[ -n "$URL_OVERRIDE" ] && REMOTE_URL="$URL_OVERRIDE"
[ -n "$BRANCH_OVERRIDE" ] && BRANCH="$BRANCH_OVERRIDE"
: "${BRANCH:=main}"

# --- Execution --------------------------------------------------------------
case "$MODE" in
  refresh)
    if [ -z "$REMOTE_URL" ]; then
      write_cache "" 0 "unconfigured" "$(installed_version)"
      exit 0
    fi
    ensure_git
    locked_refresh || true
    exit 0
    ;;

  check)
    if [ -z "$REMOTE_URL" ]; then
      warn "Auto-update not configured ($UPDATE_CONF: REMOTE_URL missing)."
      exit 1
    fi
    ensure_git
    locked_refresh || true
    print_check_status
    exit 0
    ;;

  update)
    [ "$(id -u)" -eq 0 ] || die "Please run as root (sudo docker-backup update)."
    [ -n "$REMOTE_URL" ] || die "Auto-update not configured: create $UPDATE_CONF (REMOTE_URL=, BRANCH=) or use --repo-url."
    ensure_git

    INSTALLED="$(installed_version)"
    TAGS="$(git ls-remote --tags --refs "$REMOTE_URL" 2>/dev/null)" \
      || die "git ls-remote failed — check network/URL ($REMOTE_URL)."
    LATEST_TAG="$(highest_tag_from "$TAGS" || true)"
    [ -n "$LATEST_TAG" ] || die "No release tags (vX.Y.Z) found in the repo."
    LATESTVER="${LATEST_TAG#v}"

    if [ "$LATESTVER" = "$INSTALLED" ]; then
      log "Already up to date (version $INSTALLED)."
      locked_refresh || true
      exit 0
    fi
    if ver_gt "$INSTALLED" "$LATESTVER"; then
      warn "Installed ($INSTALLED) is newer than the latest release ($LATESTVER) — this would be a downgrade."
      [ "$ASSUME_YES" = 1 ] || die "Aborting. Force with --yes."
    fi

    log "Updating docker-backup: $INSTALLED -> $LATESTVER  (tag $LATEST_TAG)"
    if [ "$ASSUME_YES" != 1 ]; then
      printf 'Continue? [y/N] '
      read -r ANS || ANS=""
      case "$ANS" in y|Y) ;; *) die "Aborted." ;; esac
    fi

    ensure_repo
    git -C "$REPO_DIR" -c advice.detachedHead=false checkout --force --quiet "$LATEST_TAG"

    # Sanity check: the tag must point at a commit whose __version__ matches the
    # tag. Otherwise the installed version would stay unchanged after install and
    # the update notice would return endlessly (the release was tagged without a
    # version bump). Better to fail clearly here than to loop silently.
    SRC_VER="$(version_from_file "$REPO_DIR/docker_backup/__init__.py" || true)"
    if [ -n "$SRC_VER" ] && [ "$SRC_VER" != "$LATESTVER" ]; then
      die "Release error: tag $LATEST_TAG points at code with __version__=$SRC_VER (expected $LATESTVER). Bump __version__ in the repo and re-set the tag on that commit — installation aborted, it would change nothing."
    fi

    log "Starting installation from $REPO_DIR…"
    # exec: replaces this process; install.sh runs from the freshly fetched
    # checkout and overwrites /opt, never the file currently running.
    exec "$REPO_DIR/install.sh"
    ;;
esac
