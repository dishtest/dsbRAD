#!/usr/bin/env bash
# =============================================================================
# Remote TV-Control Site Installer  (Lubuntu / Ubuntu-based, amd64)
# -----------------------------------------------------------------------------
# Intended usage (one-liner behind a bit.ly-style short link):
#
#     curl -fsSL https://bit.ly/YOUR-SHORT-LINK | sudo bash
#
# ...where the short link resolves to the raw GitHub URL of THIS file, e.g.
#     https://raw.githubusercontent.com/YOURUSER/YOURREPO/main/install.sh
#
# Installs: guvcview, v4l-utils, firefox, tailscale, NoMachine, and the
# PiBeam Universal Remote stack (via pibeam_install.sh from the same repo).
# Creates desktop shortcuts + a post-setup notes file, then offers a reboot.
# =============================================================================
set -u

# ------------------------- EDIT THESE BEFORE PUBLISHING ----------------------
RAW_BASE="https://raw.githubusercontent.com/YOURUSER/YOURREPO/main"
# NoMachine pinned fallback (verify current version at nomachine.com/download)
NOMACHINE_URL="https://download.nomachine.com/download/9.3/Linux/nomachine_9.3.7_1_amd64.deb"
# -----------------------------------------------------------------------------

log()  { echo -e "\e[1;32m[install]\e[0m $*"; }
warn() { echo -e "\e[1;33m[warn]\e[0m $*"; }
fail() { echo -e "\e[1;31m[FAIL]\e[0m $*"; FAILURES+=("$*"); }
FAILURES=()

# ---- must be root; find the real desktop user ----
if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo:  curl -fsSL <link> | sudo bash"
    exit 1
fi
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
    warn "Could not identify the desktop user; shortcuts will go to /root."
    TARGET_USER="root"
fi
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
DESKTOP_DIR="$USER_HOME/Desktop"
mkdir -p "$DESKTOP_DIR"

export DEBIAN_FRONTEND=noninteractive

# =============================================================================
# 1. Base apt packages
# =============================================================================
log "Updating package lists..."
apt-get update -y || fail "apt update failed"

log "Installing guvcview, v4l-utils, firefox, curl..."
apt-get install -y guvcview v4l-utils firefox curl wget \
    || fail "base apt packages"

# =============================================================================
# 2. Tailscale (official installer, adds their repo)
# =============================================================================
log "Installing Tailscale..."
if curl -fsSL https://tailscale.com/install.sh | sh; then
    log "Tailscale installed."
else
    fail "tailscale install"
fi

# =============================================================================
# 3. NoMachine (.deb direct download; no apt repo exists)
# =============================================================================
log "Installing NoMachine..."
NM_DEB="/tmp/nomachine_amd64.deb"
# Try to auto-detect the latest amd64 .deb link from their download page,
# fall back to the pinned URL above.
DETECTED=$(curl -fsSL "https://downloads.nomachine.com/download/?id=1" 2>/dev/null \
    | grep -oE 'https://[^"]*nomachine_[0-9._]+_amd64\.deb' | head -1 || true)
NM_URL="${DETECTED:-$NOMACHINE_URL}"
log "NoMachine package: $NM_URL"
if wget -qO "$NM_DEB" "$NM_URL" && dpkg -i "$NM_DEB"; then
    apt-get install -f -y   # sweep up any missing deps
    log "NoMachine installed."
else
    fail "NoMachine install (check NOMACHINE_URL version pin)"
fi
rm -f "$NM_DEB"

# =============================================================================
# 4. PiBeam Universal Remote stack (dedicated script from same repo)
# =============================================================================
log "Installing PiBeam Universal Remote stack..."
if curl -fsSL "$RAW_BASE/pibeam_install.sh" | bash -s -- "$RAW_BASE" "$TARGET_USER"; then
    log "PiBeam stack installed."
else
    fail "PiBeam stack install"
fi

# =============================================================================
# 5. Desktop shortcuts
# =============================================================================
log "Creating desktop shortcuts..."

make_shortcut () {  # $1 filename  $2 Name  $3 Exec  $4 Icon
    cat > "$DESKTOP_DIR/$1" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Exec=$3
Icon=$4
Terminal=false
EOF
    chmod +x "$DESKTOP_DIR/$1"
}

# Firefox: prefer copying the system entry so the icon/exec are always right
if [[ -f /usr/share/applications/firefox.desktop ]]; then
    cp /usr/share/applications/firefox.desktop "$DESKTOP_DIR/" && \
        chmod +x "$DESKTOP_DIR/firefox.desktop"
elif [[ -f /var/lib/snapd/desktop/applications/firefox_firefox.desktop ]]; then
    cp /var/lib/snapd/desktop/applications/firefox_firefox.desktop \
        "$DESKTOP_DIR/firefox.desktop" && chmod +x "$DESKTOP_DIR/firefox.desktop"
else
    make_shortcut "firefox.desktop" "Firefox" "firefox" "firefox"
fi

# NoMachine: its installer ships a desktop entry
NM_ENTRY=$(ls /usr/share/applications/*[Nn]o[Mm]achine*.desktop 2>/dev/null | head -1)
if [[ -n "$NM_ENTRY" ]]; then
    cp "$NM_ENTRY" "$DESKTOP_DIR/" && chmod +x "$DESKTOP_DIR/$(basename "$NM_ENTRY")"
else
    make_shortcut "nomachine.desktop" "NoMachine" "/usr/NX/bin/nxplayer" \
        "/usr/NX/share/icons/nomachine.png"
fi

make_shortcut "view-cam.desktop" "View Cam" "guvcview" "guvcview"
# "Universal Remote" shortcut is created by pibeam_install.sh

chown -R "$TARGET_USER:$TARGET_USER" "$DESKTOP_DIR"

# =============================================================================
# 6. Post-setup notes file
# =============================================================================
log "Fetching post-setup notes..."
if ! curl -fsSL "$RAW_BASE/setup_notes.txt" -o "$DESKTOP_DIR/SETUP-NOTES.txt"; then
    warn "Could not fetch setup_notes.txt from repo; writing minimal notes."
    cat > "$DESKTOP_DIR/SETUP-NOTES.txt" <<'EOF'
Post-install setup:
 1. Tailscale:  open a terminal, run  sudo tailscale up  and follow the
    login URL to join your tailnet.
 2. NoMachine:  runs as a service automatically; connect from your client
    using this machine's Tailscale IP, port 4000.
 3. PiBeam:     plug in the PiBeam and launch "Universal Remote".
EOF
fi
chown "$TARGET_USER:$TARGET_USER" "$DESKTOP_DIR/SETUP-NOTES.txt"

# =============================================================================
# 7. Summary + optional reboot
# =============================================================================
echo
if [[ ${#FAILURES[@]} -eq 0 ]]; then
    log "All components installed successfully."
else
    warn "Completed with ${#FAILURES[@]} problem(s):"
    for f in "${FAILURES[@]}"; do echo "   - $f"; done
    warn "See above output for details."
fi

# stdin belongs to the curl pipe, so prompt via the terminal directly
if [[ -e /dev/tty ]]; then
    read -r -p "Reboot now? (y/n): " ANSWER < /dev/tty
    if [[ "$ANSWER" =~ ^[Yy]$ ]]; then
        log "Rebooting..."
        reboot
    else
        log "Reboot skipped. A reboot is recommended before first use."
    fi
else
    warn "No terminal available for prompt; skipping reboot question."
fi
