
# Changelog

All notable changes to lionagi are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- `log_tool_call` in `lionagi.agent.hooks` and `lionagi.hooks.builtins` — canonical name for the tool-call observability post-hook, replacing `log_tool_use`. Also name-addressable via `lionagi.hooks.loader` registry as `"log_tool_call"`.
- `lionagi.testing` is now documented as a supported public surface. Register `lionagi.testing.pytest_plugin` in your `pytest_plugins` to get the bundled fixtures.

### Changed

- The CLI, studio, and operations boundaries now raise typed `LionError` subclasses (`ConfigurationError`, `OperationError`, `ExecutionError`) instead of bare `ValueError`/`RuntimeError`. These subclasses also inherit from the corresponding builtin, so existing `except ValueError` / `except RuntimeError` handlers keep working.

### Deprecated

- `log_tool_use` in `lionagi.agent.hooks` and `lionagi.hooks.builtins` — use `log_tool_call` instead. Will be removed in a future minor release.
- `CLIEndpoint` in `lionagi.service.connections` — use `AgenticEndpoint` instead. The alias now emits `DeprecationWarning` at import. Will be removed in a future minor release.

### Removed

- `request_fields` phantom param dropped from `MessageManager.create_instruction`, `create_message`, and `add_message` (was accepted but silently ignored); `parse_lndl_fuzzy` removed from `lndl.__all__` (Phase-2 feature, not yet shipped).
- Internal `_get_oai_config` alias in `lionagi.testing._legacy` removed (was never public; callers within the module now call `oai_chat_endpoint_config` directly).

## [0.27.1] - 2026-06-16

The studio desktop substrate (Vite SPA + Tauri 2 macOS shell), custom engine
definitions driven from the frontend, a hardened launch API, and the
`AgentConfig` removal — fourteen commits since v0.27.0.

### Added

