#!/usr/bin/env bash
# Launch the netmon dashboard inside a graphical session.
#
# Called by the autostart entry (or the systemd unit). It disables screen
# blanking first - a monitor that goes dark after 10 minutes is not a monitor.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Keep the panel awake. Does nothing on a Wayland desktop; the installer handles
# that case through raspi-config. Harmless if X lacks the relevant extensions.
if command -v xset >/dev/null 2>&1; then
    xset s off        || true
    xset s noblank    || true
    xset -dpms        || true
fi

# Hide the pointer if unclutter is installed (Tk also hides it, this covers
# the gap before the window maps).
if command -v unclutter >/dev/null 2>&1; then
    unclutter -idle 1 -root >/dev/null 2>&1 &
fi

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi

exec "$PYTHON" -m netmon "$@"
