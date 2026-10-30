# ADR-0122: Feature boundaries and optional runtime profiles

- **Status**: Accepted
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: governance
- **Date**: 2026-08-16
- **Relations**: depends on ADR-0119 (deterministic declaration and configuration substrate);
  constrains ADR-0027 and ADR-0030 (provider boundaries), ADR-0055 and ADR-0118 (StateDB),
  ADR-0062 (CLI ownership), ADR-0077 and ADR-0079 (Studio), and ADR-0088 (plugins)

## Context

LionAGI currently ships one Python distribution and imports one repository as if those were the
same architectural boundary. They are not. A distribution is a release and compatibility unit;
an import graph is an ownership rule; an installation profile is a dependency selection. Treating
all three as one decision has allowed SDK runtime, StateDB, providers, CLI, MCP, Studio, agent
harness, and orchestration code to reach into one another's implementation modules.

This record is in the **governance** area, rather than utilities, because it governs allowed
dependencies, feature ownership, release metadata, and merge gates across every package. It does
not introduce another helper library. ADR-0119 supplies the deterministic `Params`, `Spec`, and
explicit-registry substrate used to declare the feature manifest; this record decides how those
features may depend on and load one another.

At the evidence baseline, `origin/main` commit
`501d98abbfd55b8a0171c58b63ba671488cc77d7`, the Python package has 159,526 lines in
531 files. The feature-heavy `studio`, `cli`, `state`, `providers`, `mcp`, `tools`, `engines`,
`agent`, and `orchestration` packages account for 109,589 lines, or 68.7 percent. A consumer using
only the SDK should neither import those features nor install their infrastructure dependencies.
This measurement shows the size of the separable surface; it does **not** prove that 68.7 percent
of the code is redundant or should be deleted.

The default dependency list currently includes seventeen entries. SQLAlchemy, aiosqlite, psutil,
PyYAML, aiocache, and JSON repair are installed even when a consumer only needs in-memory
`Session`/`Branch` execution and supplies its own model endpoint. The `sqlite` extra is currently
a compatibility alias because aiosqlite is already mandatory. Conversely, optional dependency
handling is local and inconsistent: some modules defer imports, some catch a broad `ImportError`,
some name a raw package rather than a LionAGI feature, and some only fail deep inside an operation.

The source graph contains concrete inversions, not merely untidy folder names:

- State provenance and lifecycle-notification modules import CLI provider and `RunDir` helpers.
- Studio imports CLI argument, logging, run, status, project, and process-control internals.
- CLI scheduling imports Studio CLI, scheduler, and service internals.
- MCP roster, projection, dispatch, and job modules import CLI internals; MCP run-detail code
  imports Studio services.
- SDK runtime paths under `operations/run` import concrete provider implementations, including a
  provider-specific agentic runtime.
- Generic model-service code imports symbols owned by concrete provider modules.

Moving these files into more distributions before correcting the arrows would replace Python
import failures with cross-package version failures. It would also preserve duplicate business
logic: a shared helper located in `cli._util` does not become a contract merely because Studio and
MCP both import it.

The global settings object has the same problem at configuration time. One eagerly constructed
`AppSettings` contains provider secrets, StateDB configuration, caching policy, logging policy,
and agent-process timeouts. Importing a low-level SDK module can therefore parse configuration for
features the caller did not select. Import-time registries compound this by making the active
feature set depend on which optional module happened to load first.

| Concern | Decision |
|---|---|
| Import ownership | D1: one ordered layer graph defines every allowed production import. |
| Wiring | D2: CLI, MCP, and Studio are independent composition roots; optional implementations are injected through lower-layer contracts. |
| Installation | D3: the default install is the minimal SDK profile; explicit extras select state, provider, agent, orchestration, CLI, MCP, and Studio capabilities. |
| Loading and errors | D4: one feature loader and one typed missing-feature error distinguish an absent extra from a broken implementation. |
| Configuration and registration | D5: base and feature settings are resolved independently; registries are explicit immutable compositions from ADR-0119. |
| Enforcement | D6: static import rules, minimal-wheel tests, extra matrices, and import-side-effect tests are required merge gates. |
| Distribution shape | D7: enforce profiles inside the existing distribution first; consider multiple distributions only after the graph is clean and independently releasable. |
| Compatibility | D8: retain documented import aliases, extra aliases, and console-command feature gates for a declared migration window. |

