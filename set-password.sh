#!/usr/bin/env bash
# =============================================================================
#  MeshTech-Bot - dashboard password helper
#
#  One command to set (first run) or change the dashboard password:
#
#      cd /opt/meshtech-bot
#      sudo ./set-password.sh
#
#  What it does:
#    1. asks you to type the password twice (so a typo can't lock you out)
#    2. writes it to data/.dashboard_password  (mode 600, owned by the bot)
#    3. restarts the bot service so the new password is active immediately
#
#  Run it again any time you want to change the password.
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PW_FILE="${PW_FILE:-$DIR/data/.dashboard_password}"

log()  { printf '\033[1;32m[password]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[password]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "$(id -u)" -ne 0 ]]; then
  die "Please run with sudo:  sudo ./set-password.sh"
fi

mkdir -p "$DIR/data"

echo "---------------------------------------------------------------------"
echo " MeshTech-Bot dashboard password"
echo " The bot reads the first line of data/.dashboard_password at startup."
echo "---------------------------------------------------------------------"

read -r -s -p " New dashboard password: " password
echo
read -r -s -p " Type it again to confirm: " confirm
echo

if [[ -z "$password" ]]; then
  die "The password cannot be empty - run the script again."
fi
if [[ "$password" != "$confirm" ]]; then
  die "The two entries did not match - run the script again."
fi

# Write the file. The bot's account owns the install folder (native install:
# meshtech; Docker host: 1001) - take ownership from the folder itself so the
# right account always ends up as the file owner. Root may always read it.
printf '%s\n' "$password" > "$PW_FILE"
chown "$(stat -c %U:%G "$DIR")" "$PW_FILE" 2>/dev/null || true
chmod 600 "$PW_FILE"

log "Password saved to $PW_FILE (mode 600)."

# Restart the service if it is running so the change applies right away.
if systemctl list-unit-files meshtech-bot.service &>/dev/null; then
  if systemctl is-active --quiet meshtech-bot.service; then
    log "Restarting meshtech-bot ..."
    systemctl restart meshtech-bot.service
    log "Done - log in to the dashboard with your new password."
    exit 0
  fi
fi

log "The bot service is not running (or this is a Docker/manual install)."
log "Start the bot, then log in with your new password."
