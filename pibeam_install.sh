#!/usr/bin/env bash
# =============================================================================
# PiBeam Universal Remote - stack installer
# -----------------------------------------------------------------------------
# Called by install.sh as:
#     curl -fsSL $RAW_BASE/pibeam_install.sh | bash -s -- <RAW_BASE> <USER>
# or run standalone:
#     sudo bash pibeam_install.sh https://raw.githubusercontent.com/U/R/main youruser
#
# Installs host dependencies, fetches the app into /opt/pibeam, adds the
# user to the dialout group (serial access), creates the "Universal Remote"
# desktop shortcut, and - if a MicroPython-flashed PiBeam is plugged in -
# pushes the firmware (main.py) onto the device automatically.
#
# NOTE: the one-time MicroPython UF2 flash (hold BOOT while plugging in)
# must still be done by hand; only the main.py deployment is automated.
# =============================================================================
set -u

RAW_BASE="${1:?usage: pibeam_install.sh RAW_BASE TARGET_USER}"
TARGET_USER="${2:?usage: pibeam_install.sh RAW_BASE TARGET_USER}"
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
DESKTOP_DIR="$USER_HOME/Desktop"
APP_DIR="/opt/pibeam"

log()  { echo -e "\e[1;36m[pibeam]\e[0m $*"; }

if [[ $EUID -ne 0 ]]; then
    echo "pibeam_install.sh must run as root"; exit 1
fi

export DEBIAN_FRONTEND=noninteractive

log "Installing Python dependencies..."
apt-get install -y python3 python3-tk python3-pil python3-pil.imagetk \
    python3-serial python3-pip
# mpremote (firmware deployment tool) - not in Ubuntu's repos, so via pip
pip3 install --break-system-packages -q mpremote || \
    pip3 install -q mpremote || \
    echo "[pibeam] WARN: mpremote install failed; firmware auto-deploy will be skipped"

log "Fetching application files into $APP_DIR ..."
mkdir -p "$APP_DIR"
for f in pibeam_remote.py firmware_main.py README.md; do
    curl -fsSL "$RAW_BASE/$f" -o "$APP_DIR/$f" || {
        echo "[pibeam] FAILED fetching $f from $RAW_BASE"; exit 1; }
done
chmod +x "$APP_DIR/pibeam_remote.py"

log "Adding $TARGET_USER to dialout group (serial port access)..."
usermod -aG dialout "$TARGET_USER"

log "Creating 'Universal Remote' shortcut..."
ENTRY="[Desktop Entry]
Type=Application
Name=Universal Remote
Comment=PiBeam IR learning remote
Exec=python3 $APP_DIR/pibeam_remote.py
Icon=input-gaming
Terminal=false
Categories=Utility;"
mkdir -p "$DESKTOP_DIR"
echo "$ENTRY" > /usr/share/applications/universal-remote.desktop
echo "$ENTRY" > "$DESKTOP_DIR/universal-remote.desktop"
chmod +x "$DESKTOP_DIR/universal-remote.desktop"
chown "$TARGET_USER:$TARGET_USER" "$DESKTOP_DIR/universal-remote.desktop"

# =============================================================================
# Firmware auto-deploy (best effort - skipped gracefully if no device found)
# =============================================================================
log "Checking for a plugged-in MicroPython PiBeam to deploy firmware..."
if ! command -v mpremote >/dev/null 2>&1; then
    log "mpremote not available; skipping firmware deploy."
    log "  Manual fallback:  mpremote cp $APP_DIR/firmware_main.py :main.py"
else
    # RP2040 MicroPython CDC enumerates with USB vendor ID 2e8a
    PIBEAM_PORT=""
    for dev in /dev/ttyACM*; do
        [[ -e "$dev" ]] || continue
        VID=$(udevadm info -q property -n "$dev" 2>/dev/null \
              | grep -i '^ID_USB_VENDOR_ID=' | cut -d= -f2)
        if [[ "${VID,,}" == "2e8a" ]]; then
            PIBEAM_PORT="$dev"
            break
        fi
    done

    if [[ -z "$PIBEAM_PORT" ]]; then
        log "No PiBeam detected on USB; skipping firmware deploy."
        log "  (Plug it in and re-run this script, or deploy manually with:"
        log "   mpremote cp $APP_DIR/firmware_main.py :main.py)"
    else
        log "PiBeam found on $PIBEAM_PORT - deploying main.py..."
        # Interrupt any running program first so the copy can proceed,
        # then push the firmware and reset the board to start it.
        if mpremote connect "$PIBEAM_PORT" exec "print('ok')" >/dev/null 2>&1 \
           && mpremote connect "$PIBEAM_PORT" \
                cp "$APP_DIR/firmware_main.py" :main.py >/dev/null 2>&1 \
           && mpremote connect "$PIBEAM_PORT" reset >/dev/null 2>&1; then
            log "Firmware deployed and device restarted."
        else
            log "WARN: firmware deploy failed. Common causes:"
            log "  - Device not flashed with MicroPython yet (do the UF2 step)"
            log "  - Another program (Thonny, the remote app) holds the port"
            log "  Manual fallback:  mpremote cp $APP_DIR/firmware_main.py :main.py"
        fi
    fi
fi

log "PiBeam stack done. (Group change takes effect after reboot/re-login.)"