This record deliberately does not decide:

- which persistence entities exist, how they migrate, or which database owns a deployment;
  ADR-0118 owns those decisions;
- provider request/response semantics, action authorization, native harness policy, Run identity,
  hook phases, or orchestration scheduling;
- deletion by line-count quota. Replaced implementations are deleted after parity, while tests,
  contracts, and necessary adapters remain;
- a new plugin marketplace or arbitrary third-party dependency resolver;
- the names or repository layout of future distributions. No package split is approved here;
- removal of Pydantic or the existing public SDK modeling contract;
- making imports silently degrade. An unselected feature fails at its activation boundary with an
  actionable error; only explicitly best-effort behavior may degrade.

## Decision

### D1 — A lower-to-higher layer order is the import law

The target order is:

```text
foundation -> contracts -> SDK runtime -> optional feature adapters -> composition roots
```

The arrow means “may be consumed by the layer on its right.” A module may import only its own
layer or a layer to its left. A lower layer never imports a higher one. Imports used only under
`TYPE_CHECKING`, string annotations, dynamic imports, and module-level `__getattr__` are still
dependency edges and receive the same review; hiding an inversion from an AST check does not make
it valid.

The layers own the following concerns.

**Foundation**

- sentinel semantics, `Params`/`DataClass`, `Spec`/`Operable`, internal concurrency,
  serialization, schema primitives, and dependency-free collection/identity mechanics;
- no StateDB, provider, agent, CLI, MCP, Studio, or orchestration imports;
- no environment parsing, filesystem discovery, network client construction, registry mutation,
  or process-global logger/storage creation at import time.

**Contracts**

- provider-neutral ports, invocation and result DTOs, store ports, feature descriptors, and
  protocol-level errors;
- immutable declarations and projections built on ADR-0119;
- no SQLAlchemy objects, HTTP clients, subprocess providers, web framework types, CLI parser
  types, or frontend models.

**SDK runtime**

- in-memory `Session`, `Branch`, operation composition, messages, graph execution, and model/tool
  invocation through contracts;
- a caller can construct and exercise these paths with injected test/in-memory adapters on the
  minimal installation;
- runtime chooses a capability by a passed contract or a composed registry key, never by importing
  a concrete provider or inspecting whether its module happens to be importable.

**Optional feature adapters**

- concrete providers, StateDB, native agent harnesses, orchestration integrations, authoring
  formats, and other feature implementations;
- each adapter imports foundation, contracts, and SDK runtime as required, but does not import a
  sibling implementation merely to reuse a helper;
- shared semantics needed by two adapters move down to a neutral contract or foundation utility;
  feature-specific coordination moves up to a composition root;
- StateDB does not import a provider implementation; providers do not import StateDB; agent and
  orchestration adapters receive both through ports.

**Composition roots**

- CLI, the `li mcp` server, and Studio each assemble a selected application from contracts and
  feature adapters;
- composition roots may depend on lower layers but not on another composition root's
  implementation;
- CLI and Studio share command/request DTOs and application services below both roots. They do
  not import one another;
- MCP protocol code and Studio API code may expose the same application service but do not call
  each other's route or parser helpers;
- the Studio frontend consumes generated or contract-tested wire projections and is never the
  authority for backend lifecycle or feature availability.

This order is an ownership rule, not necessarily one directory per layer. Existing public module
paths may remain as compatibility facades. A move is justified only when the destination has the
right owner and callers are migrated; file motion alone is not progress.

### D2 — Composition roots own selection; contracts own reuse

