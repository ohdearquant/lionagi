#!/usr/bin/env bash
set -euo pipefail

# ci.sh — Single source of truth for CI checks.
# Used by: Makefile, .github/workflows/ci.yml, pre-commit hooks.
# Usage: scripts/ci.sh <command> [args...]

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/apps/studio/frontend"
MARKETPLACE_DIR="$REPO_ROOT/marketplace"

_has_cmd() { command -v "$1" &>/dev/null; }

_require() {
  for cmd in "$@"; do
    _has_cmd "$cmd" || { echo "SKIP: $cmd not found"; return 1; }
  done
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

lint-python() {
  echo "==> ruff check"
  # Scope to non-core paths by default. Core SDK (lionagi/) has pre-existing
  # violations from the black+isort era; full migration tracked separately.
  # Pre-commit ruff hook still catches violations in edited core files.
  if [ $# -eq 0 ]; then
    uv run ruff check apps/ tests/ marketplace/ scripts/
  else
    uv run ruff check "$@"
  fi
}

fmt-python() {
  echo "==> ruff format"
  uv run ruff format "${@:-.}"
  echo "==> ruff check --fix"
  uv run ruff check --fix "${@:-.}" 2>/dev/null || true
}

test-python() {
  echo "==> pytest"
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-3}" \
    --disable-warnings \
    "${@:-tests/}"
}

test-python-cov() {
  echo "==> pytest with coverage"
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-1}" \
    --disable-warnings \
    --cov=lionagi --cov-report=xml --cov-report=term \
    "${@:-tests/}"
}

# ---------------------------------------------------------------------------
# Frontend (apps/studio/frontend)
# ---------------------------------------------------------------------------

_fe_ready() {
  [ -d "$FRONTEND_DIR/node_modules" ] || {
    echo "SKIP: frontend node_modules missing (run 'make fe-install' first)"
    return 1
  }
}

fe-install() {
  echo "==> pnpm install (frontend)"
  cd "$FRONTEND_DIR"
  if _has_cmd pnpm; then
    pnpm install --frozen-lockfile 2>/dev/null || pnpm install
  elif _has_cmd npm; then
    npm ci 2>/dev/null || npm install
  else
    echo "SKIP: no pnpm or npm"; return 1
  fi
}

lint-frontend() {
  _fe_ready || return 0
  cd "$FRONTEND_DIR"
  echo "==> eslint"
  npx eslint .
  echo "==> tsc --noEmit"
  npx tsc --noEmit
}

fmt-frontend() {
  _fe_ready || return 0
  cd "$FRONTEND_DIR"
  echo "==> prettier --write"
  npx prettier --write .
}

fmt-check-frontend() {
  _fe_ready || return 0
  cd "$FRONTEND_DIR"
  echo "==> prettier --check"
  npx prettier --check .
}

build-frontend() {
  _fe_ready || return 0
  cd "$FRONTEND_DIR"
  echo "==> next build"
  npx next build
}

typecheck-frontend() {
  _fe_ready || return 0
  cd "$FRONTEND_DIR"
  echo "==> tsc --noEmit"
  npx tsc --noEmit
}

# ---------------------------------------------------------------------------
# Marketplace
# ---------------------------------------------------------------------------

