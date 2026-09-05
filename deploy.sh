#!/usr/bin/env bash
# =============================================================================
#  MeshTech-Bot - update the bot the safe way
#
#  Two locations, two jobs:
#    ~/meshtech-bot      YOUR clone - git happens here, as you (no sudo)
#    /opt/meshtech-bot   the RUNTIME - locked down; only its config.yaml,
#                        data/ and .venv are unique to it
#
#  deploy.sh pulls the latest code into your clone, then copies everything
#  EXCEPT config.yaml / data / .venv into the runtime, fixes ownership,
#  refreshes dependencies, validates your real config and restarts the
#  service.  Your settings and captured data are never touched.
#
#  Usage:
#      ./deploy.sh                  # from your home clone (asks sudo once)
#      sudo ./deploy.sh             # or under sudo (pull still runs as you)
#      ./deploy.sh --dry-run        # list what WOULD change in /opt (no sudo,
#                                   #   no pull, nothing modified anywhere)
#      deploy.sh --clone DIR --runtime DIR --no-restart
# =============================================================================
set -euo pipefail

CLONE="${HOME}/meshtech-bot"
RUNTIME="/opt/meshtech-bot"
SERVICE_USER="meshtech"
SERVICE_GROUP="meshtech"
SERVICE="meshtech-bot"
DO_RESTART=1
DRY_RUN=0
APPLY_TARBALL=""

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m WARNING: %s\n' "$*"; }
die()  { printf '\033[1;31m[deploy]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clone)     CLONE="$2"; shift 2 ;;
    --runtime)   RUNTIME="$2"; shift 2 ;;
    --user)      SERVICE_USER="$2"; SERVICE_GROUP="$2"; shift 2 ;;
    --service)   SERVICE="$2"; shift 2 ;;
    --no-restart) DO_RESTART=0; shift ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --apply)     APPLY_TARBALL="$2"; shift 2 ;;   # internal: sudo re-exec
    -h|--help)   sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
done

