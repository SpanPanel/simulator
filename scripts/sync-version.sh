#!/usr/bin/env bash
# Sync version strings across the project from pyproject.toml (source of truth).
# Intended to run as a pre-commit hook — fixes files in-place and exits non-zero
# if any file was out of sync so the commit is retried with corrected versions.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

TOML="$REPO_ROOT/pyproject.toml"
INIT="$REPO_ROOT/src/span_panel_simulator/__init__.py"
CONFIG="$REPO_ROOT/span_panel_simulator/config.yaml"
DOCKERFILE="$REPO_ROOT/span_panel_simulator/Dockerfile"

# Extract version from pyproject.toml
VERSION=$(grep -m1 '^version' "$TOML" | sed 's/version *= *"\(.*\)"/\1/')
if [[ -z "$VERSION" ]]; then
    echo "sync-version: could not parse version from $TOML" >&2
    exit 1
fi

DIRTY=0

sync_file() {
    local file="$1" pattern="$2" replacement="$3"
    if ! grep -qF "$replacement" "$file"; then
        sed -i '' "s|${pattern}|${replacement}|" "$file"
        git add "$file"
        echo "sync-version: updated $file -> $VERSION"
        DIRTY=1
    fi
}

sync_file "$INIT"       '__version__ = ".*"'          "__version__ = \"$VERSION\""
sync_file "$CONFIG"      'version: ".*"'               "version: \"$VERSION\""
sync_file "$DOCKERFILE"  'io.hass.version=".*"'        "io.hass.version=\"$VERSION\""

if [[ "$DIRTY" -eq 1 ]]; then
    echo "sync-version: files updated to $VERSION — restage and retry commit"
    exit 1
fi
