#!/bin/bash
# ==============================================================================
# 🌸 STACKSSTACKS INSTALLER
# Installs the stacks CLI from github.com/crunkazcanbe/stacks
# Usage: curl -fsSL https://raw.githubusercontent.com/crunkazcanbe/stacks/master/install.sh | bash
# ==============================================================================

REPO="https://raw.githubusercontent.com/crunkazcanbe/stacks/master"
BIN_DIR="/usr/local/bin"
LIB_DIR="/usr/local/lib"

GREEN='\033[38;5;120m'
CYAN='\033[38;5;87m'
YELLOW='\033[38;5;221m'
RED='\033[38;5;203m'
RESET='\033[0m'
BOLD='\033[1m'

ok()   { echo -e "  ${GREEN}✔${RESET}  $1"; }
bad()  { echo -e "  ${RED}✖${RESET}  $1"; exit 1; }
info() { echo -e "  ${CYAN}▸${RESET}  $1"; }
ask()  { echo -e "\n  ${YELLOW}?${RESET}  ${BOLD}$1${RESET}"; }

echo -e "\n${CYAN}"
echo "  ____  _____  _    ____ _  _______ "
echo " / ___||_   _|/ \  / ___| |/ /  ___|"
echo " \___ \  | | / _ \| |   | \' /|___ \\"
echo "  ___) | | |/ ___ \ |___| . \ ___) |"
echo " |____/  |_/_/   \_\____|_|\_\____/ "
echo -e "${RESET}"
echo -e "  ${BOLD}StacksStacks Installer${RESET} — github.com/crunkazcanbe/stacks\n"

# Check dependencies
for dep in docker curl python3; do
    if ! command -v $dep &>/dev/null; then
        bad "$dep is required but not installed."
    fi
    ok "$dep found"
done

# Ask config questions
ask "Where are your Docker compose stacks? (default: /home/$USER/MyDocker/Stacks)"
read -r STACKS_DIR
STACKS_DIR="${STACKS_DIR:-/home/$USER/MyDocker/Stacks}"

ask "Where are your Traefik dynamic configs? (default: /home/$USER/MyDocker/Configs/Dynamics)"
read -r DYNAMICS_DIR
DYNAMICS_DIR="${DYNAMICS_DIR:-/home/$USER/MyDocker/Configs/Dynamics}"

ask "Your primary domain? (e.g. example.com)"
read -r DOMAIN
DOMAIN="${DOMAIN:-example.com}"

ask "Your server username? (default: $USER)"
read -r SERVER_USER
SERVER_USER="${SERVER_USER:-$USER}"

echo ""
info "Installing to $BIN_DIR and $LIB_DIR..."

# Create dirs
mkdir -p "$STACKS_DIR" "$DYNAMICS_DIR" 2>/dev/null || true

# Download main script
curl -fsSL "$REPO/bin/stacks" -o /tmp/stacks_install || bad "Failed to download stacks"

# Substitute config values
sed -i "s|/srv/stacks/Stacks|$STACKS_DIR|g" /tmp/stacks_install
sed -i "s|/srv/stacks/Configs/Dynamics|$DYNAMICS_DIR|g" /tmp/stacks_install
sed -i "s|user\.com|$DOMAIN|g" /tmp/stacks_install
sed -i "s|stacks|$SERVER_USER|g" /tmp/stacks_install
sed -i "s|user|$USER|g" /tmp/stacks_install

sudo mv /tmp/stacks_install "$BIN_DIR/stacks"
sudo chmod +x "$BIN_DIR/stacks"
ok "stacks installed to $BIN_DIR/stacks"

# Download lib files
for lib in stacks_build.py stacks_fix.py stacks_inject.py stacks_search.py stacks_describe.py stacks_gen_srvs.py stacks_network_guardian.py; do
    curl -fsSL "$REPO/lib/$lib" -o /tmp/$lib || bad "Failed to download $lib"
    sed -i "s|/srv/stacks/Stacks|$STACKS_DIR|g" /tmp/$lib
    sed -i "s|user|$USER|g" /tmp/$lib
    sudo mv /tmp/$lib "$LIB_DIR/$lib"
    sudo chmod +x "$LIB_DIR/$lib"
    ok "$lib installed"
done

# Create default config
CONFIG_DIR="/home/$USER/.config/stacks"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/stacks.conf" ]; then
    cat > "$CONFIG_DIR/stacks.conf" << EOF
# stacks.conf — StacksStacks configuration
STACKS_DIR="$STACKS_DIR"
DYNAMICS_DIR="$DYNAMICS_DIR"
DOMAIN="$DOMAIN"
EOF
    ok "Config created at $CONFIG_DIR/stacks.conf"
fi

echo -e "\n  ${GREEN}${BOLD}✨ Installation complete!${RESET}\n"
echo -e "  Run ${CYAN}stacks help${RESET} to get started.\n"

# ── Create all config files ───────────────────────────────────────────────────
CONF_DIR="/home/$USER/.config/stacks"
/usr/bin/mkdir -p "$CONF_DIR/descriptions"

# backup.conf
if [ ! -f "$CONF_DIR/backup.conf" ]; then
cat > "$CONF_DIR/backup.conf" << EOF
BACKUP_DEST="/home/$USER/Backup"
FILES=(
  "/usr/local/bin/stacks"
  "/usr/local/lib/stacks_build.py"
  "/usr/local/lib/stacks_fix.py"
  "/usr/local/lib/stacks_inject.py"
  "/usr/local/lib/stacks_search.py"
  "/home/$USER/.zshrc"
  "/home/$USER/.bashrc"
)
FOLDERS=(
  "$STACKS_DIR"
  "$DYNAMICS_DIR"
  "$CONF_DIR"
)
EOF
ok "backup.conf created"
fi

# stacks.conf additions
cat >> "$CONF_DIR/stacks.conf" << EOF
SABLIER_SCALE_ENABLED=1
DELAY=2
EOF

ok "All configs created at $CONF_DIR"
