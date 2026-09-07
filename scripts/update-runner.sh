#!/usr/bin/env bash
# =============================================================================
#  MeshTech-Bot - web-console update RUNNER (runs as root inside the
#  meshtech-bot-update transient unit, started by update-trigger.sh).
#
#  Does exactly what a careful human would do from the shell:
#    1. reads the clone path from config.yaml (updates.clone_path),
#    2. repairs clone ownership if an old root-run left root files behind
#       (the same self-healing manage.sh does),
#    3. pulls the requested branch AS THE CLONE'S OWNER (never root, so the
#       two-location rule holds: git happens in the home clone as its owner),
#    4. applies it with deploy.sh --no-pull --allow-downgrade (the web popup
#       already warned about downgrades; there is no human to type 'yes').
#
#  Everything it prints lands in data/update-log, which the dashboard serves
#  so the user watches the update happen line by line.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
LOG="$ROOT/data/update-log"
RUNTIME="${MESHTECH_RUNTIME:-/opt/meshtech-bot}"

# Trust any git directory for THIS job only (via the environment, not a
# config change): the clone belongs to another user and this root job must
# never be blocked by the safe.directory check.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0='*'

mkdir -p "$(dirname "$LOG")"
: > "$LOG"
chmod 644 "$LOG" 2>/dev/null || true
exec > "$LOG" 2>&1

log()  { printf '[runner] %s\n' "$*"; }
fail() { printf '[runner] ERROR: %s\n' "$*"; printf '[runner] UPDATE FAILED\n'; exit 1; }

echo "=== MeshTech-Bot web update - $(date) ==="

BRANCH="${1:-}"
[[ "$BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "invalid branch name '$BRANCH'"
log "Requested branch: $BRANCH"

# --- clone path from config ----------------------------------------------------
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
CLONE="$("$PY" -c "import sys,yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
print(((cfg.get('updates') or {}).get('clone_path') or '').strip())" \
  "$ROOT/config.yaml" 2>/dev/null || true)"
[[ -n "$CLONE" ]] || fail "updates.clone_path is not set in config.yaml - web updates are disabled (set it to your home clone, e.g. /home/you/meshtech-bot)"

# expand a leading ~ against the service account's home (documented as an
# absolute path, but be kind if someone used ~/)
if [[ "$CLONE" == "~/"* && -x "$(command -v getent)" ]]; then
  SVC_USER="$(stat -c '%U' "$RUNTIME" 2>/dev/null || echo meshtech)"
  SVC_HOME="$(getent passwd "$SVC_USER" 2>/dev/null | cut -d: -f6)"
  [[ -n "$SVC_HOME" ]] && CLONE="$SVC_HOME/${CLONE#~/}"
fi

[[ -d "$CLONE/.git" ]] || fail "no clone at $CLONE (fix updates.clone_path in config.yaml)"
log "Clone: $CLONE"

# --- ownership self-healing ------------------------------------------------------
OWNER="$(stat -c '%U' "$CLONE/.git" 2>/dev/null || true)"
[[ -n "$OWNER" ]] || fail "cannot determine the clone's owner"
ROOT_FILE="$(find "$CLONE" -user root -print -quit 2>/dev/null || true)"
if [[ -n "$ROOT_FILE" ]]; then
  log "Repairing clone ownership (a past root-run left root-owned files)..."
  chown -R "$OWNER" "$CLONE" || fail "could not repair clone ownership"
fi

# --- pull as the clone's owner (never root) ---------------------------------------
run_as_owner() { runuser -u "$OWNER" -- "$@"; }

run_as_owner git -C "$CLONE" fetch origin --prune \
  || fail "could not reach GitHub to fetch branches (offline?)"

run_as_owner git -C "$CLONE" rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1 \
  || fail "branch '$BRANCH' does not exist on GitHub (try: DEV or main)"

CUR="$(run_as_owner git -C "$CLONE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if [[ "$CUR" != "$BRANCH" ]]; then
  log "Switching the clone from '$CUR' to '$BRANCH'..."
  run_as_owner git -C "$CLONE" checkout "$BRANCH" 2>/dev/null \
    || run_as_owner git -C "$CLONE" checkout -b "$BRANCH" "origin/$BRANCH" \
    || fail "could not check out '$BRANCH' (uncommitted changes in the clone?)"
fi

if ! run_as_owner git -C "$CLONE" diff --quiet \
   || ! run_as_owner git -C "$CLONE" diff --cached --quiet; then
  fail "the clone has uncommitted changes - update refused (inspect: git -C $CLONE status)"
fi

log "Pulling the latest code as '$OWNER' ..."
run_as_owner git -C "$CLONE" pull --ff-only || fail "git pull failed - nothing was changed"

# --- apply with the standard, battle-tested deploy path ----------------------------
log "Applying to $RUNTIME ..."
"$ROOT/deploy.sh" --clone "$CLONE" --runtime "$RUNTIME" \
  --branch "$BRANCH" --no-pull --allow-downgrade
RC=$?

echo
if [[ "$RC" -eq 0 ]]; then
  echo "=== update finished OK - $(date) ==="
else
  echo "=== UPDATE FAILED (exit $RC) - $(date) ==="
fi
exit "$RC"
