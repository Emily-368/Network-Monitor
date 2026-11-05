#!/usr/bin/env bash
# Pull the latest netmon from GitHub and restart whatever is running.
#
# Safe to re-run. Never discards local edits: if one conflicts with an update,
# git refuses, and this stops and says so. It does nothing further if the repo
# is already up to date.
#
# Usage:  bash scripts/update.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

say() { printf '\n== %s\n' "$*"; }

# install.sh marks scripts executable, which git would otherwise count as a
# local change and refuse to update over.
git config core.fileMode false

BEFORE="$(git rev-parse HEAD)"

say "pulling $(git branch --show-current)"
if ! git pull --ff-only; then
    cat >&2 <<EOF

Update stopped; nothing was changed. See git's message above. If it names files
you edited, see what changed with:   git diff
Then either undo the edit:           git restore <file>
or set it aside and re-run:          git stash
EOF
    exit 1
fi

AFTER="$(git rev-parse HEAD)"
if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "   already up to date"
    exit 0
fi
git log --oneline "$BEFORE..$AFTER"

chmod +x scripts/*.sh

if ! git diff --quiet "$BEFORE" "$AFTER" -- requirements.txt && [[ -x .venv/bin/pip ]]; then
    say "python packages"
    .venv/bin/pip install --quiet -r requirements.txt
fi

say "restarting"
if systemctl is-active --quiet netmon-collector 2>/dev/null; then
    sudo systemctl restart netmon-collector
    echo "   collector service restarted"
elif systemctl --user is-active --quiet netmon-dashboard 2>/dev/null; then
    systemctl --user restart netmon-dashboard
    echo "   dashboard service restarted"
elif PID="$(pgrep -f 'python[0-9.]* -m netmon$' | head -n 1)" && [[ -n "$PID" ]]; then
    # Reuse the running dashboard's display, so this also works over ssh.
    DISPLAY_ENV=()
    if [[ -r "/proc/$PID/environ" ]]; then
        while IFS= read -r line; do
            DISPLAY_ENV+=("$line")
        done < <(tr '\0' '\n' < "/proc/$PID/environ" \
            | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR)=' || true)
    fi
    kill "$PID"
    sleep 2
    env ${DISPLAY_ENV[@]+"${DISPLAY_ENV[@]}"} nohup scripts/start-dashboard.sh >/dev/null 2>&1 &
    echo "   dashboard restarted"
else
    echo "   netmon is not running; the new version will load next time it starts"
fi

echo
echo "Updated to $(git log -1 --format='%h %s')"
