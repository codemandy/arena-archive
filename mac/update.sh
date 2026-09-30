#!/bin/zsh
# Updates CHANNEL on this Mac: pulls the latest code, rebuilds, reinstalls and
# reopens the app. The app's own "Update CHANNEL…" menu item runs this script.
#
#   ./mac/update.sh
set -euo pipefail

ROOT=${0:A:h:h}
BUNDLE_ID=studio.oxoy.arena-archive
cd "$ROOT"

is_running() {
  [[ "$(osascript -e "application id \"$BUNDLE_ID\" is running")" == "true" ]]
}

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "This Mac has uncommitted changes in $ROOT."
  echo "Commit and push them (or stash them), then run the update again."
  exit 1
fi

echo "Pulling the latest CHANNEL…"
before=$(git rev-parse HEAD)
git pull --ff-only
if [[ "$(git rev-parse HEAD)" == "$before" ]]; then
  echo "Already up to date. Rebuilding anyway, in case the installed app is older."
fi

# Quit normally, so the app writes the archive back to iCloud and releases its lock.
if is_running; then
  echo "Quitting CHANNEL…"
  osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
  for _ in {1..40}; do is_running || break; sleep 0.5; done
  if is_running; then
    echo "CHANNEL didn't quit, probably because a dialog is open. Close it, quit CHANNEL, then run the update again."
    exit 1
  fi
fi

./mac/build.sh --install
open "$HOME/Applications/CHANNEL.app"
echo "CHANNEL is up to date ($(git rev-parse --short HEAD))."
