.PHONY: lint fmt test ci \
       lint-python fmt-python test-python \
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