lint-marketplace() {
  echo "==> marketplace content lint"
  cd "$REPO_ROOT"
  local rc=0
  local SCAN_PATHS="marketplace/ .claude-plugin/ README.md"

  echo "  checking absolute paths..."
  if rg --hidden "/Users/lion" $SCAN_PATHS 2>/dev/null; then
    echo "  FAIL: absolute paths found"; rc=1
  fi

  echo "  checking stale repo refs..."
  if rg --hidden "khive-ai/lionagi" $SCAN_PATHS 2>/dev/null; then
    echo "  FAIL: stale khive-ai/lionagi refs found"; rc=1
  fi

  echo "  checking yolo without bypass..."
  # Multiline-aware: join backslash-continued lines before checking
  # for --yolo without --bypass.
  if _has_cmd python3 || _has_cmd python; then
    local py=$(_has_cmd python3 && echo python3 || echo python)
    local yolo_files
    yolo_files=$(rg -l --hidden 'li play' $SCAN_PATHS 2>/dev/null || true)
    if [ -n "$yolo_files" ]; then
      if ! echo "$yolo_files" | xargs "$py" -c "
import sys, re
rc = 0
for path in sys.argv[1:]:
    text = open(path).read()
    joined = re.sub(r'\\\\\n\s*', ' ', text)
    for m in re.finditer(r'li play[^\n]*--yolo[^\n]*', joined):
        if '--bypass' not in m.group():
            print(f'{path}: {m.group().strip()[:120]}')
            rc = 1
sys.exit(rc)
"; then
        echo "  FAIL: --yolo without --bypass found"; rc=1
      fi
    fi
  else
    if rg --hidden 'li play.*--yolo' $SCAN_PATHS 2>/dev/null | grep -v -- '--bypass' | grep -q .; then
      echo "  FAIL: --yolo without --bypass found"; rc=1
    fi
  fi

  echo "  validating manifests..."
  if _has_cmd python3; then
    python3 "$MARKETPLACE_DIR/scripts/validate_manifests.py" || rc=1
  elif _has_cmd python; then
    python "$MARKETPLACE_DIR/scripts/validate_manifests.py" || rc=1
  else
    echo "  SKIP: no python for manifest validation"
  fi

  echo "  linting skill content..."
  if _has_cmd uv; then
    uv run python "$MARKETPLACE_DIR/scripts/lint_skills.py" || rc=1
  elif _has_cmd python3; then
    python3 "$MARKETPLACE_DIR/scripts/lint_skills.py" || rc=1
  elif _has_cmd python; then
    python "$MARKETPLACE_DIR/scripts/lint_skills.py" || rc=1
  else
    echo "  SKIP: no python for skill content lint"
  fi

  echo "  running pytest-based surface validation..."
  if _has_cmd uv; then
    uv run pytest tests/marketplace_lint.py -v --timeout=60 --no-header -q || rc=1
  else
    echo "  SKIP: uv not found for pytest marketplace validation"
  fi

  [ $rc -eq 0 ] && echo "  marketplace lint: PASS" || { echo "  marketplace lint: FAIL"; return 1; }
}

# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

lint() {
  lint-python "$@"
  lint-frontend
  lint-marketplace
}

fmt() {
  fmt-python "$@"
  fmt-frontend
}

ci() {
  echo "=== CI: lint ==="
  lint-python
  lint-frontend
  lint-marketplace

  echo ""
  echo "=== CI: test ==="
  test-python

  echo ""
  echo "=== CI: build ==="
  build-frontend

  echo ""
  echo "ALL CI CHECKS PASSED"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

cmd="${1:-help}"
shift 2>/dev/null || true

case "$cmd" in
  lint-python|fmt-python|test-python|test-python-cov) "$cmd" "$@" ;;
  lint-frontend|fmt-frontend|fmt-check-frontend|build-frontend|typecheck-frontend|fe-install) "$cmd" "$@" ;;
  lint-marketplace) "$cmd" "$@" ;;
  lint|fmt|ci) "$cmd" "$@" ;;
  help|--help|-h)
    echo "Usage: scripts/ci.sh <command>"
    echo ""
    echo "Python:      lint-python, fmt-python, test-python, test-python-cov"
    echo "Frontend:    fe-install, lint-frontend, fmt-frontend, fmt-check-frontend,"
    echo "             build-frontend, typecheck-frontend"
    echo "Marketplace: lint-marketplace"
    echo "Composite:   lint (all linters), fmt (all formatters), ci (full pipeline)"
    ;;
  *)
    echo "Unknown command: $cmd (run 'scripts/ci.sh help')" >&2
    exit 1
    ;;
esac
