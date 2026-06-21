# Den — VS Code extension. Build, test, and install helpers.
# Run from apps/vscode/. Bare `make` shows this help.

VERSION := $(shell node -p "require('./package.json').version")
VSIX    := den-$(VERSION).vsix
EXT_ID  := lionagi.den

.PHONY: help install typecheck test compile watch package install-vsix reinstall uninstall ci all clean

.DEFAULT_GOAL := help

help:         ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:      ## Install npm dependencies
	npm install

# File target: runs only when node_modules is absent, so the dogfood path
# (package / install-vsix) bootstraps deps on a fresh checkout (e.g. Codespaces).
node_modules:
	npm install

typecheck: node_modules ## Type-check, no emit (tsc --noEmit)
	npm run typecheck

test: node_modules ## Run unit tests (vitest)
	npm run test

compile: node_modules ## Bundle to out/ (esbuild)
	npm run compile

watch: node_modules ## Rebuild on change (esbuild --watch)
	npm run watch

package: node_modules typecheck test ## Build the installable VSIX (gated on typecheck + test)
	npm run package
	@echo "built $(VSIX)"

install-vsix: ## Install the VSIX into VS Code, building it first if absent
	@test -f $(VSIX) || $(MAKE) package
	code --install-extension $(VSIX) --force

reinstall: package install-vsix ## Rebuild the VSIX and (re)install it into VS Code

uninstall:    ## Remove the extension from VS Code
	code --uninstall-extension $(EXT_ID)

ci:           ## What CI runs: clean install + typecheck + compile + test
	npm ci
	npm run typecheck
	npm run compile
	npm run test

all: typecheck test compile package ## Verify everything and build the VSIX

clean:        ## Remove build output (out/) and packaged VSIX files
	rm -rf out
	rm -f *.vsix
