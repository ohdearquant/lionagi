#!/usr/bin/env bash
# Pre-commit shim: ESLint --fix on staged files.
# Pre-commit passes paths relative to repo root; ESLint runs from the
# frontend dir for module resolution (eslint-config-next, plugins).
set -eu
cd "$(dirname "$0")/.."
if [ ! -d node_modules ]; then
  echo "studio frontend: node_modules missing — skipping ESLint."
  echo "  enable with: npm install --prefix apps/studio/frontend"
  exit 0
fi
args=()
for f in "$@"; do
  args+=("${f#apps/studio/frontend/}")
done
exec npx --no-install eslint --fix --no-error-on-unmatched-pattern "${args[@]}"
