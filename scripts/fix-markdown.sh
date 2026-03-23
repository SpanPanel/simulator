#!/usr/bin/env bash
# Format Markdown with Prettier (prose wrap) then markdownlint-cli2 --fix.
# Usage: scripts/fix-markdown.sh [<workspace_root>]
# Default workspace root is the repository root (this script's parent directory).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="${1:-$REPO_ROOT}"
cd "$WORKSPACE_ROOT"

if [ ! -d "$WORKSPACE_ROOT" ]; then
  echo "Error: not a directory: $WORKSPACE_ROOT" >&2
  exit 1
fi

# Best-effort: activate project venv when present (not required for npx tools).
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate" || true
fi

if ! command -v npx >/dev/null 2>&1; then
  echo "Error: npx is not available (install Node.js)." >&2
  exit 1
fi

MD_GLOBS=(
  "*.md"
  "**/docs/**/*.md"
  "tests/**/*.md"
  "scripts/**/*.md"
  ".github/**/*.md"
)

echo "Fixing markdown under: $(pwd)"

echo "Step 1: Prettier (markdown, prose wrap)..."
npx --yes prettier --ignore-path .prettierignore --parser markdown \
  --print-width 160 --prose-wrap always \
  "${MD_GLOBS[@]}" --write 2>/dev/null || true

echo "Step 1b: Prettier (repo .prettierrc* when present)..."
if [ -f ".prettierrc" ] || [ -f ".prettierrc.json" ] || [ -f ".prettierrc.yaml" ]; then
  npx --yes prettier --ignore-path .prettierignore \
    "${MD_GLOBS[@]}" --write 2>/dev/null || true
else
  npx --yes prettier --ignore-path .prettierignore --print-width 160 --prose-wrap always \
    --parser markdown "${MD_GLOBS[@]}" --write 2>/dev/null || true
fi

echo "Step 2: markdownlint-cli2 --fix..."
npx --yes markdownlint-cli2 --fix "${MD_GLOBS[@]}" 2>/dev/null || true

echo "Done."
