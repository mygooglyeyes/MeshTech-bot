#!/usr/bin/env bash
# =============================================================================
#  MeshTech-Bot - control panel (SSH / headless friendly)
#
#  One command for day-to-day bot management:
#
#      sudo /opt/meshtech-bot/manage.sh              # interactive menu
#      sudo /opt/meshtech-bot/manage.sh update       # the one update command
#
#  You can also run this panel from your ~/meshtech-bot clone - it always
#  manages the RUNTIME (/opt/meshtech-bot), never the clone itself.
#
#  Plain ASCII boxes only, so it renders the same over any SSH client.
#  If whiptail is installed (most Debian/Ubuntu/Raspberry Pi systems) and a
#  real terminal is attached, arrow-key dialog boxes are used instead -
#  automatically, with zero configuration and zero dependencies added.
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="meshtech-bot"
SERVICE_USER="meshtech"

# The RUNTIME this panel manages - the folder where the bot actually runs
# (its own folder when that is the runtime, otherwise the standard install
# location).  In the two-location layout the panel may live in the user's
# git clone (~/meshtech-bot); a clone is NOT the runtime - it has .git and
# its config/data are not the live ones.  The runtime is wherever bot.py
# sits without a .git next to it (older direct installs kept .git in /opt;
# they are handled too).
INSTALL_ROOT="$DIR"
if [[ -d "$DIR/.git" || ! -f "$DIR/bot.py" ]]; then
  if [[ -f /opt/meshtech-bot/bot.py ]]; then
    INSTALL_ROOT=/opt/meshtech-bot
  fi
fi

# Pick the Python that has the bot's libraries: the installation's venv
# first, then plain system python.
if [[ -x "$INSTALL_ROOT/.venv/bin/python" ]]; then
  PY="$INSTALL_ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

log()  { printf '\033[1;32m[manage]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[manage]\033[0m WARNING: %s\n' "$*"; }

# --- whiptail detection --------------------------------------------------------
# Dialog boxes only when the binary exists AND stdin/stdout are a terminal.
# Anything else (minimal server, Docker exec, piped output) falls back to the
# plain prompts below, which work everywhere.
WT=""
if command -v whiptail >/dev/null 2>&1 && [[ -t 0 && -t 1 ]]; then
  WT=1
fi

confirm() {  # confirm PROMPT DEFAULT(y|n) -> exit 0 on yes
  if [[ -n "$WT" ]]; then
    local args=(--yes-button Yes --no-button No)
    [[ "$2" == "n" ]] && args+=(--defaultno)
    whiptail --title " MeshTech-Bot " "${args[@]}" --yesno "$1" 0 0
  else
    ask_yes_no "$1" "$2"
  fi
}

paused() { read -r -p "  Press Enter to return to the menu..." _; }

# --- status line -------------------------------------------------------------
status_line() {
  local state="(not installed)"
  if [[ -f /etc/systemd/system/$SERVICE.service ]]; then
    if systemctl is-active --quiet "$SERVICE.service"; then
      state="running"
    else
      state="stopped"
    fi
  fi
  printf '%s' "$state"
}