Feature selection is explicit at an application boundary:

```python
application = compose_application(
    runtime=runtime,
    providers=(openai_adapter,),
    state=sqlite_state,
    harness=local_harness,
)
```

The exact public builder may differ, but these rules are binding:

1. imports do not activate a provider, database, hook, plugin, or background service;
2. adapters return declared registry fragments and settings specs without mutating globals;
3. the composition root validates fragment names, dependency requirements, and collisions before
   it constructs the application;
4. runtime receives the resulting immutable registry or specific port, not an import path into a
   feature implementation;
5. the same feature manifest drives CLI availability, MCP capability reporting, Studio health,
   diagnostics, and missing-extra messages;
6. tests can compose two isolated applications with different feature sets in one process.

Cross-root helpers are classified before they move:

- pure argument/schema parsing belongs to a contract or authoring adapter;
- reusable business operations belong to an application-service module below the roots;
- terminal rendering and process exit codes remain CLI presentation;
- HTTP request/response conversion remains Studio presentation;
- MCP schema/transport conversion remains MCP presentation;
- OS process control belongs to a harness/system adapter, not `cli._util`;
- provider profile discovery belongs to provider configuration, not CLI.

This removes the current CLI-as-library and Studio-as-library patterns without creating a
`common.py` dumping ground.

### D3 — Runtime profiles are explicit installation contracts

The first implementation remains one `lionagi` distribution. Its default dependency set and
import behavior define the **minimal SDK profile**:

- foundation, contracts, and SDK runtime;
- in-memory stores and deterministic test adapters;
- public modeling and serialization behavior required by the SDK;
- no SQL database, web server, CLI process management, MCP server, agent subprocess, workflow
  authoring, provider-specific package, cache backend, or visualization dependency.

Feature extras are additive. The final names are checked against existing extras during Phase 0,
but their semantic owners are fixed:

| Profile owner | Capability selected | Candidate dependencies removed from default |
|---|---|---|
| `state` | SQLite StateDB and persistence adapters | `sqlalchemy[asyncio]`, `aiosqlite` |
| `postgres` | PostgreSQL StateDB adapter in addition to `state` | `asyncpg`, PostgreSQL adapter stack |
| provider adapters | first-party HTTP or provider-native implementations | provider/client-specific packages; common HTTP stack if no base path needs it |
| `agent` | native coding-agent and local process adapters | process/provider-specific libraries, `psutil` where required |
| `orchestration` | flow/play/fanout application adapters and authoring | feature-specific YAML/process dependencies |
| `cli` | `li` command implementations and terminal UX | CLI-only diagnostics and rendering dependencies |
| `mcp` | MCP client/server surfaces | `fastmcp`, `mcp`, transport floors |
| `studio` | API, scheduler, operator, and web-server composition | FastAPI, Uvicorn, Starlette, cron and Studio HTTP dependencies |
| `reader`, `graph`, `schema`, and similar existing extras | existing leaf capabilities | their current third-party libraries |
| `all` | every supported first-party profile | union of declared extras |

An extra may depend on another extra in packaging metadata, for example `postgres` including
`state`. At runtime, however, feature dependency is validated from the explicit manifest rather
than inferred by successfully importing a transitive package.

Three dependencies are first-wave candidates because their imports are already localized:

- `aiocache` is imported lazily only by endpoint caching and becomes a cache capability rather
  than a base requirement;
- `json-repair` is an optional Claude transcript fallback and belongs to that provider/harness
  adapter;
- `packaging` is imported by plugin manifest version checks and belongs to plugin activation.

The next candidates require boundary extraction before metadata changes:

- SQLAlchemy and aiosqlite move only after SDK/session modules no longer import StateDB;
- psutil moves only after process liveness/control is owned by CLI or harness adapters;
- PyYAML moves only after agent settings, plugin manifests, playbooks, and Studio authoring share a
  declared authoring adapter rather than importing each other's parsers;
