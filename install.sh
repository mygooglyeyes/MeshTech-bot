#!/usr/bin/env bash
# =============================================================================
#  MeshTech-Bot - native Linux installer (Debian / Ubuntu / Raspberry Pi OS)
#
#  What this does:
#    1. creates a dedicated, unprivileged system account (default: meshtech)
#    2. installs the Python dependencies  (own venv by default, or your
#       system Python with --no-venv)
#    3. creates config.yaml from config.example.yaml (if missing) and fixes
#       file/group ownership so ONLY the bot's account can touch them
#    4. registers a systemd service that starts the bot at every boot
#       and restarts it if it crashes
#
#  Usage (run from the cloned folder):
#      sudo ./install.sh                       # venv install (recommended)
#      sudo ./install.sh --no-venv             # use system python3 + pip
#      sudo ./install.sh --dir /opt/meshtech-bot --user meshtech
#      sudo ./install.sh --uninstall           # stop service, remove user & unit
#
#  You only need to run it once. Re-running it is safe (idempotent) and is
#  how you pick up new options after a config change.
# =============================================================================
set -euo pipefail

# --- defaults -----------------------------------------------------------------
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="meshtech"
SERVICE_GROUP="meshtech"
USE_VENV=1
DO_UNINSTALL=0

# --- arguments ------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)      INSTALL_DIR="$2"; shift 2 ;;
    --user)     SERVICE_USER="$2"; SERVICE_GROUP="$2"; shift 2 ;;
    --no-venv)  USE_VENV=0; shift ;;
    --venv)     USE_VENV=1; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $1  (see header of install.sh)"; exit 2 ;;
  esac