version_line() {
  # Pull __version__ without importing the world: the assignment is a plain
  # literal near the top of the file, so grep it out.
  local v=""
  v="$(grep -oE '__version__ = "[^"]+"' "$INSTALL_ROOT/core/version.py" 2>/dev/null \
    | head -1 | cut -d'"' -f2)"
  echo "${v:-unknown}"
}

backup_data() {
  local dest="/var/backups/meshtech-bot"
  local file="$dest/meshtech-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  mkdir -p "$dest"
  tar -czf "$file" -C "$INSTALL_ROOT" data config.yaml 2>/dev/null \
    || { warn "backup failed"; return 1; }
  chmod 600 "$file"
  log "Backup written to $file"
  echo "$file"
}

show_header() {
  clear 2>/dev/null || true
  echo "=============================================================================="
  echo "   MeshTech-Bot control panel          status: $(status_line)   v$(version_line)"
  echo "=============================================================================="
  echo "     1) Configure the bot        (repeater IP/port, channels, admins, hops)"
  echo "     2) Update the bot software  (git pull + dependency refresh)"
  echo "     3) Uninstall                (asks to back up your data first)"
  echo "     4) Restart the service"
  echo "     5) View live logs           (journalctl -f, Ctrl-C to stop)"
  echo "     q) Quit"
  echo "------------------------------------------------------------------------------"
}

# --- 1) configure -------------------------------------------------------------
do_configure() {
  # Option 1 edits the RUNTIME's config - never the clone's (the clone has
  # no live config; seeding one there would be misleading).  When the
  # runtime has no config yet, the example is the starting point.
  if [[ ! -f "$INSTALL_ROOT/bot.py" ]]; then
    warn "No bot installation found (looked at $DIR and /opt/meshtech-bot)."
    warn "Install first:  cd ~/meshtech-bot && sudo ./install.sh"
    return 1
  fi
  if [[ "$DIR" != "$INSTALL_ROOT" ]]; then
    # panel running from a clone/trial copy - say plainly what will change
    if ! confirm "Edit the LIVE config at $INSTALL_ROOT/config.yaml?" y; then
      return 0
    fi
  fi
  if [[ ! -f "$INSTALL_ROOT/config.yaml" ]]; then
    local source=""
    if [[ -f "$INSTALL_ROOT/config.example.yaml" ]]; then
      source="$INSTALL_ROOT/config.example.yaml"
      warn "No config.yaml yet - copying the example first."
    elif [[ -f "$DIR/config.example.yaml" ]]; then
      source="$DIR/config.example.yaml"
      warn "No config.yaml yet - copying the example from this folder."
    else
      warn "No config.yaml or config.example.yaml found - nothing to edit."
      return 1
    fi
    cp "$source" "$INSTALL_ROOT/config.yaml"
  fi
  # The service account owns config.yaml (mode 640); run the editor as root
  # here (sudo manage.sh) so saving always works, then hand the file back.
  # A copy of the panel running outside the repo (a /tmp trial) borrows the
  # installed editor if its own is missing. "|| rc=$?" keeps an aborted
  # editor from killing the whole menu (set -e).
  local editor="$INSTALL_ROOT/scripts/configure_bot.py"
  if [[ ! -f "$editor" && -f "$DIR/scripts/configure_bot.py" ]]; then
    editor="$DIR/scripts/configure_bot.py"
  fi
  local rc=0
  "$PY" "$editor" "$INSTALL_ROOT/config.yaml" || rc=$?
  if [[ $rc -eq 0 ]]; then
    chown "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_ROOT/config.yaml" 2>/dev/null || true
    chmod 640 "$INSTALL_ROOT/config.yaml"
    echo
    if confirm "  Restart the service now to apply immediately?" y; then
      systemctl restart "$SERVICE.service" && log "Service restarted."
    fi
  fi
  return 0
}

# --- 2) update ----------------------------------------------------------------
# The one update command for everyone. Delegates to deploy.sh, which:
#   - uses (or creates) the invoking user's clone at ~/meshtech-bot, where
#     git runs as the user, never root
#   - applies the new code to the runtime: config, data and .venv untouched
#   - validates the live config, then restarts the service
# Fallback: a runtime that is itself a git checkout (old direct installs)
# is updated by pulling in place.
do_update() {
  local runtime="$INSTALL_ROOT"
  local deploy="$runtime/deploy.sh"
  # the clone belongs to the person at the keyboard (SUDO_USER when the
  # panel runs under sudo, otherwise the current user)
  local invoker="${SUDO_USER:-$(id -un)}"
  local uh=""
  if command -v getent &>/dev/null; then
    uh="$(getent passwd "$invoker" 2>/dev/null | cut -d: -f6)"
  fi
  [[ -z "$uh" && -d "/home/$invoker" ]] && uh="/home/$invoker"
  local clone="${uh:-$HOME}/meshtech-bot"
  if [[ -f "$deploy" ]]; then
    log "Updating: pulling into $clone (as $invoker), applying to $runtime."
    if "$deploy" --clone "$clone" --runtime "$runtime" --user "$SERVICE_USER" \
        --service "$SERVICE"; then
      log "Update complete: now running v$(version_line)."
    else
      warn "update failed - nothing was changed. Check the messages above."
    fi
    return 0
  fi
  if ! git -C "$runtime" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    warn "No update path found: no deploy.sh and no git checkout at $runtime."
    return 1
  fi
  log "Updating the git checkout at $runtime in place ..."
  if ! git -C "$runtime" pull --ff-only; then
    warn "git pull failed (local changes? offline?) - nothing was updated."
    return 1
  fi
  log "Refreshing Python dependencies..."
  "$PY" -m pip install -q -r "$runtime/requirements.txt" || warn "pip install had warnings (continuing)"
  log "Validating the new version..."
  # Validate from the INSTALLATION directory: bot.py resolves data/ (password
  # file, database) relative to the current directory, so running it from
  # elsewhere would check a throwaway state instead of the live one.
  (cd "$runtime" && "$PY" bot.py --check --config "$runtime/config.yaml") \
    || { warn "validation failed - check the output above"; return 1; }
  log "Restarting the service..."
  if systemctl restart "$SERVICE.service"; then
    log "Update complete: now running v$(version_line)."
  else
    warn "restart failed - check: systemctl status $SERVICE"
  fi
}

# --- 3) uninstall ---------------------------------------------------------------
do_uninstall() {
  echo
  echo "  This stops and disables the service, removes the systemd unit and"
  echo "  the 'meshtech' service account. Your files stay in $INSTALL_ROOT."
  echo
  if confirm "  Back up your data first (config + database + captures)?" y; then
    backup_data || warn "Continuing WITHOUT a backup."
  fi
  if ! confirm "  Really uninstall the service? This cannot be undone." n; then
    log "Uninstall cancelled."
    return 0
  fi
  log "Stopping and disabling $SERVICE..."
  systemctl disable --now "$SERVICE.service" 2>/dev/null || true
  rm -f /etc/systemd/system/"$SERVICE.service"
  systemctl daemon-reload
  if id "$SERVICE_USER" &>/dev/null; then
    log "Removing service account '$SERVICE_USER' (files kept)."
    userdel "$SERVICE_USER" 2>/dev/null || true
  fi
  echo
  if confirm "  ALSO delete ALL files in $INSTALL_ROOT (config, database, everything)?" n; then
    if confirm "  Final check - ALL bot files will be PERMANENTLY deleted. Continue?" n; then
      cd /
      rm -rf "$INSTALL_ROOT"
      log "All files removed. Goodbye."
      exit 0
    fi
  fi
  log "Uninstalled. The folder $INSTALL_ROOT was kept."
}

ask_yes_no() {  # prompt, default(y|n)
  local hint="Y/n"; [[ "$2" == "n" ]] && hint="y/N"
  local raw
  read -r -p "$1" raw
  raw="${raw,,}"
  if [[ -z "$raw" ]]; then [[ "$2" == "n" ]] && return 1 || return 0; fi
  [[ "$raw" == "y" || "$raw" == "yes" ]]
}

# --- direct subcommands (no menu): manage.sh update|configure|restart|logs ----
if [[ $# -gt 0 ]]; then
  case "$1" in
    update)
      [[ "$(id -u)" -eq 0 ]] || { warn "update needs root - run:  sudo ./manage.sh update"; exit 1; }
      do_update ;;
    configure)
      [[ "$(id -u)" -eq 0 ]] || { warn "configure needs root - run:  sudo ./manage.sh configure"; exit 1; }
      do_configure ;;
    restart)
      [[ "$(id -u)" -eq 0 ]] || { warn "restart needs root - run:  sudo ./manage.sh restart"; exit 1; }
      systemctl restart "$SERVICE.service" && log "Service restarted." ;;
    logs)
      journalctl -u "$SERVICE.service" -f --no-pager ;;
    -h|--help)
      echo "Usage: sudo ./manage.sh [update|configure|restart|logs]"
      echo "  With no arguments, opens the interactive menu."
      echo "    update    pull the latest code and apply it (the one update command)"
      echo "    configure edit the live config interactively"
      echo "    restart   restart the service"
      echo "    logs      follow the live log (Ctrl-C to stop)" ;;
    *)
      warn "unknown subcommand: $1 (try: sudo ./manage.sh help)"; exit 2 ;;
  esac
  exit 0