- `pydantic-settings` and `python-dotenv` move or remain only after base settings are separated
  from provider, state, agent, and Studio settings;
- tiktoken and the HTTP stack remain default only if the accepted minimal SDK contract directly
  requires them. A provider-specific convenience path is not sufficient justification.

The Phase 0 dependency inventory records, for every third-party requirement, its importing
modules, owning feature, eager/lazy state, public behavior, and test environment. `pyproject.toml`
is generated or checked against that ownership manifest. A dependency is not moved based only on
its name or wheel size.

### D4 — One loader reports absent features without hiding defects

ADR-0119's immutable declarations describe each selectable feature. Conceptually:

```python
@dataclass(frozen=True, slots=True, init=False, eq=False)
class FeatureSpec(Params):
    name: str
    extra: str | None
    requires: tuple[str, ...]
    import_roots: tuple[str, ...]
    exports: tuple[str, ...]
```

The shipped representation may add fields, but it may not become an import-time mutable catalog.
The distribution owns a deterministic base manifest; enabled adapters contribute explicit
fragments at a composition root.

Every optional activation uses the same typed failure:

```python
class MissingFeatureError(ImportError):
    feature: str
    extra: str
    missing_distribution: str
    requested_symbol: str | None
```

Its message is actionable and stable enough for CLI, MCP, and Studio adapters to project:

```text
LionAGI feature 'state' requires optional extra 'state';
install it with: pip install 'lionagi[state]'
```

The loader distinguishes absence from a broken implementation:

- it knows the top-level import names declared for the feature;
- a `ModuleNotFoundError` is translated only when its missing name belongs to that declared set;
- an `ImportError`, syntax error, version mismatch, or missing internal LionAGI symbol raised after
  dependencies are present propagates as an activation defect with its original cause;
- domain compatibility errors keep their existing typed identity. In particular, StateDB
  `SchemaTooNewError` is projected by CLI/MCP/Studio as `feature=state`,
  `reason=schema_too_new`, and its supported/observed versions; it is never rewritten as
  `MissingFeatureError` or an absent-detail success;
- catching broad `ImportError` and reporting “install the extra” is forbidden because it converts
  code regressions into misleading user action;
- best-effort discovery may omit an unselected feature, but explicit access never returns `None`,
  an empty registry, or a partially initialized adapter.

Root-level public lazy exports remain supported. Accessing an export for an uninstalled feature
raises `MissingFeatureError`; importing `lionagi`, its foundation, contracts, or SDK runtime does
not probe optional modules. `find_spec()` or package-metadata probing may be used by a diagnostics
command, never by runtime as a substitute for explicit composition.

### D5 — Configuration and registries follow feature ownership

The base settings model contains only values required to construct the minimal SDK. Feature
settings live with their feature and are resolved only when selected:

```text
BaseRuntimeSettings
ProviderSettings fragments
StateSettings fragment
HarnessSettings fragment
CliSettings / StudioSettings at their composition roots
```

Existing environment-variable names remain accepted during migration. A compatibility resolver
maps them to the owning feature and warns only when a value is ambiguous or deprecated; it does
not instantiate every feature settings model to discover whether a variable exists. Secret values
remain redacted in errors, manifests, and diagnostic output.

`FeatureSpec`, settings fragments, provider registries, entity registries, hook registries, and
tool registries use ADR-0119's explicit deterministic composition:

- optional modules expose declarations but do not self-register;
- fragment order is declared and stable;
- duplicate names fail before application startup and report both owners;
- serializing an application profile produces a stable, redacted capability snapshot;
- unresolved `Unset` configuration does not cross the feature activation boundary;
- two profile compositions in the same process do not share mutable registry or settings state.

Importing a module may define classes and constants. It may not open a database, create a
filesystem logger, parse a project, read provider credentials, start a thread/task, discover
plugins, or mutate the active application.

### D6 — Boundaries are executable merge contracts

The repository adds four complementary gates.

**Static import graph**