done

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m WARNING: %s\n' "$*"; }
die()  { printf '\033[1;31m[install]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

# --- root check --------------------------------------------------------------------
if [[ "$(id -u)" -ne 0 ]]; then
  die "Please run with sudo:  sudo ./install.sh"
fi

# --- uninstall ----------------------------------------------------------------------
if [[ "$DO_UNINSTALL" -eq 1 ]]; then
  log "Stopping and disabling the service..."
  systemctl disable --now meshtech-bot.service 2>/dev/null || true
  rm -f /etc/systemd/system/meshtech-bot.service
  systemctl daemon-reload
  if id "$SERVICE_USER" &>/dev/null; then
    log "Removing service account '$SERVICE_USER' (files in $INSTALL_DIR are kept)."
    userdel "$SERVICE_USER" 2>/dev/null || true
  fi
  log "Done. The install folder ($INSTALL_DIR) and its data were left in place."
  exit 0
fi

# --- install location ----------------------------------------------------------------
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
  if [[ ! -d "$INSTALL_DIR" ]]; then
    log "Copying project into $INSTALL_DIR ..."
    mkdir -p "$INSTALL_DIR"
    # copy everything except git metadata and local config/runtime data
    (cd "$SRC_DIR" && tar cf - --exclude='./.git' --exclude='./.venv' \
         --exclude='./venv' --exclude='./config.yaml' --exclude='./data/*' \
         --exclude='./.freebuff' .) | (cd "$INSTALL_DIR" && tar xf -)
    mkdir -p "$INSTALL_DIR/data"
  fi
fi
cd "$INSTALL_DIR"

# --- detect the platform --------------------------------------------------------------
if ! command -v apt-get &>/dev/null; then
  warn "This installer targets Debian/Ubuntu/Raspberry Pi OS. For other distros"
  warn "install python3, python3-venv and git manually, then run the steps below"
  warn "as shown in docs/INSTALL.md (or use the Docker option)."
fi
command -v python3 &>/dev/null || die "python3 is not installed (try: sudo apt install python3 python3-venv)"
command -v git &>/dev/null || die "git is not installed (try: sudo apt install git)"

# --- dedicated service account ----------------------------------------------------------
if ! id "$SERVICE_USER" &>/dev/null; then
  log "Creating service account '$SERVICE_USER' (no login, no home)."
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null \
    || useradd --system --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if ! getent group "$SERVICE_GROUP" &>/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
usermod -aG "$SERVICE_GROUP" "$SERVICE_USER" 2>/dev/null || true

# --- Python dependencies ----------------------------------------------------------------
if [[ "$USE_VENV" -eq 1 ]]; then
  if [[ ! -x .venv/bin/python ]]; then
    log "Creating a virtual environment (.venv) ..."
    python3 -m venv .venv || die "python3-venv missing (try: sudo apt install python3-venv)"
  fi
  PYTHON="$INSTALL_DIR/.venv/bin/python"
  log "Installing Python packages into .venv ..."
  "$PYTHON" -m pip install --upgrade pip >/dev/null
  "$PYTHON" -m pip install -r requirements.txt
else
  PYTHON="$(command -v python3)"
  log "Installing Python packages with the SYSTEM python ($PYTHON) ..."
  if ! python3 -m pip --version &>/dev/null; then
    die "pip missing (try: sudo apt install python3-pip)"
  fi
  # Debian 12+ / Ubuntu 23+ refuse global pip installs (PEP 668) unless the
  # environment is marked externally-managed; --break-system-packages is the
  # documented way for a dedicated appliance box like this.
  python3 -m pip install -r requirements.txt 2>/dev/null || \
    python3 -m pip install --break-system-packages -r requirements.txt \
      || die "pip install failed (see docs/INSTALL.md, 'Production without venv')"
fi

# --- config -----------------------------------------------------------------------------
if [[ ! -f config.yaml ]]; then
  log "Creating config.yaml from config.example.yaml ..."
  cp config.example.yaml config.yaml
  warn "Edit config.yaml first! At minimum set connection.host, connection.port,"
  warn "your channels, admin_pubkey_prefixes and web.password. Then run:"
  warn "    sudo -u $SERVICE_USER '$PYTHON' bot.py --check"
else
  log "config.yaml already present - leaving it untouched."
fi

# --- permissions -------------------------------------------------------------------------
log "Setting ownership: $SERVICE_USER:$SERVICE_GROUP on $INSTALL_DIR"
mkdir -p data
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR" "$INSTALL_DIR/data"
chmod 640 config.yaml config.example.yaml 2>/dev/null || true
find "$INSTALL_DIR" -type d -exec chmod 750 {} \;
find "$INSTALL_DIR" -type f -exec chmod 640 {} \;
# the bot's Python + venv binaries still need to be executable
chmod 755 "$INSTALL_DIR/bot.py" "$INSTALL_DIR/install.sh" 2>/dev/null || true
if [[ -d .venv ]]; then
  find .venv -type d -exec chmod 755 {} \;
  find .venv -type f -name 'python*' -exec chmod 755 {} \; 2>/dev/null || true
  find .venv -type f \( -name '*.so' -o -perm -u+x \) -exec chmod 755 {} \; 2>/dev/null || true
fi

# --- validate before installing the service -----------------------------------------------
log "Validating config ..."
if ! sudo -u "$SERVICE_USER" "$PYTHON" bot.py --check; then
  die "Validation failed - fix config.yaml first (see the message above). Service NOT installed."
fi

# --- systemd unit --------------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/meshtech-bot.service"
log "Writing systemd unit $SERVICE_FILE"
sed -e "s|{INSTALL_DIR}|$INSTALL_DIR|g" \
    -e "s|{USER}|$SERVICE_USER|g" \
    -e "s|{GROUP}|$SERVICE_GROUP|g" \
    -e "s|{PYTHON}|$PYTHON|g" \
    deploy/meshtech-bot.service > "$SERVICE_FILE"
chmod 644 "$SERVICE_FILE"

log "Enabling the service (starts now and at every boot) ..."
systemctl daemon-reload
systemctl enable meshtech-bot.service
systemctl restart meshtech-bot.service

# --- done -----------------------------------------------------------------------------------
echo
echo "=============================================================================="
echo "  MeshTech-Bot is installed and running."
echo "=============================================================================="
echo "  Status :  systemctl status meshtech-bot"
echo "  Logs   :  journalctl -u meshtech-bot -f"
echo "  Config :  $INSTALL_DIR/config.yaml"
echo "  Data   :  $INSTALL_DIR/data/  (owned by $SERVICE_USER)"
echo
echo "  Next steps:"
echo "    1. Edit $INSTALL_DIR/config.yaml (host, port, channels, admin prefix,"
echo "       dashboard password) - the bot auto-reloads config changes."
echo "    2. Open the dashboard:  http://127.0.0.1:8081"
echo "    3. Install docs + troubleshooting: $INSTALL_DIR/docs/INSTALL.md"
echo "=============================================================================="
