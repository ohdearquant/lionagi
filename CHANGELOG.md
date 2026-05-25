
# Changelog

All notable changes to lionagi are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.26.7] - 2026-05-25

Issue-sweep release. 16 issues closed, 14 PRs merged this cycle.

### Added

- **CLI: `li kill`** (#1094) — terminate runs/sessions/plays with SIGTERM→SIGKILL escalation,
  `--recursive` cascade, `--all-stale` sweep (sessions + invocations; plays/shows are
  orchestrators with no direct PID — use `--recursive` for explicit cleanup).
- **CLI: `li monitor`** (#1089) — table + detail + `--watch` for play/agent/run progress.
  Filters: `--since`, `--type`, `--project`.
- **CLI: `--timeout` deadline awareness** (#1087) — when `--timeout N` is set, a deadline
  preamble is injected as a leading user message so agents (codex/claude-code/etc.) can
  ration reasoning instead of running until guillotine.
- **Orchestrator: FlowOp budget propagation** (#1091) — total budget split across ops by
  `FlowOp.budget_weight`; each worker receives a BUDGET preamble with its share + deadline.
- **LNDL Phase 1** (#966) — opt-in structured-output formatter ported from beta:
  `LNDLOutput`, `LNDLError`, `get_lndl_system_prompt`, `extract_lndl_blocks`,
  `normalize_lndl_text`. `parse_lndl_fuzzy` stubbed pending Phase 2.
- **Custom render/parser protocols Phase 1** (#1092) — `CustomRenderer`, `CustomParser`,
  `StructureFormat`, `validate_image_url`, pure-function `prepare_messages_for_chat`.
  Phase 2 (manager.py refactor + `branch.operate(renderer=...)` wiring) deferred.
- **Studio: project chips on invocations rows** (#1081). Schedules + runs already had them.
- **Studio a11y baseline (4 of 6 tracks)** (#1020):
  - Track 1 — programmatic labels + `aria-describedby` on form inputs
  - Track 2 — `--content-muted` and `dag-assign-text` contrast lifted to WCAG AA (4.5:1)
  - Tracks 3-4 — skip-to-main link, `aria-live` on status/loading regions, `aria-busy` on
    skeleton tables, `aria-pressed`/`aria-expanded` on toggle widgets
  - Tracks 5-6 — global `prefers-reduced-motion` guard, `eslint-plugin-jsx-a11y` installed
    (13 findings deferred via `TODO(#1020 follow-up)`).
- **Studio: ARIA tabs keyboard model** (#1040) — `RunStepCard` gains ArrowLeft/Right/Home/End
  with roving `tabIndex` per WAI-ARIA Tabs Pattern.
- **CI: marketplace skill validation** (#1031) — 175 parameterized tests across 35 marketplace
  MD files: canonical khive verbs, valid CLI subcommands, no banned models, no `nohup`.
- **API: public exports** (#1122) — 19 lndl + adapters symbols now importable from package root.
- **Tests: public-API smoke test** (#1134) — fails loudly on broken `lionagi/__init__.py`
  (no `importorskip` on the package under test).

### Fixed

- **`li kill --all-stale` scope** (#1117, codex-iter ×3) — was iterating only sessions+invocations
  despite docstring claiming all entity types. Now correctly limited to sessions + invocations
  (plays/shows excluded — orchestrators with no direct PID). Follow-up #1144 tracks smart
  child-derived staleness for plays/shows.
- **`anyio.NoEventLoopError` on resumed-codex teardown** (#1082) — cancelled-exception class
  cached at session start so the error path survives loop exit.
- **SIGINT bypassed shielded teardown in `run_async()`** (#1055) — signal-aware handler
  installed in parent thread; child loop is canceled cleanly so structured `finally:`
  finalizers run before exit.
- **Test timing-race patterns** (#1090, codex-iter ×3) — replaced `anyio.sleep(<small>)` sync
  points with `anyio.Event` and `TaskGroup.start(...)` `task_status.started()`. Behavioral
  assertions added alongside CI-tolerant relative timing bounds.
- **SSRF guard missed `::169.254.169.254`** (#1125, CWE-918) — IPv4-compatible IPv6 form was
  bypassing block-net check; now unmapped before lookup, same treatment as IPv4-mapped.
- **Hook subprocess errors silently swallowed** (#1127) — `B904`/`S110`: traceback chain
  preserved on `PermissionError`; warning logged in place of bare `except: pass`.
- **State layer imported CLI internals** (#1119) — `LIONAGI_HOME` extracted to
  `lionagi/_paths.py` leaf module; state no longer transitively pulls in CLI code.
- **Studio: 501-stubbed Run/New controls** (#983) — disabled in UI with hold-message
  pointing to CLI until backend implementation is designed.
- **CLAUDE.md described `li schedule` as shipped** (#1121) — reference removed; ADR-0027
  itself correctly remains "Proposed".

### Changed

- **Removed `pydapter` core dependency** (#1044) — `Adaptable`/`AsyncAdaptable`,
  `AdapterRegistry`, `JsonAdapter`/`CsvAdapter`/`TomlAdapter` inlined into
  `lionagi/adapters/`. `DataFrameAdapter` is now opt-in via `lionagi[pandas]`.
  `AsyncPostgresAdapter` retains `pydapter[postgres]` (it inherits from a substantial
  SQLAlchemy stack) and is opt-in via `lionagi[postgres]`.
- **Asyncio sweep Phase 1** (#1043) — `asyncio.Lock` → `lionagi.ln.concurrency.Lock` (×6
  call sites), `asyncio.sleep` → `anyio.sleep` (×1), `asyncio.gather` → anyio task group
  (×1). 9 Phase 2/3 sites tagged with `TODO(#1043 Phase 2)` for next pass.
- **CI matrix split** (#1069) — `pull_request` runs Python 3.10 + 3.14; `push` to main
  runs the full 3.10–3.14 matrix. ~60% PR wall-time reduction.

### Security

- **#1125** — IPv4-compatible IPv6 IMDS bypass in `lionagi/ln/_ssrf.py`. Cloud envs with
  IPv6-reachable IMDS could have been reached via `http://[::169.254.169.254]/…`.
- **#1127** — broken hooks now logged instead of silently swallowed.

### Internal

- **Polish**: Studio microcopy centralized in `lib/copy.ts` (#1088 merged pre-cycle).
- **Discovery**: `/discover-issues` filed 19 follow-up issues (#1117–#1135) — 8 resolved
  this cycle; the remainder live for the next sweep + decisions on canonical SIGINT
  reason code (#1118), README scope (#1120), ADR status taxonomy (#1129), custom render
  Phase 1 integration (#1130), and v0.21.0 deprecation purge strategy (#1131).

### Patch releases 0.26.1–0.26.6

These were Docker-distribution iteration releases:

- 0.26.1 — initial Docker release
- 0.26.2 — Docker fix iteration
- 0.26.3 — `fix(docker): use --legacy-peer-deps for npm install in Dockerfile` (#)
- 0.26.4 — `fix(docker): remove non-existent public/ dir from COPY step` (#)
- 0.26.5 — Docker build verified locally
- 0.26.6 — minor fixups + ADR-0028 Phase 1 (#1073), marketplace consolidation (#1070),
  ADRs 0028-0032 proposed (#1072), multi-arch Docker amd64+arm64 (#1080/#1093)

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