- **Studio desktop substrate** — Vite SPA migration with single-process serving
  (#1430) and a Tauri 2 desktop shell for macOS (#1431).
- **Studio custom engines** — create, save, and launch custom engine definitions
  from the frontend (#1441).
- **Studio launch API** — `POST /api/launches` fires runs through the
  scheduler's hardened spawn path (#1434).
- **Casts catalog** — read-only catalog API and CLI (#1433).
- **Live run streaming** — persist engine-run signals for live Studio streaming
  (#1432).

### Changed

- **Python 3.14 compat** — bump pyupgrade to v3.21.2 (#1442).

### Fixed

- **CI diagnostics** — fail fast with the crashed test's name when an xdist
  worker dies (#1437).
- **Studio frontend** — restore Tailwind content paths after the Vite migration
  (#1435).

### Removed

- **`AgentConfig`** — the deprecated `lionagi.agent.config` module is removed
  (#1449). Use `AgentSpec` (`lionagi.agent.spec`) instead:
  `AgentSpec.compose(role, ...)` for the general case and
  `AgentSpec.coding(...)` for the coding preset. `create_agent()` now takes an
  `AgentSpec`. The `HooksMixin` and secure-guard wiring previously in
  `config.py` now live in `spec.py`.

### Docs

- Trim verbose docstrings and comments across the codebase and relocate
  load-bearing reference material to `docs/` (#1450, #1451, #1452).

## [0.27.0] - 2026-06-11

Phase C control center (engine runs, CLI coding presets, studio maintenance),
a studio lifecycle-signal layer with live Kanban/playfield views and en/zh
i18n, inline-flow scheduling, the ADR-0079..0082 orchestration substrate
cluster, and an eight-issue security/bug discovery sweep.

### Added

- **Phase C control center** — `li engine run` + `engine_runs` visibility
  (#1415); `li agent --preset coding` + `--form spec.yaml` (#1418);
  `POST /api/admin/maintenance` (vacuum / checkpoint / prune) with page
  wiring (#1417).
- **Scheduler inline flow** — `flow_yaml` schedule action kind runs an inline
  YAML flow (#1265).
- **Studio lifecycle & live views** — canonical lifecycle-signal contract +
  lane projection (#1396); SSE lifecycle-signal stream + run event log (#1403);
  self-healing session/invocation lifecycle reapers (#1260); Kanban lifecycle
  view over run status (#1268); cross-project live playfield page (#1266);
  action panel, filter counts, play-graph color/click/zoom + a11y perf
  baseline (#1258).
- **Internationalization** — next-intl pipeline with en/zh catalogs + locale
  switcher (#1401); page conversions for kanban/playfield/dashboard + nav
  keys (#1402).
- **Orchestration substrate cluster** — ADR-0079..0082 + routing slice (#1267).
- **Work system re-cut** — `WorkForm` + `Rule` / `RuleSet` (#1386).
- **Coding-agent feedback surface** — guidance + ruff diagnostics (#1400).
- **Class-registry test suite** (#1375).

### Changed

- **Engine layering** — extract `ChainRun` intermediate base + research
  per-stage repair (#1385).
- **Module relocation** — `protocols/structure` → `operations/schema` (#1378);
  consolidate `Operative` construction + document HookPoint dispatch (#1377);
  split CLI spec-validation and state-db into dedicated modules (#1376); dedup
  `CodingToolkit` schemas to the tools/file canon + add the missing open
  action (#1372).
- **CI** — serialize gh-pages deploys with a concurrency group (#1395).

### Fixed

- **Registry fallback** — `LION_CLASS_FILE_REGISTRY` fallback now actually
  loads classes via package-context import, with a package-boundary guard
  against path escape (#1422).
- **Provider / engine diagnostics** — typed provider errors + emission
  diagnostics surface CLI worker failures (#1419); export a partial report on
  budget/deadline cancellation (#1399); deadline watchdog, normalize-before-gate
  and CLI-aware emission repair (#1371); gate runs on workspace ground truth
  despite implementer emission failure (#1365); fan collected chain events to
  `on_event` once across engines (#1362); honor `backoff_factor` in retry
  (#1370).
- **CLI providers** — map gemini NDJSON tool payloads + assistant answer into
  session state (#1390); gemini-cli env trust, oauth model set, clearer
  endpoint errors (#1374); codex surfaces the top-level error message instead
  of an empty-dict `str()` (#1388).
- **Session** — emit run lifecycle signals from `Branch.run`, fix ReAct
  double-wrap, fix resume empty stream (#1373).

### Security

- **Studio** — startup warnings for unauthenticated mode + a bounded CORS
  method allowlist derived from the route table (#1423).
- **Scheduler hardening** — reject flag injection in
  `action_model` / `action_extra_args` (#1414); validate `github_repo` format
  before GitHub API URL construction (#1420).
- **Bash tool** — remove the `allow_shell` bypass to close CWE-284 (#1421).
- **Dependencies** — urllib3 2.6.3→2.7.0, authlib 1.7.0→1.7.2 (#1384);
  codecov-action 6→7 (#1368); actions/cache 4→5 (#1369).

## [0.26.18] - 2026-06-09

Engine layer expansion — hypothesis and coding engines with autonomy
protections (ADR-0077), the casts module-coherence pass (ADR-0078), a
15-PR bug/security sweep, and a codebase-wide trim/consolidation.

### Added

- **HypothesisEngine** — hypothesis-driven development engine (Chain shape):
  frame → question → evidence → analyze, with per-run chain export (JSON +
  markdown evidence files). (#1358)
- **CodingEngine** — gated plan/implement/test/fix loop with a ground-truth
  subprocess test runner (pass = exit code 0, never an LLM claim), judge-gated
  fix rounds, and a `to_hypothesis_seeds` bridge into HypothesisEngine. (#1358)
- **Engine autonomy protections** (ADR-0077) — run-level resource budgets,
  judge gates, emission repair/retry for weak models, per-stage model routing,
  generation caps. (#1358)

### Changed

- **Module coherence pass** (ADR-0078) — normative casts model (pattern /
  role / mode / profile are configuration; agent is a runtime concept):
  single capability-grant source via `AgentSpec.emits`; CLI orchestrator and
  casts-role workers route through `create_agent` (one construction stack);
  provider tables moved down to `service/providers.py` and path constants to
  `lionagi._paths` (no more upward imports into `cli`); `AgentConfig`
  deprecated by delegation onto Profile/AgentSpec; plane-distinct vocabulary
  (`outcomes.Finding` → `ReviewFinding`, `ReviewVerdict` → `ReviewOutcome`);
  curated `casts` public surface; `EngineEvent` forbids extra fields. (#1358)
- **Docstring/comment trim** across session, models, service, providers,
  adapters, cli, operations, state, studio, hooks, engines, casts, tools,
  libs, config. (#1340–#1344)
- **Utility consolidation** — shared path-safety, `_io`/`_subprocess`, and
  NDJSON/CLI subprocess primitives; `ln.concurrency` adoption; render
  consolidation; CLI lifecycle/concurrency primitives + `last_response`.
  (#1345, #1346, #1351–#1355)
- **CI** — coverage on one version, uv caching, perf-test gating. (#1349)

### Fixed

- **ModelParams global cache** cross-wired distinct types — dropped. (#1356)
- **Pile set ops** — `items=` kwarg and `dict_values` crash. (#1316)
- **`iModel(model=...)`** resolves default provider from settings. (#1317)
- **PyYAML as core dependency** — `lionagi.agent` imports on base install.
  (#1315)
- **Studio run detail** reads StateDB like the list endpoint (was silently
  null for post-migration runs). (#1358)
- **ReviewEngine** uses structured concurrency instead of `asyncio.gather`.
  (#1322)
- lndl bool coercion (#1320), `CommonMeta` key-presence validation (#1325),
  `fuzzy_match_keys` Sequence handling (#1318), StateDB orjson serialization
  (#1326), `guard_destructive` default hook in coding presets (#1324), ruff
  format gate stragglers (#1314).

### Security

- **Studio API auth** — `GET /api/invocations` and `/api/sessions` gated
  behind bearer token. (#1319)
- **Docker symlink mounts** constrained to an allowlist. (#1323)
- **SSRF guard** — local-address allowlist for Ollama loopback endpoints.
  (#1327)
- **Credential redaction** across all URL schemes and nested dict details.
  (#1321)
- **Agentic-CLI path grants** validated against repo containment. (#1328)

## [0.26.17] - 2026-06-07

Security-hardening pass: fail-closed boundaries across MCP transports, file/exec
paths, and CLI providers, plus SSRF and credential-leak guards — alongside
the #1257 bug sweep.

### Security

- **Fail-closed MCP transports** — full inline MCP transport now fingerprinted
  into the policy key (closes a trust-leak where differing transports could share
  a policy entry); MCP transports fail closed under threaded access, with
  rate-limit deferral and processor-join fixes. (#1285, #1279)
- **Image-URL SSRF guard** — outbound image-URL fetches are validated and
  message construction hardened. (#1280)
- **Fail-closed path/exec boundaries** — tool path and exec boundaries reject on
  ambiguity instead of proceeding. (#1281)
- **CLI provider validation** — async correctness fixes plus fail-closed CLI path
  validation. (#1278)
- **Auth gating + credential redaction** — agent auth gating, spawn-constraint
  enforcement, and credential redaction across adapters/models. (#1282)
- **Artifact auth + input validation** — status-history integrity, artifact
  authorization, and input validation across state/cli/studio. (#1283)
- **Dependabot** — resolved 6 dependency vulnerabilities. (#1274)

### Fixed

- **Bug sweep** — pi-CLI parser events, SIGINT reason code, `li kill`
  PID-identity guard against recycled PIDs, lndl export. (#1257)
- **`os.killpg` on non-Unix** — guarded for platforms without process groups.
  (#1286)

### Changed

- **`lionagi/` ruff gate enabled** plus Studio UI alignment. (#1284)
- Ruff format + mechanical lint cleanup, no behavior change. (#1277)

### Docs

- README CLI section, ADR taxonomy, deprecation purge, migration-guide extract,
  DeepWiki integration. (#1259)
- Forbid internal audit/review references in committed code comments. (#1287)
- Align AGENT.md + CONTRIBUTING.md with actual tooling. (#1275)

## [0.26.16] - 2026-06-06

### Fixed

- **Codex silent failures** — `turn.failed`/`error` events were swallowed;
  now yield `StreamChunk(type="error")`. (#1272)
- **Codex `fast_mode`** — `service_tier=flex` → `service_tier=fast`. (#1272)

### Changed

- **Unified CLI provider types** — remove `ClaudeChunk`/`CodexChunk` and
  `ClaudeSession`/`CodexSession`; shared `CLISession` + `StreamChunk`
  passthrough. −255 lines. (#1272)

## [0.26.15] - 2026-06-02

Reactive orchestration, domain engines, observer-as-hook-transport.

### Added

- **Reactive self-expanding flow** — `SpawnRequest` injects nodes into a
  running DAG; CLI rewired onto casts + emissions.
- **Domain engines** (ADR-0075) — `PlanningEngine`, `ResearchEngine`,
  `ReviewEngine`; `li o flow` routes through `run_dag`.
- **Observer-as-hook transport** (ADR-0076) — emission/control/lifecycle
  extracted to `_observe.py`; API-model agents drive the bus.
- **Hook bus persistence** (ADR-0023b), pre-invoke governance gate,
  `li o flow --workers`, compositional filter DSL.
- **SWE-bench harness** — real instances, deterministic oracle, blind
  judge, per-dollar verdict.
- **Daytona sandbox** — isolated containers + context tool + tool guidance.

### Fixed

- **Roled agents lost tool-use** — `with_updates` dropped response schema.
- **Studio show-detail 404** in Docker + stale frontend build.
- **Daytona shell injection** — cwd now shell-quoted.
- **Codex `skip_git_repo_check`** defaulted to `True`.

## [0.26.14] - 2026-05-30

### Fixed

- **Dockerfile CMD target** — Docker image CMD still referenced the old
  `apps.studio.server.app:app` after the backend moved to `lionagi/studio/`
  in v0.26.13. Updated to `lionagi.studio.app:app`. Also added
  `MARKETPLACE_MANIFEST` fallback for pip-installed environments.
- **Dropped `[nlip]` optional dep group** — removed from pyproject.toml;
  users who need nlip install `ag2[nlip]` directly. Starlette pinned
  `>=0.46.2,<1.0` for FastAPI 0.115 compat.

## [0.26.13] - 2026-05-30

Universal AgentSpec, inline emission contracts, loop control, Studio in-wheel, and a large CLI/Studio bug-fix sweep.

### Added

- **Inline-Python roles/modes + emission contracts** — roles and modes are now a
  closed, built-in set defined in Python (`casts/roles/*.py`, `casts/roles/modes/*.py`),
  replacing the `.md` files and the string-keyed `ROLE_CAPABILITIES` dict. Each
  `Role` co-locates its emission contract (`emits`) with a 22-model emission
  ontology in `casts/emission.py`. Bad references are import errors, not silent
  `None`. (#1224)
- **Universal AgentSpec** — `AgentSpec` replaces `AgentConfig` as the primary
  agent creation surface. Composes `Profile` (frozen `Role + tuple[Mode]` with
  mode conflict detection) + model/tools/permissions/pack/emission grant.
  `from_legacy()` bridge preserves backward compatibility. Pack policy block
  rendered into system prompt. (#1227, closes #1212, #1213)
- **Loop control** — `LoopDirective` enum (CONTINUE/CANCEL/BREAK),
  `Branch.control()`/`poll_control()` one-shot API, `_check_control` seam in
  the `run()` stream loop. Observers can cleanly stop a running stream. (#1226)
- **`li schedule` CLI** — `li schedule list/get/create/enable/disable/trigger/
  delete/runs` wired to Studio REST API. Stdlib `urllib` only — no new deps.
  (#1218, closes #1165)
- **`li play` heartbeat/watchdog** — per-op heartbeat every 60s so flow.log
  never goes silent; 10-min idle stall warning; smart staleness for
  `li kill --all-stale` (child-derived sweep for plays/shows). (#1217,
  closes #1150, #1144)
- **`li play --help` common flags** — shows `--bypass`, `--team-mode`,
  `--timeout`, `--save`, `--cwd`, `--effort`, `--yolo`. (#1218, closes #1194)
- **Observe-by-role** — `RoleFilter` and `role=` keyword on
  `SessionObserver.observe`/`Session.observe`; subscribe to "anything emitted by
  role X" without enumerating types. `Signal.emitter_role` field. (#1219,
  closes #1208)

### Changed

- **Studio backend moved into `lionagi/studio/`** — ships with the Python wheel;
  `pip install lionagi[studio]` now includes the backend. `li studio` uvicorn
  target updated. Frontend stays in `apps/studio/`. (#1228, supersedes #1201)
- **Non-blocking observer dispatch** — `_emit_message_signal` scheduled as
  background `asyncio.Task`; all tasks drained in `finally`. Async handlers in
  `observer.emit()` run concurrently via `ln.gather` (structured concurrency)
  instead of serially. (#1219, closes #1214)
- **CLI orchestration wiring** — `FlowAgent` gains `modes`/`permissions`;
  planner roster built from casts ontology; plan validation for unknown
  roles/modes/permission presets; permission translation for claude_code
  provider. (#1227)
- **Dropped `[nlip]` optional dependency group** — `ag2[nlip]` removed from
  optional deps and `[sandbox]`; users who need nlip install `ag2[nlip]`
  directly. Starlette pinned `>=0.46.2,<1.0`. (#1218, closes #1133)

### Fixed

- **Codex stdin hang** — both codex and claude_code subprocess providers now
  redirect `stdin` to `DEVNULL`, preventing fd contention when multiple agents
  run concurrently. (#1216, closes #1158)
- **`--bypass` silently ignored** — `bypass=True` now injects provider-specific
  kwargs (codex: `bypass_approvals`; claude: `permission_mode`). (#1216,
  closes #1158)
- **Timeout discards partial output** — `_extract_partial_output()` preserves
  the last assistant message on hard timeout. (#1216, closes #1152)
- **Progress heartbeat** — 60s heartbeat when `--timeout` is set. (#1216,
  closes #1154)
- **`response_format` serialization** — `InstructionContent.to_dict` now
  includes `response_format` when it's a plain dict. (#1219, closes #1160)
- **Studio API_BASE 404** — `resolveApiBase()` fallback changed from `""` to
  `http://localhost:8765`. Fixes empty playbooks/skills pages. (#1220,
  closes #1215, #1157, #1156)
- **Studio MODEL column** — runs table cell count matched to header; Model
  column now renders. (#1220, closes #1167)
- **Studio dashboard staleness** — stale card hint corrected; show-status synced
  from filesystem; orphaned Health column removed; Projects breadcrumb and
  favicon fixed. (#1221, closes #1162, #1161, #1176, #1168)
- **`li monitor` type filter** — `--type play` now queries sessions with
  `invocation_kind='play'`; AGENTS column shows actual branch count;
  play↔monitor correlation via pre-generated UUID. (#1223, closes #1192,
  #1193, #1191)
- **Studio cwd** — `li studio` works from any directory (superseded by
  in-wheel move). (#1218, closes #1201)
- **Codex `service_tier`** — changed from `priority` (unsupported, caused
  hangs) to `flex`.

## [0.26.12] - 2026-05-29

Reactive capability bus + casts Role/Mode composition.

### Added

- **Reactive capability bus** (ADR-0072) — an agent's turn becomes an
  observable, typed event stream. A *capability* is a named, typed
  structured-output field an agent may emit inline as a fenced ```json block in
  its ordinary text; a `SessionObserver` dispatches each to handlers in real
  time, mid-run, without a dedicated emit tool. Built on lionagi's own
  primitives (`Element`/`Pile`/`Flow`/`Observer`). (#1204, #1206, #1207)
  - `Signal` / `StructuredOutput` envelopes, plus run-lifecycle
    `RunStart` / `RunEnd` / `RunFailed` signals.
  - **Filter DSL** (`lionagi.ln.types`): `TypeFilter`, `SpecFilter`,
    `FieldRef` (via `Spec.q`), composable with `& | ~`. Subscribe by type
    (`session.observe(Finding)`) or by named-field value
    (`session.observe(flower.q == "rose")`).
  - `branch.grant_capabilities(operable)` opts a branch into per-message
    emission and injects a schema-derived instruction block; the legality rule
    is `set(keys) ⊆ grant`, with over-grant attempts surfaced as an observable
    `CapabilityViolation`. `response_format` (strict final parse) and
    capabilities (per-message emission) remain orthogonal knobs.
- **Casts Role/Mode pattern** — a thin `Pattern` dataclass with a dense
  built-in role roster and a default operational pack; `AgentConfig` composes
  `role` + `modes` into the system prompt. (#1200, #1202, #1205)
- **`Structure` / `JsonStructure`** (`protocols/structure/`) — composable
  schema builders wrapping `Operable`, handling both `BaseModel` and `dict`
  response formats as first-class citizens. Wired through `ChatParam` /
  `ParseParam`; `communicate()` and `run_and_collect()` extract the structure
  from the instruction as the single source of truth for rendering + parsing.
  (#1159)
- **`lionagi.testing`** module — test infrastructure shipped with the library
  so the CLI and downstream consumers can be tested without real API calls:
  `ScriptedEndpoint` (`provider="scripted"`), `ScriptModel` (YAML/JSON/dict
  fixtures with positional + `when:` matching), and `TestBranch` factories.
  Consolidates 20+ ad-hoc inline mocks. (#1151)

### Changed

- **`InstructionContent` refactored** to delegate rendering/parsing to an
  auto-created `JsonStructure` when `response_format` is set. Removes the
  internal `_schema_dict`, `_model_class`, `custom_renderer`,
  `structure_format`, `render()`, `response_model_cls`, and `schema_dict`
  members. Public `branch.operate`/`communicate` behavior is unchanged. (#1159)

### Fixed

- **`Pile.include` dropped falsy `Observable` items** — items whose truthiness
  is `False` were silently skipped on include. (#1203)

### Docs

- Governance ADR series 0053–0070, charter DSL v0, and governance standards;
  OSS-purity cleanup of the ADR corpus. (#1185, #1190)

## [0.26.11] - 2026-05-25

### Fixed

- **`li play` AttributeError on artifact contract resolution** — `flow.py`
  referenced `env.agent_profile` in two places (lines 643 + 648) but the
  `OrchestrationEnv` dataclass field is named `orc_profile`. The bug was
  introduced by #1083 (ADR-0029 artifact contract). It only triggered when
  an explicit agent name was passed via `--agent`, so the orchestrator
  tried to look up artifact defaults on the orchestrator profile. Now
  references the correct `env.orc_profile`.
- **Regression test added** — `tests/cli/orchestrate/test_orchestration_env_attrs.py`
  parses `flow.py` for every `env.<public_attr>` access and asserts each
  matches a real `OrchestrationEnv` field or method. Prevents future typos
  of this exact shape.

## [0.26.10] - 2026-05-25

Studio launcher: auto-mount symlink targets in `~/.lionagi/*/` so the Library tab works for users with symlinked content.

### Fixed

- **`li studio` launcher — dangling symlinks inside container** — many power-user
  setups symlink `~/.lionagi/agents/*.md`, `~/.lionagi/skills/*`, etc. to
  content living elsewhere on the host (e.g. a `firm/` content repo). When
  `~/.lionagi` is bind-mounted into the container, those symlinks point at
  paths the container can't see — ENOENT → empty Library / Agents / Skills /
  Playbooks / Teams views. The launcher now walks `~/.lionagi/{agents,skills,
  playbooks,teams}`, resolves any symlinks via `Path.resolve(strict=True)`,
  and adds read-only bind mounts for each unique target parent directory so
  the symlinks resolve identically inside the container.

## [0.26.9] - 2026-05-25

Hotfix for empty Studio Library / Skills / Agents views in the Docker image.

### Fixed

- **Studio Docker image — empty Library tab** — `apps/studio/server/services/plugins.py`
  resolves `MARKETPLACE_DIR = _REPO_ROOT / "marketplace"` and
  `MARKETPLACE_MANIFEST = _REPO_ROOT / ".claude-plugin" / "marketplace.json"`,
  where `_REPO_ROOT` is `/app` inside the container. The Dockerfile previously
  COPYed only `lionagi/`, `apps/studio/server/`, and the built frontend — the
  bundled marketplace and plugin manifest were missing from the image. Now
  copied at `/app/marketplace/` and `/app/.claude-plugin/`.

- **`li studio` launcher — third-party plugins** — also mounts
  `~/.claude/plugins:/root/.claude/plugins:ro` (when the host directory exists)
  so Studio can enumerate Claude Code third-party plugins from the user's
  cache, not just the bundled marketplace.

### No Python package changes

The Python package surface is identical to v0.26.7. This bump exists only to
publish a corrected Docker image and an updated `li studio` launcher.

## [0.26.8] - 2026-05-25

Hotfix for the Lion Studio Docker image.

### Fixed

- **Studio Docker image — `/api/*` 404s** — the v0.26.7 image baked
  `NEXT_PUBLIC_STUDIO_API_BASE=""` into the client bundle at build time. Because
  `NEXT_PUBLIC_*` vars are resolved during `next build`, the empty string
  became the literal URL prefix in production — `fetch('/api/runs')` hit the
  Next.js server on `:3000` (which has no `/api` route) and returned 404 for
  every backend call. The Dockerfile now bakes
  `NEXT_PUBLIC_STUDIO_API_BASE="http://localhost:8765"` so the browser reaches
  the FastAPI backend through the host port mapping.

- **`lib/api.ts::resolveApiBase()`** — defense in depth: treat empty string as
  "not configured" (`if (env)` instead of `if (env !== undefined)`) so the
  runtime port-based fallback can recover if the env var is ever accidentally
  baked as empty again.

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
