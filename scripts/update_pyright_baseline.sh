#!/usr/bin/env bash
# Regenerate the pyright error-count baseline from the current tree.
set -euo pipefail
cd "$(dirname "$0")/.."
uvx pyright==1.1.402 --outputjson > /tmp/pyright_output.json || true
count=$(python3 -c "import json; print(json.load(open('/tmp/pyright_output.json'))['summary']['errorCount'])")
printf '%s\n' "$count" > .github/pyright-baseline.txt
echo "pyright baseline updated to $count"