- A checked manifest maps production modules to the five layers and optional feature owners.
- An AST-based test rejects an import of a higher layer, a sibling adapter implementation, or a
  different composition root.
- Dynamic import literals and lazy-export maps are included in the check.
- Temporary exceptions name an issue, owner, and removal phase; a wildcard allowlist is invalid.
- The gate also rejects imports of raw `asyncio` concurrency helpers, generic JSON libraries, or
  external schema helpers where the house-rule internal library owns that concern.

**Minimal wheel environment**

- CI builds the wheel and installs it into a fresh environment with no extras and no repository
  source path.
- The test imports every documented foundation, contract, and SDK module, constructs a Session
  with in-memory/test adapters, executes a deterministic no-network operation, and serializes the
  result.
- `sys.modules` and import tracing prove that SQLAlchemy, aiosqlite, FastAPI, MCP, Studio, CLI,
  provider-specific adapters, psutil, and YAML authoring modules were not loaded.
- importing `lionagi` performs no network, database, filesystem-write, environment-discovery, or
  background-task side effect.

**Feature matrices**

- Each supported extra has a fresh-wheel job that activates its feature and runs its contract
  suite.
- Selected combinations cover declared dependencies (`postgres` plus `state`) and composition
  roots (`studio` plus the adapters it enables).
- A no-extra job accesses one representative export per absent feature and asserts the exact
  `MissingFeatureError` fields and installation hint.
- A broken-feature fixture raises an internal `ImportError`; the test proves it is not mislabeled
  as an absent dependency.
- `pip check` and the lock/constraints policy run for the minimal, each leaf, and `all` profiles.

**Compatibility and purity**

- Public root and historical module imports have explicit compatibility tests.
- Import cycles are forbidden even where Python happens to resolve them under a particular order.
- Two isolated application compositions are tested in one process.
- Reload and differing import order produce the same feature manifest and registry hash.
- `PYTHONHASHSEED` variation produces the same manifest, relying on ADR-0119's order and
  serialization guarantees.
- Base import time and imported-module count are recorded as regression metrics. They are budgets
  with reviewed thresholds, not substitutes for the semantic forbidden-module assertions.

A source move, dependency reclassification, or new root export cannot merge without updating the
manifest and the applicable gates.

### D7 — One distribution first; a package split requires new evidence

The implementation sequence keeps the `lionagi` distribution and top-level namespace intact
while it establishes the dependency graph, profile manifest, extras, and tests. All feature code
may therefore remain in the wheel during the first phases, but it is neither imported nor backed
by unselected third-party dependencies.

The decisive fact is that the most obvious split line is not merely inverted, it is circular.
Measured on this record's baseline, 14 files under `lionagi/studio/` import from `lionagi.cli`,
and 3 files under `lionagi/cli/` import from `lionagi.studio`
(`lionagi/cli/mirror.py`, `lionagi/cli/machine_schedule.py`, `lionagi/cli/main.py`). A one-way
inversion could be repaid by moving a contract to a lower layer. A cycle cannot: no ordering of
two distributions satisfies it, so cutting there first converts a Python import error, which
fails at the first import and names the module, into an unsatisfiable version constraint between
two published packages, which fails at resolution time and names neither. Breaking the cycle is
therefore a precondition of gate 1 above rather than a task the split can carry.

This distinction is deliberate:

- optional dependencies reduce resolver surface, installation failures, vulnerability exposure,
  and environment conflicts;
- lazy, side-effect-free modules reduce startup cost and accidental activation;
- import contracts enable deletion and independent ownership;
- only a later distribution split reduces the bytes of feature source shipped in the wheel.

A follow-up ADR may propose multiple distributions only when all of these gates are true:

1. the static graph has no exceptions across the proposed boundary;
2. every cross-boundary interaction is a versioned public contract rather than an implementation
   import;
3. minimal and feature-extra wheel jobs have been stable through a documented release window;
4. the release pipeline can build, test, publish, roll back, and constrain compatible versions of
   every proposed distribution atomically enough for supported upgrades;
