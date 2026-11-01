#!/usr/bin/env bash
# netmon installer for Raspberry Pi OS (64-bit, Bookworm or Bullseye).
#
# Idempotent: safe to re-run. Does four things -
#   1. apt packages (tkinter, optionally iperf3)
#   2. a virtualenv with --system-site-packages, so the pip deps and the
#      apt-installed tkinter can see each other
#   3. the sysctl that lets ICMP echo work without root
#   4. an autostart entry so the dashboard comes up with the desktop
#
# Usage:  bash scripts/install.sh [--no-autostart] [--with-iperf3]
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
AUTOSTART=1
WITH_IPERF3=0

for arg in "$@"; do
    case "$arg" in
        --no-autostart) AUTOSTART=0 ;;
        --with-iperf3)  WITH_IPERF3=1 ;;
        -h|--help)      sed -n '2,16p' "$0"; exit 0 ;;
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