fi

choose_option() {  # sets CHOICE; Esc/Cancel on the dialog means Quit
  if [[ -n "$WT" ]]; then
    CHOICE="$(whiptail --title " MeshTech-Bot control panel " \
      --ok-button Select --cancel-button Quit \
      --menu "status: $(status_line)   v$(version_line)" 0 0 0 \
        "1" "Configure the bot (repeater, channels, admins, hops)" \
        "2" "Update the bot software (pull + apply, the one update command)" \
        "3" "Uninstall (asks to back up your data first)" \
        "4" "Restart the service" \
        "5" "View live logs (Ctrl-C stops watching)" \
        "q" "Quit" 3>&1 1>&2 2>&3)" || CHOICE="q"
  else
    show_header
    read -r -p "  Choose an option: " CHOICE
  fi
}

# --- main loop -------------------------------------------------------------------
if [[ "$(id -u)" -ne 0 ]]; then
  warn "Most options need root - run with:  sudo ./manage.sh"
  confirm "  Continue anyway (view only)?" n || exit 1
fi

while true; do
  choose_option
  echo
  case "$CHOICE" in
    1) do_configure; paused ;;
    2) do_update;    paused ;;
    3) do_uninstall; paused ;;
    4) if systemctl restart "$SERVICE.service"; then
         log "Service restarted."
       else
         warn "restart failed - is the service installed?"
       fi
       paused ;;
    5) echo "  Live logs - press Ctrl-C to stop, then you return here."
       trap ':' INT   # keep the menu alive when Ctrl-C stops journalctl
       journalctl -u "$SERVICE.service" -f --no-pager || true
       trap - INT
       paused ;;
    q|Q) echo "  73!"; exit 0 ;;
    *) echo "  Unknown option: $CHOICE" ; sleep 1 ;;
  esac
done
