.PHONY: lint fmt test ci \
       lint-python fmt-python test-python \
       pg-up pg-down test-sqlite test-pg test-dual \
       lint-frontend fmt-frontend build-frontend fe-install typecheck-frontend \
       lint-marketplace \
       help

CI := scripts/ci.sh

# --- Composite targets ---

lint:           ## Run all linters (python + frontend + marketplace)
	@$(CI) lint

fmt:            ## Run all formatters (python + frontend)
	@$(CI) fmt

test:           ## Run pytest
	@$(CI) test-python

ci:             ## Full CI pipeline (lint + test + build)
	@$(CI) ci

# --- Python ---

lint-python:    ## Ruff check
	@$(CI) lint-python

fmt-python:     ## Ruff format + fix
	@$(CI) fmt-python

test-python:    ## Pytest
	@$(CI) test-python

# --- State DB / Postgres ---

PG_CONTAINER := lionagi-pg-test
PG_IMAGE     := postgres:16-alpine
PG_PORT      := 5433
PG_TEST_URL  := postgresql+asyncpg://postgres:lionagi@localhost:$(PG_PORT)/lionagi_state

pg-up:          ## Start a throwaway Postgres for StateDB tests (port 5433)
	-@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true
	@docker run -d --name $(PG_CONTAINER) \
	  -e POSTGRES_PASSWORD=lionagi -e POSTGRES_DB=lionagi_state \
	  -p $(PG_PORT):5432 $(PG_IMAGE) >/dev/null
	@echo "waiting for postgres on :$(PG_PORT) ..."
	@for i in $$(seq 1 60); do \
	  docker exec $(PG_CONTAINER) pg_isready -U postgres -d lionagi_state >/dev/null 2>&1 && exit 0; \
	  sleep 0.5; \
	done; echo "postgres did not become ready" >&2; exit 1

pg-down:        ## Stop and remove the throwaway Postgres
	-@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true

test-sqlite:    ## StateDB tests on the default SQLite backend
	@$(CI) test-python tests/state tests/apps_studio_server tests/cli tests/hooks

test-pg:        ## StateDB tests against a running Postgres (run `make pg-up` first)
	@LIONAGI_TEST_PG_URL=$(PG_TEST_URL) $(CI) test-python tests/state

test-dual: pg-up ## Spin up Postgres, run the SQLite + Postgres legs, then tear down
	@LIONAGI_TEST_PG_URL=$(PG_TEST_URL) $(CI) test-python tests/state; \
	  status=$$?; docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true; exit $$status

# --- Frontend ---

fe-install:     ## Install frontend dependencies (pnpm)
	@$(CI) fe-install

lint-frontend:  ## ESLint + TypeScript check
	@$(CI) lint-frontend

fmt-frontend:   ## Prettier format
	@$(CI) fmt-frontend

build-frontend: ## Next.js production build
	@$(CI) build-frontend

typecheck-frontend: ## TypeScript --noEmit
	@$(CI) typecheck-frontend

# --- Marketplace ---

lint-marketplace: ## Marketplace content + manifest lint
	@$(CI) lint-marketplace

# --- Help ---

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
