#!/usr/bin/env bash
# Regenerate Lion.xcodeproj from project.yml, then build and unit-test the Mac app. DerivedData and
# the log go to a workspace outside the repository. Exit status: xcodebuild's, 75 when a build lock
# refuses (the build did not run), 78 when the locks are not configured (nothing ran).
#
#   LION_MAC_WORKSPACE  where DerivedData and the log go
#                       (default: ~/Library/Caches/lion-mac-build, outside any checkout)
#   LION_LOCK_DIR       the directory holding this machine's two build locks, with-build-lock.sh and
#                       with-machine-lock.sh. xcodebuild runs under both, in that order:
#                         <dir>/with-build-lock.sh <dir>/with-machine-lock.sh xcodebuild <arguments>
#                       Read from the environment, else from the gitignored Local.xcconfig. "none"
#                       builds without locks on a machine that has none. Unset, or a lock that is
#                       not there, refuses rather than building unlocked.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${LION_MAC_WORKSPACE:-$HOME/Library/Caches/lion-mac-build}"
XCODEGEN=/opt/homebrew/bin/xcodegen
XCODEBUILD=/usr/bin/xcodebuild
LOG="$WORKSPACE/xcodebuild.log"

if [ ! -f "$HERE/Local.xcconfig" ]; then
    cp "$HERE/Local.xcconfig.example" "$HERE/Local.xcconfig"
fi

LOCK_DIR="${LION_LOCK_DIR:-}"
if [ -z "$LOCK_DIR" ]; then
    LOCK_DIR="$(sed -n 's/^LION_LOCK_DIR[[:space:]]*=[[:space:]]*//p' "$HERE/Local.xcconfig" | tail -n 1)"
fi
if [ -z "$LOCK_DIR" ]; then
    echo "== refusing: no build locks configured; set LION_LOCK_DIR (environment or Local.xcconfig), or set it to none" >&2
    exit 78
fi

LOCKED=()
if [ "$LOCK_DIR" != "none" ]; then
    for wrapper in with-build-lock.sh with-machine-lock.sh; do
        if [ ! -x "$LOCK_DIR/$wrapper" ]; then
            echo "== refusing: $LOCK_DIR/$wrapper is missing or not executable" >&2
            exit 78
        fi
    done
    LOCKED=("$LOCK_DIR/with-build-lock.sh" "$LOCK_DIR/with-machine-lock.sh")
fi

mkdir -p "$WORKSPACE"
"$XCODEGEN" generate --spec "$HERE/project.yml" --project "$HERE"

set +e
${LOCKED[@]+"${LOCKED[@]}"} "$XCODEBUILD" -project "$HERE/Lion.xcodeproj" -scheme Lion -destination 'platform=macOS' \
    -derivedDataPath "$WORKSPACE/DerivedData" build test 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e

if [ "$rc" -eq 75 ]; then
    echo "== a build lock refused (rc=75): nothing was built; see the lock's message above"
else
    echo "== xcodebuild rc=$rc (log $LOG)"
    echo "== the app: open $WORKSPACE/DerivedData/Build/Products/Debug/Lion.app --args -demo"
fi
exit "$rc"