5. duplicate helpers and business authorities have already been removed, so the split does not
   fossilize them;
6. measured wheel footprint, release cadence, security isolation, or independent ownership shows
   a benefit beyond what extras and lazy loading provide;
7. editable installs, source checkouts, plugins, type checking, documentation, and downstream
   imports have an explicit migration plan.

If a split is later accepted, `lionagi` remains the compatibility/meta distribution for at least
one documented deprecation window. This ADR reserves no names such as `lionagi-core` and does not
approve namespace-package machinery.

### D8 — Compatibility is explicit and finite

The migration preserves user intent while allowing the default dependency contract to become
smaller:

- public symbols retain their current root/module paths through thin lazy compatibility facades
  when their semantics are unchanged;
- facades contain no business logic and are deleted after the published deprecation window;
- `lionagi[all]` remains the supported union profile;
- existing extras such as `sqlite` remain aliases where necessary and point users to their
  canonical successor;
- the `li` console entry point may remain installed by the base wheel, but when CLI dependencies
  are absent it performs only enough dependency-free dispatch to raise the actionable `cli`
  feature error. It does not crash while importing the parser;
- code that previously relied on an undeclared transitive dependency receives release-note and
  diagnostic guidance. LionAGI does not keep that dependency in the default set indefinitely to
  preserve accidental availability;
- provider and StateDB convenience constructors keep their signatures while delegating to an
  activated adapter, or fail at their public boundary with `MissingFeatureError`;
- environment variables and serialized profile names receive explicit aliases; persisted data is
  not rewritten merely because Python ownership moved;
- profile capability reports expose `available`, `selected`, and `reason` without leaking secrets
  or treating package presence as authorization.

The first release that removes a dependency from the default set must include a migration table
from the affected public surface to its installation extra. Because LionAGI is pre-1.0, this can
be a minor release, but it is still a deliberate compatibility change.

## Implementation plan

Implementation remains blocked while this ADR is Proposed.

### Phase 0 — Characterize without moving behavior

1. Freeze the public import, console-command, environment-variable, and extras compatibility
   fixtures at the baseline commit.
2. Build the production import graph, including lazy maps, dynamic import literals, and optional
   imports.
3. Assign every production module and third-party dependency to one owner and layer.
4. Record import-time filesystem, environment, registry, logger, network, database, and background
   task side effects.
5. Enumerate direct cross-root and sibling-adapter imports with a target owner for every shared
   symbol.
6. Add the static checker in report-only mode; every existing exception gets a concrete follow-up
   issue rather than a package-wide allowlist.

No production package moves, dependency removals, public-export deletions, or registry rewrites
occur in this phase.

### Phase 1 — Extract contracts and remove inversions

1. Land ADR-0119's deterministic feature/registry substrate.
2. Move provider profiles, pure request schemas, reusable application services, process-control
   ports, and store ports to their declared owners.
3. Replace runtime imports of concrete providers with injected contracts.
4. Replace StateDB imports of CLI/provider implementation helpers with neutral values or injected
   ports.
5. Replace CLI/Studio/MCP cross-imports with shared contracts or application services below the
   roots.
6. Delete each old implementation in the same change that proves its compatibility facade.
7. Drive the static checker exception count to zero before changing default dependencies.

### Phase 2 — Introduce profiles and loading contracts

1. Add the immutable feature manifest and typed loader.
2. Split base and feature settings while retaining environment aliases.
3. Make root-level lazy exports and the `li` bootstrap use `MissingFeatureError`.
4. Add minimal-wheel, feature-matrix, broken-import, side-effect, and dual-composition tests.
5. Keep the existing default dependency set during this phase so behavior changes are attributable
   to loading and ownership, not resolver changes.

### Phase 3 — Optionalize dependencies in measured waves

