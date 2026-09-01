#!/bin/bash
#
# Local daily run, invoked by launchd. Wraps hnb_watch.py with git sync so this
# machine and GitHub Actions share ONE history and ONE alert-cooldown state.
#
# Without the sync, both runners keep their own CSV and their own state file:
# the histories drift apart, the N-day-high comparison differs between them, and
# a sustained high alerts you twice -- once from each.
#
# No paths are hardcoded. The script locates the repo from its own position on
# disk, so it works unchanged under any username or on any machine -- which is
# exactly the problem that bit this project once already.
#
# Install:
#   chmod +x run_daily.sh
#   ./run_daily.sh                       # test by hand first
#   cp com.hnbwatch.daily.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.hnbwatch.daily.plist

set -uo pipefail

# Resolve this script's own directory, following symlinks.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
REPO_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

VENV_PY="$REPO_DIR/.venv/bin/python"
LOG="$REPO_DIR/run_daily.log"

# launchd starts jobs with a near-empty environment and no shell profile, so
# anything on PATH must be spelled out. Both Homebrew prefixes are included so
# this works on Apple Silicon and Intel.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"

exec >> "$LOG" 2>&1
echo "=============================================================="
echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  starting local run"
echo "repo: $REPO_DIR"

cd "$REPO_DIR" || { echo "FATAL: cannot cd to $REPO_DIR"; exit 1; }

if [ ! -x "$VENV_PY" ]; then
    echo "FATAL: no venv python at $VENV_PY"
    echo "  create it with:  python3.12 -m venv .venv"
    exit 1
fi

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "WARNING: no .env found -- Telegram and Sheets will be skipped"
fi

# Pull first: GitHub Actions may have already recorded today and advanced the
# cooldown. --autostash protects any uncommitted local edits.
echo "--- syncing history from GitHub ---"
git pull --rebase --autostash || echo "WARNING: pull failed, continuing with local history"

echo "--- running watcher ---"
"$VENV_PY" hnb_watch.py run
STATUS=$?
echo "watcher exit status: $STATUS"

# Push results back so the cloud run sees them. -f on the state file because
# .gitignore blocks *.json.
if [ "$STATUS" -eq 0 ]; then
    echo "--- publishing history ---"
    git add hnb_usd_buying.csv 2>/dev/null
    git add -f hnb_watch_state.json 2>/dev/null
    if ! git diff --staged --quiet; then
        git commit -m "rates: $(date '+%Y-%m-%d') (local)" && git push
    else
        echo "nothing new to publish"
    fi
else
    echo "watcher failed -- not publishing anything"
fi

# Keep the log from growing without bound.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S %Z')  done"
exit "$STATUS"
