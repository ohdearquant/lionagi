
# Changelog

All notable changes to lionagi are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.26.0] - 2026-05-21

### Added

- **Lion Studio** — first public release of the operational UI for lionagi.
  Dashboard with system health, runs/shows/agents inventory. Four new pages:
  Runs (paginated, filterable by status/playbook, provenance badges), Teams
  (read-only viewer), Admin (phantom session doctor + prune), Stats (DB health).
- **State persistence** — `lionagi/state/` with SQLite-backed session, branch,
  show, and definition tracking. Forward-only migrations via `StateDB.open()`.
- **Studio security** — optional bearer auth (`LIONAGI_STUDIO_AUTH_TOKEN`),
  path sanitization, plugin path bounds, `PRAGMA foreign_keys = ON`,
  TOCTOU-safe admin prune.
- **Studio a11y** — WCAG AA contrast, skip-to-main, ARIA labels/roles,
  keyboard-navigable tables.
- **CI** — unified `scripts/ci.sh` + `Makefile`, frontend lint/typecheck/build
  job, marketplace content lint, Python 3.14 in test matrix.
- **Marketplace** — LICENSE, `.gitignore`, manifest validation, lint CI,
  root README install instructions.

### Fixed

- 45 issues from discover-2026-05-21 audit (#984–#1031): N+1 queries, SSE
  races, dead code (−1787 LOC), broken ESLint baseline, data-loss chain in
  migrate-memory, dead khive verb syntax in 6 skills, shell injection in
  show redo path, stub manifests, stale repo refs, private IP leaks.

### Changed

- ag2 0.12.2 → 0.13.0
- Pre-commit: black + isort → ruff

### Security

- Bump `idna` 3.15, `pymdown-extensions` 10.21.3
- Starlette CVE fix blocked upstream — [ag2#2894](https://github.com/ag2ai/ag2/issues/2894)

## [0.25.1] - 2026-05-12

### Added

- **Lion Studio apps** — FastAPI backend + Next.js 16 frontend (`apps/studio/`),
  17 ADRs, SSE streaming, SQLite-backed runs provenance.
- **Marketplace** — Claude Code plugin system (`.claude-plugin/marketplace.json`),
  7 plugins (show, play, orchestrate, research, memory, devx, kg-bridge).
- **Codex fast mode** — `fast_mode` for OpenAI priority tier routing.
- **AG2 passthrough** — `pre_built_agent` param on `AG2BetaEndpoint`.

## [0.25.0] - 2026-05-08

### Changed

- 0.24.0 yanked — LNDL reverted, will return once spec stabilises.

### Fixed

- SSE parser rewrite — multi-line events, `[DONE]` frames, Anthropic deltas.
- Handler leak in CLI providers, `messages` default, `call_kwargs` transport.
- Request model wiring for embed/response endpoints.

### Security

- Bump `python-multipart>=0.0.27`, `gitpython>=3.1.49`

## [0.23.1] - 2026-05-02

### Added

- Provider registry (`EndpointRegistry`, `@register` decorator)
- AG2 GroupChat endpoint
- `Note` model with `deep_update()`

### Changed

- Provider modules reorganized to `providers/{company}/{endpoint}/`
- `CLIEndpoint` → `AgenticEndpoint`

## [0.23.0] - 2026-04-27

### Added

- Agent infrastructure (`AgentConfig`, `create_agent()`, `PermissionPolicy`, hooks)
- `SandboxSession` — git-worktree isolation for speculative edits
- DeepSeek native provider, Pi CLI endpoint
- `li play NAME --help`, 250+ tests

### Fixed

- `li o flow --save` regression, flaky timing tests

## [0.22.9] - 2026-04-24

### Security

- Symlink containment, flow-id validation, `--max-ops` enforcement, save-path containment
- Pin `lxml>=6.1.0`, `python-dotenv>=1.2.2`

### Added

- `li skill`, `li play`, playbook args, `--team-attach`, `--bypass`, `--add-dir`

## [0.22.8] - 2026-04-21

- Fix `StreamChunk` propagation through iModel layer

## [0.22.7] - 2026-04-20

- `li --version`, `--background` flow, docs overhaul (74% reduction)
- Fix `--show-graph` macOS, codex `reasoning_effort` clamping

## [0.22.6] - 2026-04-20

- Two-level flow DAG (`FlowAgent` + `FlowOp`), run persistence, `Middle` protocol
- Team file locking, per-agent artifact dirs
- `branch.operate()` absorbs `branch.instruct()`

## [0.22.2–0.22.5] - 2026-04-19

- `li o flow` DAG orchestration, stream persist, unified operate routing
- Security: authlib, python-multipart, pillow bumps

## [0.22.0–0.22.1] - 2026-04-18

- `branch.run()` async generator, agent profiles, `li team`, `--team-mode`

## [0.21.1] - 2026-04-17

- `li orchestrate fanout` — parallel fan-out with synthesis

## [0.21.0] - 2026-04-15

- `li` CLI with `--theme`, `--yolo`, `--verbose`; model spec parsing

## [0.20.2–0.20.4] - 2026-03-16 to 2026-04-11

- Firecrawl, Tavily search, event lifecycle, CLI provider updates, 20 fixes

## [0.20.0–0.20.1] - 2026-02-13

- `NodeConfig`/`create_node`, `Flow`, `Broadcaster`, graph algorithms, 187 doc tests

## [0.19.0–0.19.2] - 2026-02-11

- Native Gemini API, `CLIEndpoint`, async context managers

## [0.18.0–0.18.6] - 2025-10-09

- `ChatParam`/`ParseParam`, `LION_SYSTEM_MESSAGE`, AnyIO structured concurrency

## [0.17.0] - 2025-09-14

- Remove deprecated `ClaudeCodeEndpoint`

## [0.16.0] - 2025-09-02

- V1 Observable Protocol, `CompletionStream`

## [0.15.0] - 2025-08-16

- Structured concurrency (`CancelScope`, `TaskGroup`), `Pile` generics

## [0.14.0] - 2025-07-22

- `DependencyAwareExecutor`, `Session.flow()` DAG execution

## [0.13.0] - 2025-07-13

- Claude Code provider with session management

## [0.12.0] - 2025-05-14

- XML/JSON parsing, async file I/O, adapter registry

## [0.11.0] - 2025-05-01

- `Research` models, `concat` utility

## [0.10.0] - 2025-03-19

- Pandas adapters, `BaseForm`/`FlowDefinition`/`Report`, `ln` namespace

## [0.9.0] - 2025-01-24

- `Analysis` class, `as_readable` YAML formatting

## [0.8.0] - 2025-01-18

- `FlowStep`/`FlowDefinition`, Exa search, action batching

## [0.7.0] - 2025-01-13

- `interpret`/`select`/`translate` ops, Groq/Perplexity/OpenRouter

## [0.6.0] - 2025-01-04

- `MailManager`, Branch serialization, `LiteiModel`

## [0.5.0] - 2024-12-16

- LION2 protocol, class registry, `ReactInstruct`

## [0.4.0] - 2024-10-30

- `lion-core` integration, LangChain/LlamaIndex adapters

## [0.3.0] - 2024-10-06

- `uv` replaces Poetry, pre-commit hooks, CI with dependabot

## [0.2.0] - 2024-05-28

- Ollama integration, token compressor (experimental)

## [0.1.0] - 2024-04-10

- `Branch` + tree-node architecture, tool manager, async queue, knowledge graph

---

See git history for the 0.0.x series (v0.0.102–v0.0.316).