# ==============================================================================
#  Phase 2 - root side: apply the staged tarball to the runtime
# ==============================================================================
if [[ -n "$APPLY_TARBALL" ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "--apply must run as root"
  [[ -f "$APPLY_TARBALL" ]] || die "staged tarball missing: $APPLY_TARBALL"
  [[ -f "$RUNTIME/bot.py" ]] || die "no bot installation at $RUNTIME (run install.sh first)"
  id "$SERVICE_USER" &>/dev/null || die "service account '$SERVICE_USER' missing - run install.sh first"

  log "Applying new code to $RUNTIME ..."
  tar xf "$APPLY_TARBALL" -C "$RUNTIME"
  # accept both member spellings ('./.git-commit' or '.git-commit' - tar
  # stores whatever name it was given, and versions of this script differ)
  STAMP="$(tar xOf "$APPLY_TARBALL" ./.git-commit 2>/dev/null \
    || tar xOf "$APPLY_TARBALL" .git-commit 2>/dev/null \
    || true)"
  # a deploy without a stamp means the staging half is broken - refuse to
  # continue silently (this exact silence is what let a stale stamp persist)
  [[ -n "$STAMP" ]] || die "staged tarball has no .git-commit stamp - refusing to deploy"
  printf '%s\n' "$STAMP" > "$RUNTIME/.git-commit"
  log "Applying code from $STAMP"
  rm -f "$APPLY_TARBALL"

  # mirror install.sh's permission scheme (code is data here, not a checkout
  # anyone edits, so the blanket chown/chmod is safe and keeps it consistent)
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RUNTIME"
  find "$RUNTIME" -type d -exec chmod 750 {} \;
  find "$RUNTIME" -type f -exec chmod 640 {} \;
  chmod 755 "$RUNTIME/bot.py" "$RUNTIME/install.sh" "$RUNTIME/deploy.sh" \
            "$RUNTIME/set-password.sh" "$RUNTIME/manage.sh" \
            "$RUNTIME/scripts/configure_bot.py" 2>/dev/null || true
  if [[ -d "$RUNTIME/.venv" ]]; then
    find "$RUNTIME/.venv" -type d -exec chmod 755 {} \;
    find "$RUNTIME/.venv" -type f -exec chmod 755 {} \; 2>/dev/null || true
  fi

  PY="$RUNTIME/.venv/bin/python"
  [[ -x "$PY" ]] || PY="$(command -v python3)"
  log "Refreshing Python dependencies..."
  "$PY" -m pip install -q -r "$RUNTIME/requirements.txt" 2>/dev/null \
    || warn "pip install had warnings (continuing)"

  log "Validating your config with the new code..."
  # Run from the RUNTIME directory: bot.py resolves data/ (password file,
  # database) relative to the current directory, so validating from elsewhere
  # would check a throwaway state instead of the live one. Validate as the
  # service user when possible (proves it can read the config from its own
  # folder); fall back to root on systems without sudo.
  if command -v sudo &>/dev/null; then
    if ! (cd "$RUNTIME" && sudo -u "$SERVICE_USER" "$PY" bot.py --check --config "$RUNTIME/config.yaml"); then
      warn "could not validate as '$SERVICE_USER' - retrying as root"
      (cd "$RUNTIME" && "$PY" bot.py --check --config "$RUNTIME/config.yaml") \
        || die "validation failed - the OLD code keeps running until this passes"
    fi
  else
    (cd "$RUNTIME" && "$PY" bot.py --check --config "$RUNTIME/config.yaml") \
      || die "validation failed - the OLD code keeps running until this passes"
  fi

  if [[ "$DO_RESTART" -eq 1 ]]; then
    log "Restarting the service..."
    systemctl restart "$SERVICE.service" \
      || die "restart failed - check: systemctl status $SERVICE"
    log "Deploy complete: running $([[ -n "$STAMP" ]] && echo "$STAMP" || echo 'new code')."
  else
    log "Deploy complete (no restart requested)."
  fi
  exit 0
fi

# ==============================================================================
#  Phase 1 - pull phase: always runs AS THE INVOKING USER (never root),
#  so the home clone never accumulates root-owned files.
# ==============================================================================
INVOKER="${SUDO_USER:-$(id -un)}"
INVOKER_HOME=""
if command -v getent &>/dev/null; then
  INVOKER_HOME="$(getent passwd "$INVOKER" 2>/dev/null | cut -d: -f6)"
fi
[[ "$CLONE" == "${HOME}/meshtech-bot" && -n "$INVOKER_HOME" ]] && CLONE="$INVOKER_HOME/meshtech-bot"

if [[ ! -d "$CLONE/.git" ]]; then
  log "No clone at $CLONE - creating one (as $INVOKER, no sudo needed)..."
  sudo -u "$INVOKER" git clone https://github.com/mygooglyeyes/MeshTech-bot.git "$CLONE" \
    || die "clone failed - check your network, or pass --clone /path"
fi
[[ -f "$CLONE/bot.py" ]] || die "$CLONE does not look like MeshTech-Bot"

if [[ "$(id -u)" -eq 0 && "$INVOKER" != "root" ]]; then
  PULL=(sudo -u "$INVOKER" git -C "$CLONE")
else
  PULL=(git -C "$CLONE")
fi

if ! "${PULL[@]}" diff --quiet || ! "${PULL[@]}" diff --cached --quiet; then
  die "your clone has uncommitted changes - inspect them first:  cd $CLONE && git status"
fi
log "Pulling the latest code into $CLONE ..."
STAMP=""
if "${PULL[@]}" pull --ff-only; then
  STAMP="$("${PULL[@]}" rev-parse HEAD)"
  log "Pulled: $STAMP"
elif [[ "$DRY_RUN" -eq 1 ]]; then
  warn "could not pull (offline? no upstream?) - comparing the clone's"
  warn "  CURRENT state against the runtime instead."
  STAMP="$("${PULL[@]}" rev-parse HEAD 2>/dev/null || echo unknown)"
else
  die "git pull failed (offline? upstream moved?) - nothing changed"
fi

# ------------------------------------------------------------------------------
# Dry-run: compare the freshly pulled clone against the runtime and list what
# WOULD change.  Read-only: no sudo, nothing staged, nothing applied.
# ------------------------------------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  log "DRY RUN - what would change in $RUNTIME (deploying $STAMP):"
  if [[ ! -f "$RUNTIME/bot.py" ]]; then
    warn "  no runtime found at $RUNTIME - a deploy would create it fresh"
    exit 0
  fi
  STAGE="$(mktemp /tmp/meshtech-dryrun-XXXXXX.tar)"
  trap 'rm -f "$STAGE"' EXIT
  (cd "$CLONE" && tar cf "$STAGE" \
      --exclude=./.git --exclude=./.freebuff \
      --exclude=./config.yaml --exclude=./data \
      --exclude=./.venv --exclude=./venv \
      --exclude='./config.yaml.bak-*' \
      --exclude=./.git-commit \
      .)
  # list files that differ (content or missing on either side)
  new=0; changed=0
  while IFS= read -r f; do
    rel="${f#./}"
    if [[ ! -e "$RUNTIME/$rel" ]]; then
      echo "  new      $rel"; new=$((new+1))
    elif ! cmp -s "$CLONE/$rel" "$RUNTIME/$rel"; then
      echo "  changed  $rel"; changed=$((changed+1))
    fi
  done < <(cd "$CLONE" && tar tf "$STAGE" | grep -v '/$')
  echo "  ------"
  echo "  would add $new file(s), update $changed file(s)"
  echo "  untouched: config.yaml, data/, .venv (and any extra files already in"
  echo "  the runtime that the new code does not contain)"
  echo
  log "No changes were made. Run ./deploy.sh to apply."
  exit 0
fi

# ==============================================================================
#  Stage what the runtime needs (everything except its unique state)
# ==============================================================================
STAGE="$(mktemp /tmp/meshtech-deploy-XXXXXX.tar)"
trap 'rm -f "$STAGE"; rm -f "$CLONE/.git-commit"' EXIT
(cd "$CLONE" && tar cf "$STAGE" \
    --exclude=./.git --exclude=./.freebuff \
    --exclude=./config.yaml --exclude=./data \
    --exclude=./.venv --exclude=./venv \
    --exclude='./config.yaml.bak-*' \
    --exclude=./.git-commit \
    .)
# bake the FRESH commit stamp into the tarball (an old leftover file in the
# clone must never win - write it unconditionally, append, then clean up)
# the member is added as './.git-commit' so phase 2's reader finds it
(cd "$CLONE" && printf '%s\n' "$STAMP" > .git-commit && tar rf "$STAGE" ./.git-commit)
chmod 644 "$STAGE"

# ==============================================================================
#  Phase 2 hand-off
# ==============================================================================
if [[ "$(id -u)" -eq 0 ]]; then
  exec "$0" --apply "$STAGE" --runtime "$RUNTIME" --user "$SERVICE_USER" \
            --service "$SERVICE" $([[ "$DO_RESTART" -eq 1 ]] || echo --no-restart)
else
  log "Applying to $RUNTIME (sudo will ask for your password once)..."
  exec sudo "$0" --apply "$STAGE" --runtime "$RUNTIME" --user "$SERVICE_USER" \
                 --service "$SERVICE" $([[ "$DO_RESTART" -eq 1 ]] || echo --no-restart)
fi