1. Move aiocache, JSON repair, and plugin version parsing dependencies first.
2. Move StateDB dependencies after the state boundary and minimal Session test pass.
3. Move psutil, YAML, settings, tokenization, HTTP, and provider dependencies according to the
   Phase 0 ownership result, not as one bulk metadata edit.
4. For each wave, build and test the minimal wheel, affected extras, `all`, and at least one
   supported upgrade from the previous release.
5. Update install docs and compatibility diagnostics in the same change.

### Phase 4 — Delete compatibility-only structure

1. Remove deprecated import facades and extra aliases only after their declared window.
2. Remove obsolete global settings fields and import-side-effect registration paths.
3. Delete dependency checks duplicated outside the feature loader.
4. Re-measure production lines, wheel sizes, dependency counts, import time, and forbidden edges;
   report deletions separately from moves and generated artifacts.

### Phase 5 — Re-evaluate distributions

Apply D7's evidence gate. If it passes and a split has measured value, write a new ADR with exact
distribution names, ownership, version constraints, release/rollback mechanics, and compatibility
period. If it does not pass, remain one distribution; that is a valid end state.

## Acceptance criteria

This ADR is implemented only when:

1. every production module and third-party dependency has one declared owner and layer;
2. the layer checker reports zero unapproved upward, sibling-implementation, and cross-root edges;
3. StateDB imports no CLI or concrete provider module;
4. generic SDK runtime imports no concrete provider implementation;
5. CLI, MCP, and Studio import no other composition root's implementation;
6. a fresh minimal wheel imports all documented SDK surfaces and runs the in-memory smoke contract
   without optional modules installed or imported;
7. every supported profile installs from the built wheel, passes `pip check`, activates through
   the common loader, and passes its contract suite;
8. absent dependencies produce `MissingFeatureError` with feature, extra, missing distribution,
   requested symbol, and an actionable installation command;
9. internal import defects are never rewritten as missing-extra errors;
10. an installed State adapter raising `SchemaTooNewError` retains that type/cause through SDK,
    CLI, MCP, and Studio activation projections and is distinguishable from an absent extra;
11. two different profiles compose independently in one process and import order does not change
    their registry hashes;
12. importing the base package performs no feature discovery or external side effect;
13. existing public paths and extras either pass compatibility tests or have an accepted,
    documented deprecation;
14. dependency ownership and `pyproject.toml` cannot drift without a failing test;
15. production source decreases over completed migration waves after moves and generated files are
    reported separately;
16. no multi-distribution split occurs without the follow-up ADR and D7 evidence.

## Consequences

### Positive

- SDK consumers install and load only the infrastructure needed for their selected capabilities.
- A missing optional feature becomes a stable, diagnosable condition instead of an arbitrary deep
  import failure.
- CLI, MCP, and Studio become adapters over shared application contracts rather than libraries for
  one another.
- Provider, state, agent, and orchestration implementations can evolve without pulling their
  dependencies into the base runtime.
- Import purity and explicit composition make test isolation, deterministic registries, and later
  package splitting practical.
- Architectural deletion becomes reviewable: a replaced authority disappears in the same wave
  that proves the replacement.

### Negative

- Install instructions become more explicit, and users of previously bundled features must select
  an extra.
- CI grows a wheel/profile matrix and must maintain several dependency environments.
- Compatibility facades temporarily increase indirection before they are removed.
- Moving shared helpers to their true owner requires domain judgment; an automated directory split
  cannot complete the work.
- One distribution continues to ship optional feature source until a later ADR justifies a split.
- Feature-scoped settings and composition add explicit wiring that a global settings singleton hid.

### Risks and mitigations

- **Risk: a broad `ImportError` is mislabeled as a missing extra.** Mitigation: translate only
  declared missing top-level packages and test a deliberately broken feature.
- **Risk: extras form another undocumented dependency graph.** Mitigation: one checked feature
  manifest drives packaging, diagnostics, and combination tests.
- **Risk: public imports trigger optional side effects through compatibility facades.** Mitigation:
  facades are lazy, business-logic-free, and covered by the minimal-wheel denylist.
