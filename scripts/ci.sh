#!/usr/bin/env bash
set -euo pipefail

# ci.sh — Single source of truth for CI checks.
# Used by: Makefile, .github/workflows/ci.yml, pre-commit hooks.
# Usage: scripts/ci.sh <command> [args...]

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/apps/studio/frontend"
MARKETPLACE_DIR="$REPO_ROOT/marketplace"
NOTEBOOK_HYGIENE_SCRIPT="$REPO_ROOT/scripts/lint_notebook_hygiene.py"
QUARANTINE_SCRIPT="$REPO_ROOT/scripts/quarantine.py"

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
  if [ $# -eq 0 ]; then
    uv run ruff check lionagi/ apps/ tests/ marketplace/ scripts/
  else
    uv run ruff check "$@"
  fi
}

lint-quarantine() {
  echo "==> pytest quarantine manifest"
  uv run python "$QUARANTINE_SCRIPT" check --max-entries 15
}

fmt-python() {
  echo "==> ruff format"
  uv run ruff format "${@:-.}"
  echo "==> ruff check --fix"
  uv run ruff check --fix "${@:-.}" 2>/dev/null || true
}

# Wall-clock perf/scaling tests are unreliable under CI load + coverage; they
# are gated behind the `performance` marker and run by ci.yml's dedicated lane.
# Override with PYTEST_MARKEXPR=performance (or "") to run them locally.
test-python() {
  echo "==> pytest"
  # --max-worker-restart=0: a hard-crashed xdist worker ("node down") otherwise
  # wedges the session for ~15 minutes before the job dies with no test name;
  # failing fast prints "crashed while running <nodeid>" instead.
  local report_args=()
  if [ -n "${PYTEST_JUNIT_XML:-}" ]; then
    mkdir -p "$(dirname "$PYTEST_JUNIT_XML")"
    report_args=(-o junit_family=legacy --junitxml="$PYTEST_JUNIT_XML")
  fi
  local targets=("$@")
  [ ${#targets[@]} -gt 0 ] || targets=(tests/)
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-3}" \
    --max-worker-restart="${MAX_WORKER_RESTART:-0}" \
    -m "${PYTEST_MARKEXPR:-not performance and not flaky_quarantine}" \
    --disable-warnings \
    "${report_args[@]}" \
    "${targets[@]}"
}

test-python-cov() {
  echo "==> pytest with coverage"
  local report_args=()
  if [ -n "${PYTEST_JUNIT_XML:-}" ]; then
    mkdir -p "$(dirname "$PYTEST_JUNIT_XML")"
    report_args=(-o junit_family=legacy --junitxml="$PYTEST_JUNIT_XML")
  fi
  local targets=("$@")
  [ ${#targets[@]} -gt 0 ] || targets=(tests/)
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-1}" \
    --max-worker-restart="${MAX_WORKER_RESTART:-0}" \
    -m "${PYTEST_MARKEXPR:-not performance and not flaky_quarantine}" \
    --disable-warnings \
    --cov=lionagi --cov-report=xml --cov-report=term \
    "${report_args[@]}" \
    "${targets[@]}"
}

test-python-quarantine() {
  echo "==> quarantined pytest lane"
  local quarantine_count
  quarantine_count=$(uv run python "$QUARANTINE_SCRIPT" count)
  if [ "$quarantine_count" -eq 0 ]; then
    echo "No quarantined tests; lane is green."
    return 0
  fi

  local report_args=()
  if [ -n "${PYTEST_JUNIT_XML:-}" ]; then
    mkdir -p "$(dirname "$PYTEST_JUNIT_XML")"
    report_args=(-o junit_family=legacy --junitxml="$PYTEST_JUNIT_XML")
  fi
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-0}" \
    --max-worker-restart="${MAX_WORKER_RESTART:-0}" \
    -m flaky_quarantine \
    --disable-warnings \
    "${report_args[@]}" \
    tests/
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
# Publication hygiene (docs/, docs/_archive/, notebooks/, cookbooks/, repo root)
#
# lint-marketplace's absolute-path check above only covers marketplace/ +
# .claude-plugin/ + README.md. This check covers the rest of the tree that
# can carry publication leaks: archived ADRs, notebooks, cookbooks, and stray files
# dropped at the repo root (e.g. internal review scratch files).
# ---------------------------------------------------------------------------

_hygiene_rg_scan() {
  local rg_bin="$1"
  local label="$2"
  shift 2

  local rg_rc=0
  if "$rg_bin" "$@"; then
    echo "  FAIL: $label found"
    return 1
  else
    rg_rc=$?
  fi

  if [ "$rg_rc" -eq 1 ]; then
    return 0
  fi

  echo "  scanner error: ripgrep failed while checking $label (exit $rg_rc)" >&2
  return 2
}

lint-hygiene() {
  echo "==> publication hygiene lint (docs/notebooks/cookbooks/root)"
  cd "$REPO_ROOT"
  local rc=0
  local scan_rc=0
  local rg_bin="${RG_BIN:-rg}"
  if ! _has_cmd "$rg_bin"; then
    echo "ERROR: ripgrep (rg) is required for publication hygiene. Install ripgrep and retry." >&2
    return 2
  fi

  # docs/ is scanned recursively, so docs/_archive/ (nested under it) is
  # covered by the same pass. Repo root is scanned at depth 1 only (its own
  # files), so this never descends into src/tests/benchmarks trees where
  # "lambda:" is overwhelmingly Python's own closure syntax.
  local CONTENT_PATHS=(docs/ notebooks/ cookbooks/)

  echo "  checking absolute machine-local paths..."
  # Scanned across ALL file types, including .py — a hardcoded machine path
  # in a committed Python helper (e.g. a notebook script) is exactly
  # as much of a leak as one in markdown; the .py exclusion below is scoped
  # to the lambda namespace check only, where it guards against
  # Python's own closure syntax, not this check.
  if _hygiene_rg_scan "$rg_bin" "machine-local paths" --hidden '/Users/[^/[:space:]"]+/' "${CONTENT_PATHS[@]}"; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi
  if _hygiene_rg_scan "$rg_bin" "machine-local paths at repo root" --hidden --max-depth 1 -g '!.git' '/Users/[^/[:space:]"]+/' .; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi

  echo "  checking internal namespace identifiers (lambda:<name>)..."
  # Source code is excluded from this textual pass. Notebook prose and outputs
  # are parsed separately so valid lambda expressions in code cells are not
  # mistaken for namespace identifiers.
  if _hygiene_rg_scan "$rg_bin" "internal namespace identifiers" --hidden -g '!*.py' -g '!*.ipynb' '\blambda:[a-z][a-z0-9_-]*\b' "${CONTENT_PATHS[@]}"; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi
  if _hygiene_rg_scan "$rg_bin" "internal namespace identifiers at repo root" --hidden --max-depth 1 -g '!*.py' -g '!*.ipynb' -g '!.git' '\blambda:[a-z][a-z0-9_-]*\b' .; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi
  if uv run python "$NOTEBOOK_HYGIENE_SCRIPT" notebooks/ cookbooks/; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -eq 1 ]; then
      echo "  FAIL: internal namespace identifiers found in notebook prose or outputs"
    else
      echo "  scanner error: notebook hygiene scan failed (exit $scan_rc)" >&2
    fi
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi

  # The Unicode shorthand cannot be Python's lambda keyword, so source files
  # do not need the code-cell exclusion used above.
  if _hygiene_rg_scan "$rg_bin" "internal Unicode namespace identifiers" --hidden 'λ:[a-z][a-z0-9_-]*' "${CONTENT_PATHS[@]}"; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi
  if _hygiene_rg_scan "$rg_bin" "internal Unicode namespace identifiers at repo root" --hidden --max-depth 1 -g '!.git' 'λ:[a-z][a-z0-9_-]*' .; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi

  echo "  checking founder-name process narration (Ocean's)..."
  if _hygiene_rg_scan "$rg_bin" "founder-name process narration" --hidden "\bOcean's\b" "${CONTENT_PATHS[@]}"; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi
  if _hygiene_rg_scan "$rg_bin" "founder-name process narration at repo root" --hidden --max-depth 1 -g '!.git' "\bOcean's\b" .; then
    :
  else
    scan_rc=$?
    if [ "$scan_rc" -gt "$rc" ]; then rc=$scan_rc; fi
  fi

  if [ "$rc" -eq 0 ]; then
    echo "  publication hygiene: PASS"
  elif [ "$rc" -eq 1 ]; then
    echo "  publication hygiene: FAIL"
    return 1
  else
    echo "  publication hygiene: ERROR" >&2
    return "$rc"
  fi
}

# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

lint() {
  lint-python "$@"
  lint-quarantine
  lint-frontend
  lint-marketplace
  lint-hygiene
}

fmt() {
  fmt-python "$@"
  fmt-frontend
}

ci() {
  echo "=== CI: lint ==="
  lint-python
  lint-quarantine
  lint-frontend
  lint-marketplace
  lint-hygiene

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
  lint-python|lint-quarantine|fmt-python|test-python|test-python-cov|test-python-quarantine) "$cmd" "$@" ;;
  lint-frontend|fmt-frontend|fmt-check-frontend|build-frontend|typecheck-frontend|fe-install) "$cmd" "$@" ;;
  lint-marketplace|lint-hygiene) "$cmd" "$@" ;;
  lint|fmt|ci) "$cmd" "$@" ;;
  help|--help|-h)
    echo "Usage: scripts/ci.sh <command>"
    echo ""
    echo "Python:      lint-python, lint-quarantine, fmt-python, test-python,"
    echo "             test-python-cov, test-python-quarantine"
    echo "Frontend:    fe-install, lint-frontend, fmt-frontend, fmt-check-frontend,"
    echo "             build-frontend, typecheck-frontend"
    echo "Marketplace: lint-marketplace"
    echo "Hygiene:     lint-hygiene (publication leaks: docs/notebooks/cookbooks/root)"
    echo "Composite:   lint (all linters), fmt (all formatters), ci (full pipeline)"
    ;;
  *)
    echo "Unknown command: $cmd (run 'scripts/ci.sh help')" >&2
    exit 1
    ;;
esac
