#!/usr/bin/env bash
# Pre-commit shim: Prettier --write on staged files.
set -eu
cd "$(dirname "$0")/.."
if [ ! -d node_modules ]; then
  echo "studio frontend: node_modules missing — skipping Prettier."
  echo "  enable with: npm install --prefix apps/studio/frontend"
  exit 0
fi
args=()
for f in "$@"; do
  args+=("${f#apps/studio/frontend/}")
done
exec npx --no-install prettier --write --ignore-unknown "${args[@]}"