- **Risk: configuration aliases keep the global object alive forever.** Mitigation: aliases have a
  published window and resolve directly to feature settings without constructing other features.
- **Risk: module moves inflate apparent deletion.** Mitigation: report moved, deleted, generated,
  test, and production lines separately.
- **Risk: a distribution split is treated as the goal.** Mitigation: D7 makes one clean,
  profile-driven distribution an acceptable final architecture.

## Issue relationships

The consolidation snapshot initially grouped #2152, #2367, #2727, #2966, #3044-#3049,
and #3085-#3087 under a broad modularity/release/quality heading. Review narrows that
relationship:

- #2152 is a Python 3.14/xdist worker-crash report and merges into the #1679 CI reliability
  umbrella. It is not a module-boundary task.
- #2367 is plugin shadow-diagnostic/cache-collision correctness. It remains standalone adapter
  work, while its tests become useful evidence for explicit plugin activation.
- #2727 remains the release-gating defect that publish tests differ from CI and the hosted
  frontend does not advance with a release. Its resolution is an acceptance prerequisite for
  optional-profile cutover and any later multi-distribution proposal, not a child implementation
  of this ADR.
- #2966 has already been fixed by #3006 and #3219. Verify its detector against current main and
  close it; this ADR does not create a replacement code change.
- #3044 is the comment/docstring-trimming umbrella. Merge its mechanical children #3045-#3049
  into it and remeasure only after architectural deletion. Those deleted lines do not demonstrate
  boundary consolidation.
- #3085 (deliberate sleeps), #3086 (duplicate tests), and #3087 (tautological/mock-self tests)
  remain standalone test-quality work. They are not packaging tasks and their test deletion is not
  production-code reduction.
- Boundary-adjacent #1175, #2048, #2367, and #2779 must consume the accepted profile/adapter
  contract where applicable, but are not absorbed into the modularity epic.
- None of these issues independently authorizes a package split or a new cross-layer dependency.
- A new implementation epic is opened only after this ADR is accepted. Its children mirror
  Phases 0-4: dependency inventory, import-contract enforcement, inversion removal, feature
  manifest/loader, settings split, minimal-wheel CI, and dependency migration waves;
- any issue that moves a module must name its old owner, target owner, compatibility projection,
  forbidden edge removed, and deletion/parity test. “Move package” is not an independently
  sufficient acceptance criterion.

## Alternatives considered

### Split immediately into `core`, `state`, `cli`, and `studio` distributions

This would reduce individual wheel contents quickly. It lost because the current graph contains
implementation imports in both directions, so a split would create cyclic release dependencies
and coordinated-version failures while preserving duplicate ownership.

### Keep every dependency mandatory and rely only on lazy imports

This preserves existing install behavior and improves startup. It lost because SDK consumers still
receive resolver conflicts, installation surface, and vulnerabilities for unselected features.

### Make every internal package an optional extra

This appears maximally modular. It lost because extras are user capability profiles, not a mirror
of the source tree. Fine-grained extras would expose implementation layout and create an
unmaintainable combination matrix.

### Infer features from installed packages

This avoids a manifest. It lost because package presence does not express user selection,
authorization, adapter configuration, or registry order, and it can change behavior when an
unrelated dependency happens to be installed.

### Catch all import failures and suggest `lionagi[all]`

This is simple support guidance. It lost because it hides real code and version defects, defeats a
minimal install, and makes `all` the operational default again.

### Create a shared `common` package for every cross-imported helper

This would remove visible CLI/Studio imports rapidly. It lost because it classifies by reuse rather
than ownership and would become a new miscellaneous dependency hub. Contracts, application
services, presentation adapters, and system adapters have different owners even when each has two
callers.

### Keep one global settings object and make all fields optional

This minimizes constructor changes. It lost because import-time environment parsing and feature
coupling remain, and “optional” values do not establish who resolves or validates them.
