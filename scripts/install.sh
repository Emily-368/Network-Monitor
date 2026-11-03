#!/usr/bin/env bash
# netmon installer for Raspberry Pi OS (Bullseye or later; needs Python 3.9+).
#
# Idempotent: safe to re-run. Does these things -
#   1. apt packages (tkinter, optionally iperf3)
#   2. a virtualenv with --system-site-packages, so the pip deps and the
#      apt-installed tkinter can see each other
#   3. the sysctl that lets ICMP echo work without root
#   4. an autostart entry so the dashboard comes up with the desktop
#   5. turns off screen blanking, for a panel that has to stay readable
#   6. with --headless-service: the collector service instead of the autostart
#   7. with --spi-display: X11 drawing on the SPI panel's framebuffer
#
# Usage:  bash scripts/install.sh [--no-autostart] [--with-iperf3] [--headless-service]
#                                 [--spi-display]
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
AUTOSTART=1
WITH_IPERF3=0
HEADLESS_SERVICE=0
SPI_DISPLAY=0

for arg in "$@"; do
    case "$arg" in
        --no-autostart) AUTOSTART=0 ;;
        --with-iperf3)  WITH_IPERF3=1 ;;
        # The service and the dashboard must not run together (both would collect).
        --headless-service) HEADLESS_SERVICE=1; AUTOSTART=0 ;;
        --spi-display)  SPI_DISPLAY=1 ;;
        -h|--help)      sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user (it calls sudo where needed)." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
say "apt packages"
APT_PKGS=(python3-tk python3-venv python3-pip)
[[ $WITH_IPERF3 -eq 1 ]] && APT_PKGS+=(iperf3)
[[ $SPI_DISPLAY -eq 1 ]] && APT_PKGS+=(xserver-xorg-video-fbdev)
sudo apt-get update -qq
sudo apt-get install -y "${APT_PKGS[@]}"

# --------------------------------------------------------------------------- #
say "python environment"
# --system-site-packages is required: tkinter cannot be installed from pip,
# so the venv has to be able to see the apt-provided one.
if [[ ! -d "$VENV" ]]; then
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
"$VENV/bin/python" -c "import tkinter" \
    || { echo "tkinter still missing - check that python3-tk installed" >&2; exit 1; }
echo "   venv ready at $VENV"

# --------------------------------------------------------------------------- #
say "unprivileged ICMP"
# Lets any process open an ICMP datagram socket, so netmon never needs root.
SYSCTL_FILE=/etc/sysctl.d/99-netmon-icmp.conf
if [[ ! -f "$SYSCTL_FILE" ]]; then
    echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee "$SYSCTL_FILE" >/dev/null
fi
sudo sysctl -q -p "$SYSCTL_FILE"
echo "   net.ipv4.ping_group_range = $(sysctl -n net.ipv4.ping_group_range)"

# --------------------------------------------------------------------------- #
say "history directory"
DB_DIR="$(cd "$PROJECT_DIR" && "$VENV/bin/python" -c \
    "import os; from netmon.config import load_config; print(os.path.dirname(load_config().db_file()))")"
mkdir -p "$DB_DIR"
echo "   $DB_DIR"

# --------------------------------------------------------------------------- #
chmod +x "$PROJECT_DIR/scripts/start-dashboard.sh"

if command -v raspi-config >/dev/null 2>&1; then
    say "screen blanking"
    # Covers Wayland desktops too, where the xset calls in start-dashboard.sh do nothing.
    sudo raspi-config nonint do_blanking 1 && echo "   off (takes effect after a reboot)" \
        || echo "   could not change it - see Display Options in raspi-config"
fi

# --------------------------------------------------------------------------- #
if [[ $SPI_DISPLAY -eq 1 ]]; then
    say "SPI display"
    # Wayland desktops need a DRM device, which an fbtft panel does not provide.
    if command -v raspi-config >/dev/null 2>&1; then
        sudo raspi-config nonint do_spi 0 || echo "   could not enable SPI"
        sudo raspi-config nonint do_wayland W1 || echo "   could not select X11"
    fi
    if [[ -e /dev/fb1 ]]; then
        # X draws straight on the panel, so no fbcp process has to copy the screen.
        sudo mkdir -p /etc/X11/xorg.conf.d
        sudo tee /etc/X11/xorg.conf.d/99-netmon-spi.conf >/dev/null <<'XORG'
Section "Device"
    Identifier "netmon SPI panel"
    Driver     "fbdev"
    Option     "fbdev" "/dev/fb1"
EndSection
XORG
        echo "   X will draw on /dev/fb1 after a reboot"
    else
        echo "   no /dev/fb1: load the panel's dtoverlay from its vendor, reboot, re-run."
        echo "   If the panel is a DRM display instead, X should find it without this."
    fi
fi

# --------------------------------------------------------------------------- #
if [[ $HEADLESS_SERVICE -eq 1 ]]; then
    say "collector service"
    # The unit is written for user pi in /home/pi/netmon; fit it to this install.
    sed -e "s|/home/pi/netmon|$PROJECT_DIR|g" \
        -e "s|/home/pi|$HOME|g" \
        -e "s|^User=pi|User=$(id -un)|" \
        -e "s|^Group=pi|Group=$(id -gn)|" \
        "$PROJECT_DIR/systemd/netmon-collector.service" \
        | sudo tee /etc/systemd/system/netmon-collector.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now netmon-collector
    echo "   enabled for $(id -un)"
fi

if [[ $AUTOSTART -eq 1 ]]; then
    say "autostart entry"
    AUTOSTART_DIR="$HOME/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    sed "s|__START__|$PROJECT_DIR/scripts/start-dashboard.sh|" \
        "$PROJECT_DIR/scripts/netmon.desktop.in" > "$AUTOSTART_DIR/netmon.desktop"
    echo "   $AUTOSTART_DIR/netmon.desktop"
    echo "   the dashboard will start with the desktop session"
fi

# --------------------------------------------------------------------------- #
say "self-test"
"$VENV/bin/python" -m netmon --selftest || true

cat <<EOF

Done. Next:

  start now          $PROJECT_DIR/scripts/start-dashboard.sh
  windowed (VNC)     $VENV/bin/python -m netmon --windowed
  no display         $VENV/bin/python -m netmon --headless
  history summary    $VENV/bin/python -m netmon --report

Edit $PROJECT_DIR/config.yaml to change targets and thresholds.
EOF
