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
  if [ $# -eq 0 ]; then
    uv run ruff check lionagi/ apps/ tests/ marketplace/ scripts/
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

# Wall-clock perf/scaling tests are unreliable under CI load + coverage; they
# are gated behind the `performance` marker and validated by benchmarks.yml.
# Override with PYTEST_MARKEXPR=performance (or "") to run them locally.
test-python() {
  echo "==> pytest"
  # --max-worker-restart=0: a hard-crashed xdist worker ("node down") otherwise
  # wedges the session for ~15 minutes before the job dies with no test name;
  # failing fast prints "crashed while running <nodeid>" instead.
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-3}" \
    --max-worker-restart="${MAX_WORKER_RESTART:-0}" \
    -m "${PYTEST_MARKEXPR:-not performance}" \
    --disable-warnings \
    "${@:-tests/}"
}

test-python-cov() {
  echo "==> pytest with coverage"
  uv run pytest \
    --asyncio-mode=auto \
    --maxfail="${MAXFAIL:-1}" \
    --max-worker-restart="${MAX_WORKER_RESTART:-0}" \
    -m "${PYTEST_MARKEXPR:-not performance}" \
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
# Publication hygiene (docs/, docs/_archive/, notebooks/, repo root)
#
# lint-marketplace's absolute-path check above only covers marketplace/ +
# .claude-plugin/ + README.md. This check covers the rest of the tree that
# can carry publication leaks: archived ADRs, notebooks, and stray files
# dropped at the repo root (e.g. internal review scratch files).
# ---------------------------------------------------------------------------

lint-hygiene() {
  echo "==> publication hygiene lint (docs/notebooks/root)"
  cd "$REPO_ROOT"
  local rc=0
  # docs/ is scanned recursively, so docs/_archive/ (nested under it) is
  # covered by the same pass. Repo root is scanned at depth 1 only (its own
  # files), so this never descends into src/tests/benchmarks trees where
  # "lambda:" is overwhelmingly Python's own closure syntax.
  local DOC_PATHS="docs/ notebooks/"
  # Known, tracked exception: these three notebooks carry absolute local
  # paths inside already-executed output cells (LLM tool-call results from
  # a prior run). Fixing them means re-running the notebooks end to end,
  # which is out of scope for a text-only hygiene pass — tracked as a
  # follow-up rather than silently left to rot the gate.
  local IPYNB_EXCEPTIONS=(-g '!notebooks/react.ipynb' -g '!notebooks/react_rag.ipynb' -g '!notebooks/references/test_instruct.ipynb')

  echo "  checking absolute machine-local paths..."
  # Scanned across ALL file types, including .py — a hardcoded /Users/lion
  # path in a committed Python helper (e.g. a notebook script) is exactly
  # as much of a leak as one in markdown; the .py exclusion below is scoped
  # to the lambda: actor-identifier check only, where it guards against
  # Python's own closure syntax, not this check.
  if rg --hidden "${IPYNB_EXCEPTIONS[@]}" "/Users/lion" $DOC_PATHS 2>/dev/null; then
    echo "  FAIL: absolute /Users/lion paths found in docs/notebooks"; rc=1
  fi
  if rg --hidden --max-depth 1 -g '!.git' "/Users/lion" . 2>/dev/null; then
    echo "  FAIL: absolute /Users/lion paths found at repo root"; rc=1
  fi

  echo "  checking internal actor identifiers (lambda:<name>)..."
  # "lambda:" immediately followed by a non-whitespace char is the internal
  # actor-identifier shape. Python's own zero-arg lambda syntax always has a
  # space after the colon once ruff-formatted (lambda: expr), so this does
  # not match committed .py source; -g '!*.py' additionally excludes .py
  # files outright as a second guard.
  if rg --hidden -g '!*.py' "${IPYNB_EXCEPTIONS[@]}" 'lambda:\S' $DOC_PATHS 2>/dev/null; then
    echo "  FAIL: internal actor identifiers (lambda:...) found in docs/notebooks"; rc=1
  fi
  if rg --hidden --max-depth 1 -g '!*.py' -g '!.git' 'lambda:\S' . 2>/dev/null; then
    echo "  FAIL: internal actor identifiers (lambda:...) found at repo root"; rc=1
  fi
  # The Unicode "λ:" shorthand for the same actor-identifier shape (λ:leo,
  # λ:lionagi). λ is not Python's lambda keyword, so there is no closure-
  # syntax ambiguity here — no .py exclusion needed.
  if rg --hidden "${IPYNB_EXCEPTIONS[@]}" 'λ:\S' $DOC_PATHS 2>/dev/null; then
    echo "  FAIL: internal actor identifiers (λ:...) found in docs/notebooks"; rc=1
  fi
  if rg --hidden --max-depth 1 -g '!.git' 'λ:\S' . 2>/dev/null; then
    echo "  FAIL: internal actor identifiers (λ:...) found at repo root"; rc=1
  fi

  echo "  checking founder-name process narration (Ocean's)..."
  if rg --hidden "${IPYNB_EXCEPTIONS[@]}" "\bOcean's\b" $DOC_PATHS 2>/dev/null; then
    echo "  FAIL: founder-name process narration found in docs/notebooks"; rc=1
  fi
  if rg --hidden --max-depth 1 -g '!.git' "\bOcean's\b" . 2>/dev/null; then
    echo "  FAIL: founder-name process narration found at repo root"; rc=1
  fi

  [ $rc -eq 0 ] && echo "  publication hygiene: PASS" || { echo "  publication hygiene: FAIL"; return 1; }
}

# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

lint() {
  lint-python "$@"
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
  lint-python|fmt-python|test-python|test-python-cov) "$cmd" "$@" ;;
  lint-frontend|fmt-frontend|fmt-check-frontend|build-frontend|typecheck-frontend|fe-install) "$cmd" "$@" ;;
  lint-marketplace|lint-hygiene) "$cmd" "$@" ;;
  lint|fmt|ci) "$cmd" "$@" ;;
  help|--help|-h)
    echo "Usage: scripts/ci.sh <command>"
    echo ""
    echo "Python:      lint-python, fmt-python, test-python, test-python-cov"
    echo "Frontend:    fe-install, lint-frontend, fmt-frontend, fmt-check-frontend,"
    echo "             build-frontend, typecheck-frontend"
    echo "Marketplace: lint-marketplace"
    echo "Hygiene:     lint-hygiene (publication leaks: docs/notebooks/root)"
    echo "Composite:   lint (all linters), fmt (all formatters), ci (full pipeline)"
    ;;
  *)
    echo "Unknown command: $cmd (run 'scripts/ci.sh help')" >&2
    exit 1
    ;;
esac
