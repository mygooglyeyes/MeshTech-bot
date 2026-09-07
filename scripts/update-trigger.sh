#!/usr/bin/env bash
# =============================================================================
#  MeshTech-Bot - web-console update TRIGGER (the ONE command the bot may run
#  as root through sudo).
#
#  Flow: the dashboard writes the requested branch to data/update-request and
#  runs THIS script via a sudoers rule that allows exactly one thing: this
#  exact path, with NO arguments.  The bot never gains any other root power.
#
#  This script then:
#    1. re-validates the branch name (the bot validated it too - belt and
#       suspenders against anyone tampering with the request file),
#    2. refuses to start twice at once (the transient systemd unit name
#       collides with a still-running update),
#    3. hands the real work to update-runner.sh as a DETACHED systemd
#       transient unit, so the sudo call returns immediately and the update
#       survives the bot restarting itself mid-way.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
REQ="$ROOT/data/update-request"
UNIT="meshtech-bot-update"

fail() { printf '[update-trigger] ERROR: %s\n' "$*" >&2; exit 1; }

# --- validate the request ----------------------------------------------------
[[ -r "$REQ" ]] || fail "no update request found ($REQ)"
BRANCH="$(head -1 "$REQ" | tr -d '[:space:]')"
[[ -n "$BRANCH" ]] || fail "empty update request"
[[ "$BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || fail "invalid branch name '$BRANCH' (letters, digits, dot, dash, underscore only)"

# --- one update at a time ------------------------------------------------------
# A still-existing unit (active OR failed and not yet collected) means an
# update is in flight or just finished - never start a second one.
if systemctl list-units --all "$UNIT.service" 2>/dev/null | grep -q "$UNIT"; then
  # an old FAILED unit from a previous run blocks the name - reset it
  if [[ "$(systemctl is-active "$UNIT.service" 2>/dev/null || true)" == "failed" ]]; then
    systemctl reset-failed "$UNIT.service" 2>/dev/null || true
  else
    fail "an update is already running ($UNIT.service)"
  fi
fi

# --- launch detached -----------------------------------------------------------
# systemd-run returns once the unit is STARTED, not finished - exactly what
# the bot needs: a fast answer and work that outlives the bot's restart.
systemd-run --unit="$UNIT" --collect \
  --working-directory="$ROOT" \
  "$SCRIPT_DIR/update-runner.sh" "$BRANCH" \
  || fail "could not start the update (systemd-run failed)"

printf '[update-trigger] update to branch %s started (%s.service)\n' "$BRANCH" "$UNIT"
