# ADR-0136: Moving the packages built on the core into lionhub, and removing their old import paths in 2.0

- **Status**: Accepted (2026-10-25), amended 2026-10-25 (see Amendments 1 and 2); Amendment 3
  (2026-10-31) is Proposed
- **Kind**: Aspirational
- **Implementation-status**: partial. Wave 1 (D6) has landed for its four packages: #3752 adds the
  `lionhub` root and the `lionagi._moved.MOVED` table and its finder, and moves
  `lionagi.orchestration`; #3753 moves `lionagi.engines`; #3754 moves `lionagi.dispatch` and
  `lionagi.work`, with ADR-0001 Amendment 2 for D5's class paths. `integrations` is held (D6). Wave 2
  has begun: #3769 moves `lionagi.mcp` to `lionhub.serve.mcp` (Amendment 1 A4 item 1). The rest of
  wave 2, wave 3 (D6) and the removal release (D4) remain.
- **Area**: governance
- **Date**: 2026-10-25
- **Relations**: follows ADR-0122 D1 (layer order), D7's interim single-distribution rule and
  distribution gates, and D8 (compatibility is explicit and finite); amends ADR-0122 D7's post-split
  compatibility-distribution requirement as stated in D1; is the explicitly versioned compatibility
  decision that ADR-0125 D4 requires before a public import path is removed; amends ADR-0001
  (Amendment 2: the new prefixes of moved packages join the class-path allow-list); updates
  ADR-0120's citations of the dispatch outbox to its new file; ADR-0138 records the Studio backend's
  form (D7, Amendment 2); Amendment 3 (Proposed) amends D6 row 3 and replaces Amendment 1 A5's
  providers-last order, with ADR-0140 as the providers discovery contract

## Amendment 3 (2026-10-31): providers move first, with finite interim imports

This amendment replaces Amendment 1 A5's providers-last order and amends D6 row 3 explicitly. It preserves D1's single distribution through 1.x, D3's one table and finder, D4's removal in 2.0.0, and D5's identity, persisted-name and logging requirements. The maintainer requires existing 0.x callers to continue working unchanged throughout 1.x, without a new install or registration step.

D6 row 3 currently requires: "Both endpoint registration (`lionagi/service/connections/registry.py`) and token-budget lookup (`lionagi/service/token_budget.py`) use that contract instead of the module paths they list today." This amendment relaxes that prerequisite: the discovery contract lands after the package move and before 1.0. Between those changes, both files retain their module lists with their 46 provider paths rewritten to the new root, as the listed interim edges in C2. This puts the rename-only move with no behavior change first and the discovery behavior change second. The move carries an exact inventory test; a release gate prevents 1.0 while any of those 46 interim edges remains.

### C1. Prerequisites and the new order

Before the providers package moves:

1. Relocate provider errors to `lionagi._provider_errors`, secret resolution to `lionagi.service.accounts._secret_resolution`, subagent admission to `lionagi.service._subagent_admission`, and the shared chat schema to `lionagi.service.connections._chat_schemas`, one change each. Preserve behavior and old names with specific D3 rows, using the first release that contains each move and removal in 2.0.0. Tests must support module rows before those rows land. The later broad providers row must not override these specific destinations. `_codex_profile` stays with the providers. For moves, D3's finder replaces A5's on-disk re-exports and returns the same objects, verified by identity tests.
2. Accept ADR-0140 before dependent discovery implementation merges. Implement its catalog, metadata, failure and collision behavior after the package move and before 1.0. The move only rewrites the existing module lists; the subsequent contract change replaces both lists with the shared catalog.
3. Preserve A5 item 1's provider-facing settings seam: secret resolution uses the existing `lionagi._settings` reader with `include_project=False` and the existing refusal/failure behavior. The tool interface and gate-contract work may wait until the tools/agent/casts move. No provider may gain an import of moved agent settings.
4. Make the four module-scope `_codex_profile` imports in `agent/factory.py` lazy at their actual uses in a separate preparatory change. The other provider references listed for that file in C2 are already lazy. C2's inventory test lands in the package move, not before it; ADR-0140's contract failure checks land with the subsequent contract implementation.

The package and contract changes are then:

1. Move `lionagi/providers` completely to `lionhub/src/lionhub/providers` with one broad compatibility row. Keep the implementation diff to renames, import rewrites, path docstrings, pinned logger names and the 46 module-string root rewrites, without behavior changes.
2. Implement ADR-0140 D1–D5 after that move and before 1.0. Write `lionhub.providers.manifest` directly at its final path, add `_provider_compat` against it, and delete the 46 interim edges.
3. Move `tools`, `agent` and `casts` under A5 item 1's coupled-move rule, including its separate target rows, root exports, installed-wheel data and old-pickle guarantees. Their tool interface, gate/settings and casts request-contract seams remain prerequisites, as does removal of the CLI catalog from core. This amendment does not require those unrelated seams before providers.
4. Move `hooks.external` to `lionhub.hooks.external`, after agent, under A1 and A5.
5. Move LNDL and its Middle under A3, after the fence-extraction seam. Its root-export compatibility exception is unchanged.

The plugin runtime and session hook bus stay in core under A1. Amendment 2's retained Studio application is unchanged. The CLI is still under `lionagi` at the development head and remains there at the providers move; its imports become hub-to-hub only when the CLI moves. The providers move does not wait for that CLI move; the CLI's own prerequisites remain in force.

### C2. Exact interim imports and their removal

D2's statement that no core code initiates a hub import is amended for the paths below through their specified removal steps. These are dependency edges under ADR-0122 D1, including lazy references and the CLI module-top import. They are not permission for any other file in these packages to name the hub.

In the move change, add `test_provider_interim_edge_inventory` and rename D2's package-set test to `test_only_packages_with_listed_interim_edges_name_lionhub`. D2 now means "packages that hold a listed interim edge", since `operations` and `service` never move. The file-and-target inventory is the authority for `lionhub.providers.*` targets, the interim edges this move creates, and it lists the existing CLI and Studio provider edges as rows like every other core consumer; the package set is its projection. The wave-1 hub edges that `cli` and `studio` already hold (`lionhub.orchestration*`, `lionhub.serve*`) are not inventory rows: only the package-set test counts them, and they leave with the CLI move and with the application extraction under Amendment 2; `_moved.py`'s D3 rows are data to both tests, as they are to the package-set test today. Both tests include ordinary, relative, string, type-only and lazy references and reject new or stale exceptions. Paths below are relative to `lionagi/`; the two target lists are frozen from the pre-move source and only their root changes.

| Core consumer | Allowed target and purpose after the move | Change ending the exception |
|---|---|---|
| `service/connections/registry.py` | The 34 `lionhub.providers.*` strings in `_import_all_providers`' `_modules` list; the separate core scripted-provider entry is not one of them | Delete the list and its 34 edges in the contract change, before 1.0. |
| `service/token_budget.py` | The 12 `lionhub.providers.*` values in `_PROVIDER_MODULES` | Delete the table and its 12 edges in the contract change, before 1.0. |
| `agent/factory.py` | Existing references to `lionhub.providers.openai._codex_profile`, `lionhub.providers.openai.codex`, and `lionhub.providers._cli_subprocess`; all lazy after the preparatory change | Remove these core edges and `agent` from the set in the tools/agent/casts move. References then belong to the hub. The shared schema reference already points down into core. |
| `operations/run/run.py` | Existing lazy `lionhub.providers.google.gemini_code` request/print-timeout behavior | Before 2.0, replace the import with an endpoint-supplied request-preparation port; remove `operations` from the set in that change. The operation stays in core. |
| `service/accounts/_router.py` | Existing lazy `lionhub.providers.anthropic._prompt_cache` helpers | Before 2.0, replace both imports with adapter-supplied cache evidence and server-tool projection through a core-owned port. |
| `service/providers.py` | Existing lazy `lionhub.providers.openai._codex_profile` effort-default lookup | Before 2.0, replace this import with provider configuration/effort policy supplied by the selected adapter. |
| `cli/_providers.py:45` | Module-top `lionhub.providers.openai._codex_profile`; also existing lazy `lionhub.providers.google.gemini_code` references at lines 200 and 320 | Remove these core edges in the CLI move; only then are they hub-to-hub. |
| `cli/agent.py` | Existing lazy `lionhub.providers.google.gemini_code` reference at line 2152 | Remove this core edge in the CLI move; only then is it hub-to-hub. |
| `cli/orchestrate/_orchestration.py` | Existing lazy `lionhub.providers.openai._codex_profile` reference at line 852 | Remove this core edge in the CLI move; only then is it hub-to-hub. |
| `studio/operator/engine.py` | Existing lazy `lionhub.providers.anthropic.claude_code` (line 405) and `lionhub.providers.google.gemini_code` (line 679) references | Remove these edges, and `studio` from the set, with the application extraction under Amendment 2 at 2.0. |
| `service/connections/_provider_compat.py` (added in the contract change, absent at the move) | One `lionhub.providers.manifest` source, used by registry and token budgeting | Delete at 2.0; roots pass composed catalogs. |

This amendment also relaxes D6 row 3's "a seam in the core for each, first" for `operations/run/run.py`, `service/accounts/_router.py` and `service/providers.py`. Their seams are deferred to before 2.0 so the behavior-preserving package move does not depend on request and policy redesign. These three existing implementation edges are temporary exceptions, not completed seams. Their replacement must preserve timeout precedence, cache-certificate and account-budget behavior, child-environment selection, explicit effort overrides and unknown-model handling. It may not copy these provider implementations or model catalogs into core. The 2.0 release guard requires none of these imports or automatic lookup paths to remain, plus behavior tests using injected policies.

At the providers-move head, `LIONHUB_USERS_UNDER_LIONAGI` is exactly `{agent, cli, operations, service, studio}`. The inventory test lands at that head and requires this exact set, with no alternative based on a presumed earlier CLI move.

The CLI and agent members leave on their respective moves. Operations leaves when its request-preparation import is removed. Service remains while any listed source file names the hub: the two module lists leave in the contract change before 1.0, which adds the automatic manifest source through 1.x. Service leaves when its final edge is removed at 2.0. Studio remains through 1.x and leaves with the application extraction under Amendment 2. At 2.0 the set is empty. Earlier deletion of an edge shrinks the file inventory immediately; it never leaves a stale exception.

### C3. Compatibility, failure and change evidence

ADR-0140 defines discovery, lazy selection, key ownership and duplicates, contract versions, missing/failed provider behavior and context-window metadata under the amended D6 row 3 timing. This amendment adopts the following reading of D6 row 3's "shown with `lionhub` present and absent": present means the unchanged call resolves; absent means it reports at the public boundary, never a silent fallback. A fresh installed process resolves the unchanged provider constructor with the hub present and no earlier hub import. With the hub physically absent in 1.x, the same constructor reports an incomplete bundled installation at its public boundary. At 2.0 the boundary distinguishes an absent `lionhub` distribution from an installed hub without a composed provider catalog, as specified in ADR-0140 D4. Explicitly injected adapters remain usable without the hub.

The interim module lists, subsequent automatic loader and legacy registry publication are explicit temporary exceptions to ADR-0122's layer and composition rules. In 2.0 roots select and validate provider fragments and pass immutable catalogs or ports; importing or installing the hub does not activate it. ADR-0088 D8 entry points remain deferred.

D6's six per-change items apply with these amendments:

1. Keep package relocation to renames, import rewrites, path docstrings and pinned logger names. The 46 module-string root rewrites belong to the move. Discovery, declaration and failure changes land separately afterward and before 1.0; the three deferred policy seams land before 2.0. The move carries fresh-process checks of every existing `lionagi.providers.*` import and every built-in `iModel(provider=...)` key with supported prerequisites and no earlier hub import.
2. Include compatibility rows and CHANGELOG lines for the four lower modules and provider package, with actual deprecation release numbers and removal in 2.0.0.
3. Rewrite repository callers and configuration/type strings to canonical paths, including `LazyType` strings. The finder is for external old references, not internal discovery.
4. Report the import graph at the change head and exact package and file exception sets. For this move, the enumerated C2 consumers replace the original assertion that no core consumer imports the moved package. No other core consumer is authorized.
5. Report D5's persisted-data population searched, old-path counts, and applicable class tests or AST evidence. A source census does not establish that external stored data contains no old path. Preserve old pickle and patch identity and the longer-prefix core destinations.
6. Update moved-file citations that executable checks depend on; historical records retain their historical paths.

The four lower-module homes do not authorize provider-specific core growth. The existing shared schema carries model-dependent validation and a warning filter; retaining its behavior during relocation is a bounded compatibility choice, not proof of a pure contract. Full ADR-0122 compliance requires separate cleanup with parity evidence. Admission and secret resolution remain explicit services and must not run on import. Importing these modules by their canonical core paths must not load the hub; their old paths load the moved parent providers packages after the broad row lands.

**MAY:** retain only C2's listed interim edges through their removal steps and perform the four lower-module moves. **MAY NOT:** enlarge the exemption to other consumers, hide references in computed strings, use the finder for internal provider discovery, change logger names, or silently substitute a generic endpoint when a declared provider fails. **Verify by:** exact package and file inventories at every head, the 1.0 gate deleting all 46 interim edges, ADR-0140's fresh-wheel matrix, D3–D5 compatibility checks, and the 2.0 guard requiring no automatic hub path or compatibility row.

## Amendment 2 (2026-10-25): the Studio boundary is set in wave 2 and the application extracted at 2.0

This amendment preserves D1's single-distribution transition and its promise that every existing
`lionagi.*` import path remains available until 2.0 with its existing dependency requirements.
It changes the timing of the Studio application extraction in D6, D7, and Amendment 1 A4.
It withdraws D7's requirement that the backend record define how the container image gets and
installs its backend and frontend from versioned public sources, with its release and rollback steps;
how the application and its frontend are distributed after extraction is decided outside these records.
It also extends the existing `studio` exemption in D2 until that extraction: D2's exempt set is
`{cli, studio}` until B2 step 6 moves the CLI, `{studio}` from that step through the last 1.x release,
and empty from 2.0. No core package gains permission to depend on the application or to name `lionhub`.

### B1. Keep application imports available through 1.x

The Studio application remains at its current `lionagi.studio` paths in the `lionagi` wheel through
1.x. This includes its application routes, Operator, app factory and app export, and module launch
surface. The daemon, scheduler, and shared execution code still move to `lionhub` during wave 2.
The application moves to `apps/studio/backend/src/` at 2.0, outside both library wheels and never into
`lionhub`. D7's target destination is unchanged.

The backend record (ADR-0138) defines and establishes the application attachment, route ownership, lifecycle,
identity authority, and shared services during wave 2. An explicitly selected application attaches
to the daemon; a daemon started without it does not import application modules. Keeping application
files in the wheel for compatibility does not make them a daemon dependency. Existing Studio launch
commands continue to select the bundled application through 1.x without a new install step.

D3 rows are added only for modules or complete subpackages actually moved into the wheel's hub root.
No broad `lionagi.studio` row may cover application modules retained on disk. Shared definitions
extracted from a mixed module retain their old exports where necessary, refer to one implementation,
and preserve object and state ownership. Retained modules are not represented as completed moves.
The original application paths remain canonical until extraction, so importing those retained paths
is not an exception to D4's prohibition on importing a path already moved by the table.

The wave-2 release documents the retained application paths and their removal in 2.0. A checked
inventory covers those paths independently of D3's moved-module table. Fresh-install tests preserve
their imports and the app and module launch surfaces throughout 1.x. A release guard requires removal
of the retained application and its exemption at 2.0, when tests require the old application imports
to fail. D4's compatibility rows still end at 2.0. The application's 2.0 extraction adds no runtime
alias and needs no row whose destination was unavailable during 1.x. D5's persisted-name inventory,
conversion, and release documentation obligations also cover the application paths removed at 2.0.

### B2. Replace Amendment 1 A4's wave-2 order

1. MCP remains moved to `lionhub.serve.mcp` under A4 item 1. Its compatibility and module-execution
   checks are unchanged.
2. Accept the CLI record and the Studio backend record before any change depends on their destinations
   or boundary. The backend record allocates every dependency listed in A4 item 2, states the 1.x
   bundled installation, and states the behavior any 2.0 application installation must keep.
3. Break the CLI and Studio cycle and move the populated CLI catalog, its selection and loading entry
   points, and shared command logic as specified in A4 item 3. A Studio launcher may select the retained
   application explicitly; daemon-only and local schedule commands must not import it. The application
   calls shared services rather than private CLI command modules. No core seed names a moved target.
4. Move the scheduler to `lionhub.orchestration.scheduler` with its compatibility rows and wheel-owned
   helpers, after or together with their extraction. Record both import directions and every retained
   Studio module, as specified in A4 item 4.
5. Move the daemon and its API to `lionhub.serve`. Establish the backend record's attachment, route split,
   identity authority, lifecycle, and shared-service boundaries while the application remains in the
   wheel. The unused HTTP module list and loading path leave `lionagi._auto` by deletion in this step.
   The CLI catalog has already left in step 3. Test local schedule validation and export on a fresh
   installed wheel outside the checkout, with the application disabled and its imports refused by the
   test. This is the 1.x proof of backend independence; application files remain installed under D1.
6. Move the CLI to `lionhub.serve.cli` in the form its record chooses and change the `li` entry-point target.
   Preserve the fresh-subprocess checks of old and new module invocation from A4 item 6. Studio commands
   select the retained application through the explicit attachment; local commands use wheel services.

Steps 3 through 5 may contain preparatory service extraction and explicitly specified behavior fixes
before package moves. The moves themselves retain D6's restriction to relocation and import rewrites.
In particular, a lifecycle cleanup correction or client authentication correction is not hidden in a
move. Every intermediate state keeps D2 and D4. D2's exempt set is `{cli, studio}` before step 6 and
`{studio}` after it, rather than the empty set the original D6 order reached at the end of wave 2. The
retained application is not core code, even though its compatibility path is under `lionagi`.
The test continues to require exact agreement with the packages that actually name `lionhub`; it does
not gain a blanket exemption for retained compatibility files elsewhere.

### B3. Complete extraction at 2.0

At 2.0, move the retained application to `apps/studio/backend/src/`, update its imports and activation
source, and remove its old import paths and the `studio` exemption. The route, identity, state, and
lifecycle contracts are already established by wave 2 and do not change because files move. The CLI
record and D1's distribution release record determine console-script and package ownership. The backend
record states the installation behavior the extracted application must keep; how the application and
its frontend are distributed after extraction is outside this record and the backend record. A fresh
core-and-hub installation with the application physically absent must pass the local schedule
validation and export test required by A4 item 5.

The remaining `lionagi` package contains no application code and names no `lionhub` module after the
compatibility removals. D7's statement that an extracted backend needs its own install or build step
applies from this extraction. During 1.x the application still comes from the `lionagi` wheel; the
`studio` extra continues to add dependencies. No second application distribution is introduced before
2.0, and this amendment does not change the other residency and seam decisions in Amendment 1.

## Amendment 1 (2026-10-25): where the hook bus and the plugin runtime live, the casts and LNDL seams, and the order of waves 2 and 3

Facts below were measured at development-line commit `6bcd28a0`. Line counts include every newline in the
named Python files. Import-site counts use an AST walk over absolute, relative, nested and type-only imports,
counting each import statement once.

### A1. The session hook bus and the plugin runtime stay in the core

D6 places `hooks` and `plugins` in wave 3 and D7 gives them `lionhub/hooks/` and `lionhub/resources/`. This
amendment narrows both.

**The hook bus.** `lionagi.hooks.bus`, `loader`, `persist`, `builtins` and `_message_retry` (953 lines) are the
session's event bus and its default persistence handlers. They import only `ln`, `session` and `state`.
`Session.hooks` builds the bus on first use (`session/session.py:137-146`) with four default handlers (session
start, session end, branch create, branch end; `hooks/loader.py:25-30, 81-104`), and operations and `Branch`
take `HookPoint` from it at 9 sites (`operations/_api_hooks.py:150, 179, 219`, `operations/act/act.py:162, 194,
220`, `operations/chat/chat.py:28`, `operations/run/run.py:451`, `session/branch.py:417`). A bus moved out of
the core would need its default builder supplied by a bootstrap, and an injected bus that starts empty would
drop those defaults. These five modules and the package initializer stay in the core under their current
paths, with no table row.

`lionagi.hooks.external` (883 lines) runs configured hook commands and reads plugin settings. It is what D7's
`lionhub/hooks/` slot describes, and it moves there in wave 3 as `lionhub.hooks.external`, one module with one
table row. The core `hooks` initializer does not import it, before or after the move. In the same change, the
compatibility tests enumerate a row's own module and walk descendants only when the destination is a package
(today they read `__path__` from every destination), and module identity, old pickles, warning attribution,
removal version and stale-file checks cover the row.

**The plugin runtime.** `lionagi.plugins` (6 modules, 1,458 lines) is the core's extension mechanism under
ADR-0088. Its consumers are tool misses and diagnostics for registered tools in `ActionManager`
(`protocols/action/manager.py:197-215, 251`), provider misses and provider revalidation in `EndpointRegistry`
(`service/connections/registry.py:336, 368-370, 421, 422, 448-475`). A provider miss currently consults every
active declared provider module, not only the requested one. Discovery is lazy, trust is read on first use, and
no caller registers anything (ADR-0088 D3). The runtime imports nothing above the core. Moving it would need an
automatic bootstrap that preserves all of this in a fresh process; ADR-0088 D8 defers entry points, and no other
bootstrap is chosen here. An injected runtime that starts empty would change caller behavior. So the runtime
stays in the core under its current paths, with no table row. Plugin authoring and management commands may move
with the CLI.

D7 changes accordingly. `lionhub/hooks/` holds `external`. `lionhub/resources/` loses `registry, plugin`; it is
created only when code other than the runtime needs it, under D7's rule against empty modules.

### A2. The casts seam

The core imports `lionagi.casts.emission` at 5 sites: `SpawnRequest` at `operations/flow.py:1126`, and
`EscalationRequest` at `operations/flow.py:1162, 1228, 1306` and `session/signal.py:280`. Both classes
(`casts/emission.py:401, 437`) are request contracts that the flow runner and the session signal consume. They
are defined once in a core module at or below `session`, and `lionagi.casts.emission` imports and re-exports the
same class objects. The seam keeps the schemas, the defaults, the refusal of extra fields, the four operation
values, and `EscalationRequest`'s legacy `blocking` input and property (`casts/emission.py:422-434`). Neither
class derives from `Element` (both are Pydantic models), so D5's class-path condition does not apply to them;
pickles that name the old module still load, and the seam change shows it with a test. This paragraph's line numbers are at `c4abb363`.

`lionagi/_auto.py:157-161` declares a CLI command whose module is `lionagi.casts.surfaces`. That declaration
belongs to the CLI's catalog and leaves the core with it (A4, step 3).

### A3. The LNDL seams

- **Fence extraction.** `operations/parse/parse.py:315` imports `extract_lndl_blocks` to pull LNDL blocks out of
  a reply and join them in source order. A core extractor takes the language name as an argument, and LNDL's
  extractor is built on it. The change keeps every current rule of `lndl/extract.py:12-27`: only fences whose
  language is LNDL, matched case-insensitively (JSON fences are not taken); backtick and tilde fences; a closing
  fence identical to the opening one (same character, same length) at the same indentation, so a shorter or longer
  interior fence stays content; CRLF normalized inside blocks; trailing block newlines kept; blocks joined in source order; and the reply
  returned unchanged when it has no block. Its line numbers are at `c4abb363`.
- **The LNDL Middle.** `operations/lndl_middle/lndl_middle.py:16` imports 15 LNDL names. It is LNDL's opt-in
  Middle, so it moves with the language to `lionhub.lndl.middle`, with a table row for
  `lionagi.operations.lndl_middle`.
- **Root exports.** `lionagi/__init__.py:86-94` exports nine LNDL names lazily through `_LAZY_MAP`. They stay
  lazy compatibility names until 2.0, and their entries keep naming the old module paths, which the table
  serves. This is a stated exception, limited to these nine entries, to D4's rule that no repository module
  imports an old name and to D2's rule that the core initiates no hub import; it adds no hub name outside the
  table. A plain `import lionagi` still imports no hub module. The first root access that loads an old module
  emits D3's warning at the external caller's line: the finder's frame walk treats the lazy-import helper
  (`ln/_lazy_init.py`) as import machinery, which it does not do today. Root access and a direct old import share
  one module and one warning history. The move change removes the root's `TYPE_CHECKING` imports of those names,
  which D3 already lists among the things that change without a warning. Tests enumerate the nine exports and
  check identity. At 2.0 a release guard that does not read the table, since the rows are gone by then, requires
  the nine names to be absent from `_LAZY_MAP`, `__all__` and root attribute access, and the release notes name
  both the root names and the module paths. D5 is unchanged: a class path names the defining module, never the
  root export, and old `lionagi.lndl.*` pickle paths keep loading.

The LNDL move amends ADR-0051 D1, which names `lionagi.lndl` as the language's package, and the file list in
ADR-0024 that names the moved files.

### A4. Wave 2, in order

D6's prerequisite that `state` stop importing `cli` is met at `6bcd28a0`: the last three imports were
`state/lifecycle/notify_settings.py:41, 383` and `state/provenance.py:19` at `1e7be920`.

Two edges decide the order. Studio imports CLI code at 32 statements in 14 files: `_util` 8, `_runs` 5,
`_providers` 3, `_project` 3, `_code_identity` 2, `_logging` 1, `_argtypes` 1, `orchestrate` 4, and `team`,
`status`, `kill`, `engine` and `mirror` 1 each, counting imports of names from the CLI package as well as from its
submodules. The CLI imports Studio code at 11 statements in 4 files (`cli/machine_schedule.py`, `cli/main.py`,
`cli/mirror.py`, `cli/monitor.py`), including the schedule declaration and export services, and
`studio/scheduler/subprocess.py:279` builds its parser with the CLI's `add_orchestrate_subparser`.

1. **MCP** to `lionhub.serve.mcp`, with one table row. Landed. The alias loader implements module execution, so
   `python -m lionagi.mcp` runs the moved module under its old name. D2's set stays `{cli, studio}`.
2. **Records.** The CLI record and the Studio backend record (D7, DEFERRED) are accepted before any change that
   depends on their destinations or on the backend boundary. The backend record allocates every scheduler and
   CLI dependency on Studio code: scheduler state and its cycle with scheduler coordination
   (`studio/scheduler/engine.py:43`, `studio/services/scheduler_state.py:26`), configuration, project
   resolution, maintenance, lifecycle reapers, schedule declaration and export, and shared command parsing.
   Logic the wheel's daemon or CLI needs stays in the wheel through shared modules or core interfaces;
   application routes and operator presentation move to the backend.
3. **Break the CLI and Studio cycle, and take the CLI catalog out of the core.** The helpers both sides use move
   to one shared module set with public names. The command logic and parsing Studio calls move to the
   `lionhub.orchestration` modules D7 lists (flow, spawn, job, wait, kill, team, notify), and both sides import
   them from there. Before the first CLI command target moves, the populated CLI seed catalog and its selection
   and loading entry points leave `lionagi/_auto.py` and their callers are rewritten; this includes the
   `studio`, `schedule` (`_auto.py:175, 180`) and `casts` declarations. A fresh CLI invocation discovers the same
   commands with no prior hub import and no caller registration. The generic declaration types may stay in the
   core if they name no hub module. Wrapper commands may stay at their old paths until their own move. The step
   is done when `lionagi.studio` imports no private `lionagi.cli` module, the remaining edges run one way, and
   no seed in the core names a moved target.
4. **The scheduler** to `lionhub.orchestration.scheduler`, with its rows, together with the wheel-owned helpers
   the backend record assigns, or after they are extracted. The change records the import graph in both
   directions and the Studio modules that remain.
5. **The daemon** to `lionhub.serve`, and the Studio application (operator, routes, app shell) to
   `apps/studio/backend` in the form the backend record chooses. The HTTP module list in `lionagi/_auto.py`
   leaves the core in the same change. The CLI's local schedule validation and export keep working with the
   backend absent, checked on a fresh install.
6. **The CLI** to `lionhub.serve.cli` in the form the CLI record chooses, and the `li` entry point's target
   changes. Old and new `python -m` invocations are checked in fresh subprocesses: arguments, package-relative
   imports and exit status.

Every intermediate change keeps D2 and D4: no package under `lionagi/` names `lionhub` outside the exempt set,
and no repository module imports an old path. After step 6, D2's set is empty.

### A5. Wave 3, in order

Seams first, one change each. Each core definition keeps the existing defaults and behavior, and each old module
re-exports the same objects.

1. The tool interface, and the gate and settings contracts. The core settings reader keeps the user-only mode
   (`include_project=False`) and failure handling that provider secret lookup uses
   (`providers/_secret_resolution.py:101-104`). Its line numbers are at `c4abb363`.
2. The casts request contracts (A2).
3. LNDL fence extraction (A3).
4. The providers record (D6), with no source change.

Then the moves:

1. `tools`, `agent` and `casts` in one change, after the seams and after the CLI catalog has left the core (A4,
   step 3). `tools/coding.py` imports `agent` at 6 sites and `agent` imports `tools` at 12 sites in five files
   (`agent/permissions.py:14` at module scope, and `agent/factory.py`, `context_fold.py`, `hooks.py`,
   `nudge.py`), so neither can move first without a package under `lionagi/` naming `lionhub`. `casts` may join
   this change or follow it: `agent` imports `casts` and `casts` does not import `agent`. `agent` goes to
   `lionhub.agent`; `casts` goes to `lionhub.agent.casts` with its own row, so neither package initializer
   overwrites the other (`agent/__init__.py` exports eagerly, `casts/__init__.py` lazily). The change keeps casts'
   root exports, its packaged `packs/default.yaml` (checked from an installed wheel, since `casts/catalog.py:21-27`
   returns `None` on a load failure), and old pickle paths. `providers/_secret_resolution.py:101` reads settings
   through the core reader from the settings seam, so D2's set does not grow. Its line numbers are at `c4abb363`.
2. `lionagi.hooks.external` to `lionhub.hooks.external` (A1). It goes after `agent` because `agent/factory.py`
   and `agent/settings.py` import it, as does the CLI's `hooks` command; nothing imports it the other way.
3. LNDL and its Middle to `lionhub.lndl` (A3).
4. `providers` last, under its record.

Each move change carries D6's six items, unchanged.

### What this amendment does not decide

- The CLI's home and the owner of `li` at 2.0, and the Studio backend's form: each remains its own record (D7).
  A4 states what those records must allocate, not their answers.
- The providers contract: its own record (D6).
- Entry-point discovery or any other automatic plugin bootstrap: still deferred by ADR-0088 D8. If one is
  adopted later, the residency in A1 can be revisited by a new amendment.

The maintainer asked for a layout that separates the core from the packages built on it, a first
migration that breaks no caller, and the removal of the compatibility paths by lionagi 2.0 under a
concrete deprecation plan. This record decides how.

## Context

Line counts are `wc -l` at main `1e7be920`; the import graph is an AST walk over 611 files that
counts absolute, relative and in-function imports.

**P1. The core and the code built on it share one package.** `lionagi/` holds the core (protocols,
messages, session, operations, the service layer, `ln`) beside orchestration patterns (520 lines),
the orchestration engines (5,998), the durable dispatch outbox (808), work forms (475), the CLI
(32,958), the MCP server (8,155) and Studio (45,130). ADR-0125 moves the core toward lion1's runtime
shape. While application code sits inside the core's package, every core change also has to be read
against it, and the core's own size and seams are hard to see. Taking the code built on the core out
of the core's package comes first.

**P2. The layer law is not enforced.** ADR-0122 D1 says a lower layer never imports a higher one,
and its Phase 0 adds a static checker. At `1e7be920` no import-rule check exists under `tests/` or
`scripts/`. Inside one package an inversion is one relative import away and reads like any other.

**P3. Moving a module renames it.** Every `import lionagi.engines…` outside this repository, every
`unittest.mock.patch("lionagi.engines…")` string, every pickle and every serialized class path names
the old module. A plain move breaks all of them in the release that ships it.

**P4. A second distribution now would tie two release contracts together.** Old paths kept in
`lionagi` would forward to `lionhub`, and `lionhub` imports `lionagi`, so each distribution would
depend on the other during the transition. Compatible version requirements can resolve such a cycle,
but the split would keep ownership running both ways and require coordinated releases and upgrades
while the boundary is still changing. ADR-0122 D7 also measured a cycle between `cli` and `studio`
(14 files one way, 3 the other), and its seven gates for a split have not been met. Providers stay
in `lionagi` during wave 1; moving them later also needs discovery and installation contracts that
keep `iModel(provider="openai")` working throughout 1.x (D6).

**P5. Serialized data names classes by module path.** `Element` data records its class in
`metadata["lion_class"]`, and deserialization imports a module only under an allowed prefix
(ADR-0001 Amendment 1, default `lionagi.`). After a move, data written under the new path is refused
by a process that has not already imported the class; data written under the old path still has to
load; and the metadata check that compares `lion_class` with the class's own path rejects old-path
metadata.

**P6. Compatibility paths need an end.** ADR-0122 D8 makes compatibility "explicit and finite", and
ADR-0125 D4 forbids removing a public name without its own versioned decision. Without a removal
release written into the code and checked by a test, old paths stay indefinitely.

| Concern | Decision |
|---------|----------|
| Distribution | D1: one `lionagi` distribution ships two top-level packages, `lionagi` (the core) and `lionhub`, until 2.0. |
| Layering | D2: the core never names `lionhub`; a test lists the packages under `lionagi/` that still do, each of which moves later. |
| Old import paths | D3: one table, `lionagi._moved.MOVED`, served by a meta-path finder that returns the moved module object itself under its old name, with one `DeprecationWarning`. |
| Deprecation plan | D4: each row records the first release that deprecates it, 0.37.0 for wave 1, and removal in 2.0.0; a test fails the release that reaches the removal version; the CHANGELOG carries the table. |
| Persisted names | D5: serialized class paths load under either path until 2.0 and are saved under the new one, nested elements included; logger names do not change; each move searches persisted data first; 2.0 ships a published mapping and a converter. |
| Order | D6: three waves by import graph: packages the core never imports, then the CLI, MCP and Studio once their cycle is broken, then packages the core imports, each after a core seam. |
| Layout | D7: all of `lionhub` lives under `lionhub/src/lionhub/`; the target layout and where each part comes from. |

Out of scope:

- **The Studio backend's form** (its own process beside the daemon, or a plugin the daemon loads):
  DEFERRED to its own record before any wave-2 change depends on the backend boundary; D7 lists
  what that record decides, including how the free container image is built.
- **Where the CLI lives and who owns `li` at 2.0**: DEFERRED to its own record before the wave-2 CLI
  move. Until 2.0 the `lionagi` distribution provides `li` wherever the code sits (D7).
- **The 2.0 release date**: the maintainer's. This record fixes the version, not the date.
- **Changing core mechanisms**: ADR-0125 and later records. The moves here change no behaviour,
  including the older safeguards in the core that current models may no longer need.
- **Which concepts from other projects are ported, and when**: each port is its own change (D7).
- **Editions outside the open-source package**: the hosted and desktop editions are outside this
  repository.

## Decision

### D1 — One distribution, two import roots, until 2.0

The `lionagi` wheel ships two regular top-level packages. `lionagi` is the core and the import root
it always was. `lionhub` holds the packages built on the core and ships in the same wheel.

```toml
[tool.hatch.build.targets.wheel]
packages = ["lionagi", "lionhub/src/lionhub"]
```

```text
lionagi/                  the core
lionhub/src/lionhub/      the second import root
  __init__.py             defines nothing (__all__ = ())
  orchestration/          wave 1 (D6)
```

Exact semantics:

- `pip install lionagi` installs both packages. No install has one without the other, so
  `import lionhub` works wherever `lionagi` is installed.
- `lionhub` has no version of its own; it is the distribution's. No `lionhub` distribution is
  published before 2.0.
- A non-editable install of the base dependency set imports `lionhub.orchestration`, and one moved
  module under both names, in the CI `core-install` job. The job runs outside the checkout and
  asserts that `lionagi` and `lionhub` both load from the installed environment, since run from the
  repository root Python would import the source tree's `lionagi` instead.
- An editable install built by hatchling without `dev-mode-exact` puts each package's parent
  directory on `sys.path`: the repository root for `lionagi` and `lionhub/src` for `lionhub`
  (hatchling 1.32 `build_editable_detection`, `add_to_path(os.path.dirname(...))`). Every directory
  in `lionhub/src` is then importable as a top-level package, which is why D7 allows nothing there
  except `lionhub/`. The first wave-1 change checks an editable install from a clean environment: one
  module imported through its old path and its new path, the same object.
- Every place that lists source roots names both: the wheel `packages`; pyright's `include` and
  `extraPaths`; ruff's `src`, so `lionhub` sorts as first-party; the lint, format, coverage and
  hygiene paths in `scripts/ci.sh`; the documentation-source roots of the skill linter; the Studio
  container build, which copies `lionhub/src/lionhub/` beside `lionagi/`; and the source roots of
  `tests/test_moved_modules.py` and `tests/operations/test_graph_entrypoint_conformance.py`; and the
  CI change filters of the container job and the import and startup jobs, which include
  `lionhub/**`. A root missing from one of them is a package that tool does not see. Each root ships
  its own `py.typed` marker.
- ADR-0122 D7 keeps the `lionagi` top-level namespace intact. It stays intact: every existing
  `lionagi.*` path imports until 2.0 (D3). `lionhub` is a second regular package, not a namespace
  package, which that record does not approve.
- At 2.0 `lionhub` gets its own project file and distribution and depends on `lionagi`, and the
  `lionagi` wheel no longer contains the `lionhub` package. By then no table row is left (D4), so
  `lionagi` imports nothing from `lionhub` and no cycle exists. This amends ADR-0122 D7's requirement
  that `lionagi` stay a compatibility distribution for a documented window after a split: the 1.x
  line is the transition window, and the 2.0 `lionagi` distribution is the core alone. Changing
  imports during 1.x is one migration step; code that uses `lionhub` also adds `lionhub` to its
  declared requirements for 2.0. Before the split, its release record provides the installation
  matrix, compatible version constraints, the one distribution that owns each package file and
  console script, and tested fresh-install, upgrade, uninstall and rollback procedures that keep a
  bundled pre-2.0 `lionhub` and a separately installed one from owning the same files. A core-only
  2.0 install keeps the core and adapters passed in by the caller; capabilities that moved to
  `lionhub` need it installed. The record decides the release mechanics against ADR-0122 D7's
  seven gates.

Why this way:

- **A separate distribution now** could keep installs working by declaring the dependencies both
  ways. It would add release, version-compatibility and upgrade obligations before ADR-0122 D7's
  gates are met (P4). The cycle itself can be resolved; the reason to wait is the unfinished
  boundary and release contract.
- **A subpackage, `lionagi.hub`**, keeps one import root but moves every caller twice: to
  `lionagi.hub.x` now and to `lionhub.x` at the split. A second top-level root now means the 2.0
  split changes distribution metadata and leaves import paths alone.
- **One wheel** keeps every install unchanged: nobody installs anything new and nothing that imported
  before fails. The layer rule that a package boundary would have enforced is enforced by a test (D2).

### D2 — The core never names lionhub

```python
# tests/test_moved_modules.py
LIONHUB_USERS_UNDER_LIONAGI = frozenset({"cli", "studio"})
_LIONHUB_NAME = re.compile(r"lionhub(\.[A-Za-z_][A-Za-z0-9_]*)*")
```

`test_only_packages_that_move_later_name_lionhub` walks every `.py` under `lionagi/` except
`_moved.py`. It collects each `import lionhub…`, each absolute `from lionhub… import`, and each string
constant that is exactly a `lionhub` dotted name, groups the hits by top-level package under
`lionagi/`, and requires that set of packages to equal `LIONHUB_USERS_UNDER_LIONAGI`.
`test_the_lionhub_check_sees_each_form_of_reference` runs the collector over fixed source and
requires the import, the from-import and the dynamic-import string to be seen, and the near misses
(`lionhubx.orchestration`, and the prefix string `lionhub.`) not to be.

Exact semantics:

- A core package that starts naming `lionhub` fails the test.
- A listed package that stops naming `lionhub` also fails it, so the set shrinks with the tree and
  never carries a stale exemption. Each move change sets it to what the tree does at its head; wave 1
  ends at `{cli, studio}`, both of which move in wave 2.
- Dynamic imports count, as ADR-0122 D1 requires: `importlib.import_module("lionhub…")` is a string
  constant naming a `lionhub` module. Imports under `TYPE_CHECKING` count because they are import
  statements.
- `lionagi/_moved.py` names `lionhub` only as table data (D3) and is excluded by name.
- The class-path allow-list in `lionagi/ln/_utils.py` takes its added prefixes from the table (D5)
  and names no `lionhub` module itself. That is deliberate and recorded here rather than hidden: the
  allow-list is trust policy, and the core imports a `lionhub` module through it only when serialized
  data names one (ADR-0001 Amendment 2). The core imports no `lionhub` module on any path of its own.

Until wave 2, imports between the two roots run both ways: `lionhub` imports the core, and the two
listed packages, `lionagi.cli` and `lionagi.studio`, import `lionhub`. That is the accepted interim
shape, and the listed set is where it is recorded. Both roots ship in one wheel (D1), so `lionagi` has
no second distribution to declare as a dependency, and the CI `core-install` job imports `lionhub` from
a non-editable install of the base dependency set. The edges into `lionhub` end when wave 2 moves the
two packages out of the core.

Why this way: inside one distribution the package boundary enforces nothing (D1), so a test does. A
40-line AST test runs in the existing CI `test` job and needs no new tool; an import-linting
dependency for one rule was weighed and lost on that. ADR-0122 D6's full static checker subsumes this
test when it lands.

### D3 — Old import paths are one table and one finder

```python
# lionagi/_moved.py
class Moved(NamedTuple):
    old: str
    new: str
    deprecated_in: str
    removed_in: str


MOVED: tuple[Moved, ...] = (
    Moved("lionagi.orchestration", "lionhub.orchestration", "0.37.0", "2.0.0"),
    Moved("lionagi.engines", "lionhub.orchestration.engines", "0.37.0", "2.0.0"),
    Moved("lionagi.dispatch", "lionhub.orchestration.dispatch", "0.37.0", "2.0.0"),
    Moved("lionagi.work", "lionhub.orchestration.work", "0.37.0", "2.0.0"),
)

def resolve(fullname: str) -> str | None: ...       # the current name, or None if it did not move
def still_on_disk(fullname: str) -> bool: ...       # a module file still at the old location
def install() -> None: ...                          # the finder first on sys.meta_path, once
```

The warning, one per old module:

```text
{old} moved to {new}; the old import path is removed in lionagi {removed_in}. Import from {new} instead.
```

`lionagi/__init__.py` calls `install()`. Importing any `lionagi.*` name imports `lionagi` first, so the
finder is in place before an old name is looked up.

Exact semantics:

- **Lookup**: the row with the longest `old` that equals the name or is followed by `.` in it.
  `lionagi.orchestrationx` matches no row and goes to the normal import system.
- **First import of an old name**: the import system creates a placeholder for the old name, and the
  loader imports the new name and replaces the placeholder in `sys.modules` with that module object,
  which the import system then returns. The loader writes nothing onto the moved module, its
  `__spec__`, `__name__`, `__package__` and loader included, so imports through two old names of one
  module, at the same time in two threads, cannot disturb each other. One `DeprecationWarning`,
  attributed to the code that wrote the import, for an `import` statement and for
  `importlib.import_module`.
- **Later imports of the old name**: a `sys.modules` hit, no warning.
- **Spec**: the module keeps its real `__spec__` and `__name__` (the new name), so
  `importlib.reload` and the relative imports inside it resolve against the new name.
- **Before the first import**: `importlib.util.find_spec` on an old name returns a spec that
  describes it the way the new module is described, a package with the new package's search
  locations where the new module is one, and resource reads through the old name, such as
  `pkgutil.get_data`, go to the new module's loader. An old name whose new module does not exist
  gets no spec.
- **Below a prefix**: `lionagi.engines.coding` maps to `lionhub.orchestration.engines.coding`; a name
  with no module under the new prefix raises `ModuleNotFoundError`.
- **A module file still at the old location** in `lionagi/` is never served: the finder steps aside,
  a copied-but-not-moved package imports as its stale self, and `test_no_old_module_is_still_on_disk`
  fails. Half a move cannot read as a move.
- **Nested rows**: `lionagi.engines` moved inside the destination of `lionagi.orchestration`. The name
  `lionagi.orchestration.engines` never existed, but the orchestration row maps it to
  `lionhub.orchestration.engines`, so it is served and is the same module object as `lionagi.engines`.
  Declining it would let the normal path finder load a second copy of the package from the
  `__path__` of `lionagi.orchestration`, which is the new package's directory.
- **Identity**: classes, functions and module globals are the same objects under both names, so
  `isinstance`, `unittest.mock.patch` on an old dotted path and pickles naming an old module keep
  working.
- **Laziness**: `import lionagi` imports no `lionhub` module; old names resolve on demand
  (`test_importing_lionagi_imports_nothing_from_lionhub`, in a fresh interpreter, with a second
  import through an old name showing the probe sees `lionhub` modules when they load).
- **Scope**: the finder covers imports, old-path module identity, and spec and resource lookup
  before the first import. Logger names, entry points and other strings that name a module are D5's.
- **What it does not cover**: there are no files at the old locations, so listing `lionagi.__path__`
  with `pkgutil.iter_modules` or `pkgutil.walk_packages` does not show a moved package, and a static
  type checker reading the installed wheel finds no module at an old path. Code that enumerates
  packages lists the new package instead; typed code changes its imports. No code in this
  repository enumerates `lionagi.__path__`. The moved modules and their objects also report the new
  path in `__name__`, `__module__`, `__file__` and tracebacks. None of these warns, and the
  CHANGELOG entry for the moves names them.
- **Cost**: the finder is first on `sys.meta_path`, so every import in a process that imported
  `lionagi` passes one prefix scan over the table (four rows in wave 1) before the normal finders.

Why this way:

- **Star-import files at the old paths** copy public names only, miss the private names callers and
  tests patch, and make `mock.patch("lionagi.engines.x.y")` patch the copy rather than the object the
  code uses. They also leave one file per old module to keep and later delete.
- **Module `__getattr__` facades** in the old packages lose identity for submodules the same way, and
  need the old package directories to stay, which `still_on_disk` exists to rule out.
- **Eager aliasing in `lionagi/__init__.py`** (registering every new module under its old name at
  import) keeps identity but imports all of `lionhub` on every `import lionagi`, against the
  lazy-import contract.
- **Generated files at the old locations**, so that `pkgutil` lists the moved packages and type
  checkers find the old paths. Any such file is a module or package on disk at an old location,
  which the stale-copy rule above exists to keep out, and a generated package would need its own
  `__path__` handling to keep one module object under both names. Type stubs generated from the
  table remain possible later without changing the runtime mechanism.
- **Writing the old-name spec onto the moved module while it loads, then restoring it**: two old
  names of one module imported at the same time in two threads interleave the save and the restore,
  and leave an old-name spec on the moved module.
- **One table** makes removal the deletion of rows (D4), and the tests enumerate the table, so a move
  without a row, or a row without its tests, cannot pass.

### D4 — The deprecation plan

1. **Deprecated in the first release that contains each move.** Wave-1 rows use 0.37.0, the first
   release cut from the development line that contains them; if that release takes another number,
   the rows change with it before it ships. Rows added by later waves use their own first release,
   and a later move leaves callers a documented migration window before 2.0.0.
2. **Removed in 2.0.0**, every row. The version is fixed here; the date is the maintainer's. The old
   paths survive 1.0, so callers can change import paths at any point across the 1.x line, apart from
   the runtime changes 1.0 brings (ADR-0125).
3. **Warning class**: `DeprecationWarning`, with the text in D3. Python shows it by default in code
   run as `__main__` and under pytest, and hides it in imports made inside other installed packages,
   so an application sees it where it imports and a library sees it in its own test run.
   `FutureWarning`, shown to every end user of an application, was weighed and lost: end users cannot
   act on a library's import path.
4. **Tests**, `tests/test_moved_modules.py`, run by the CI `test` job on Python 3.10 to 3.14 on every
   pull request, over every row and every module under each moved package:
   - `test_the_table_has_moves_and_every_package_has_modules`
   - `test_old_name_is_the_new_module_and_warns_once`: same object, real spec, one warning naming
     both paths and the removal release, attributed to the importing file
   - `test_import_statement_warning_points_at_the_importing_line`
   - `test_objects_are_shared_and_pickles_naming_the_old_path_load`
   - `test_reload_through_the_old_name_keeps_the_real_spec`
   - `test_two_old_names_imported_at_once_leave_the_module_spec_alone`: two old names of one module
     imported in two threads, with the loader steps interleaved in a fixed order
   - `test_an_old_package_not_yet_imported_is_described_as_a_package`: spec and resource reads
     through an old name in a fresh interpreter
   - `test_finder_is_installed_once_and_first_on_the_import_path`
   - `test_importing_lionagi_imports_nothing_from_lionhub`
   - `test_names_outside_the_table_are_left_to_the_normal_import_system`
   - `test_no_old_module_is_still_on_disk` and `test_finder_steps_aside_for_a_module_still_at_its_old_path`
   - `test_a_package_moved_inside_another_has_one_module_under_every_name`
   - `test_no_source_file_imports_an_old_name`, with `test_the_reference_check_sees_each_form_of_old_reference`:
     no module in either root imports an old path (relative imports resolved), so only code outside
     this repository ever reaches the finder
   - `test_removal_release_is_ahead_of_this_version`: fails once `lionagi.__version__` reaches a row's
     `removed_in`, so the 2.0.0 version change cannot pass CI while any row remains
   - `test_changelog_names_every_move`: each row's line, with its removal release, is in the CHANGELOG
   - D2's and D5's tests

   The CI `core-install` job imports `lionhub.orchestration` and checks that an old path is the same
   module object as its new path, from a non-editable install with base dependencies only, run
   outside the checkout (D1).
5. **CHANGELOG**: the release that adds rows has a Changed entry that says what moved and why, and a
   table of old path, new path and removal release, one line per row.
6. **The removal**: one change at 2.0 deletes every row, `lionagi/_moved.py`, its `install()` call, the
   old-path resolution in `Element` (D5) and the tests that enumerate the table, after exporting the
   table as the published mapping D5 requires. Tests that old imports fail, and that data converted
   by the published procedure loads without the table, replace the compatibility tests. The
   CHANGELOG's Removed section repeats the import table and the installation change (D1). Item 4's
   version test is what forces the change.

### D5 — Persisted names

The class-path allow-list is `lionagi.` plus the new prefix of each moved package, taken from the
table, which in wave 1 adds `lionhub.orchestration.` (ADR-0001 Amendment 2). The metadata check in
`lionagi/protocols/generic/element.py` reads an old path as the class's path at its new module and
returns the metadata naming that path:

```python
def _current_path(lion_class: Any) -> Any:
    """The dotted path a ``lion_class`` names today: one written under a module that has
    since moved (``lionagi._moved.MOVED``) reads as the class's path at its new module."""
    if isinstance(lion_class, str):
        return _resolve_moved(lion_class) or lion_class
    return lion_class

# in the validator
if "lion_class" in val:
    current = _current_path(val["lion_class"])
    if current != cls.class_name(full=True): ...      # a different class: refused
    if current != val["lion_class"]:
        val = {**val, "lion_class": current}
```

Exact semantics:

- **Data written before a move** names the old path. `Element.from_dict` loads it through the old
  module name (D3) whether or not the process has imported the class; `model_validate` on the class
  accepts it; saving the loaded object writes the new path. That holds for an element nested inside
  another one as well, since the nested element is saved from the metadata the check returned. It
  covers class paths in element metadata only: other strings in user data that name a moved module,
  such as a stored callable path, are not rewritten, and keep loading until 2.0 through the old name.
- **Data written after a move** names the new path and loads in a process that has not imported
  `lionhub`, because the moved packages' prefixes are allowed.
- **A path outside the allow-list** is still refused, the error names `register_type_prefix`, and
  nothing from `lionhub` is imported. The tests refuse `lionhubx.` and `lionhub_x.`, which share
  letters with an allowed prefix, a class in the top-level `lionhub` package, and `lionhub.canon`,
  a `lionhub` package outside the table. A malformed path under an allowed prefix (an empty segment
  or a leading dot) is refused before anything is imported.
- **What the prefixes reach**: every module under a moved package's new prefix also imports by its
  old name under `lionagi.` (D3), with the same parent packages, so the added prefixes make no new
  import-time code reachable from data. A module added later under a moved prefix is admitted the
  way a new `lionagi` module is. Admitting anything else in `lionhub`, a new top-level package such
  as `canon` included, is a new allow-list decision (ADR-0001 Amendment 2), as is the 2.0
  distribution split. `test_the_allow_list_adds_only_the_moved_packages_to_lionagi` fails when the
  added prefixes stop matching the table. The prefixes check names, not distributions: an allowed
  module's top-level code runs when data names it, as for any `lionagi.` module.
- **Tests**: for every `Element` subclass a moved module defines, a fresh interpreter, asserted to have
  imported nothing from `lionhub` before reading the data, loads data naming the new path and data
  naming the old one, and checks the loaded class and that saving it gives the new-path data back;
  `model_validate` is checked with old metadata. The form sample nests an element whose metadata
  names its own class, so the nested path is checked too
  (`test_the_form_sample_nests_an_element_that_names_its_class`).
  `test_every_moved_element_class_has_a_sample` fails a moved class that has no sample instance.
  Removing the moved prefixes from the allow-list, restoring the exact-match metadata check, or
  returning the metadata unchanged fails these tests.
- **Condition on each move**: a move of a module that defines a registered or serialized class lands
  with these tests in the same change. A move with no such class says so in its description, with the
  AST read that shows it.
- **Pickles**: as D3; they load under the old module name until 2.0.
- **Logger names** do not change. A moved module whose logger took its name from `__name__` gets
  that old name as a literal in the move (`lionagi.orchestration.patterns`,
  `lionagi.dispatch.outbox`); the engines already used the literal `lionagi.engines`. Logging
  configured on the `lionagi` logger therefore still captures them. They are a logging-configuration
  surface, not import paths; the old-reference test skips `getLogger` arguments for this reason.
  Renaming them belongs to the 2.0 split and its release notes.
- **Run-store rows**: the dispatch outbox records a dispatch kind and a destination, never a module
  path, so stored rows are unaffected.
- **Search before each move**: the change searches persisted data that could name the moved modules
  (saved runs and branches, profiles and playbooks, run stores) and reports in its description the
  population searched, how many records name an old path, and how many of those are `lion_class`
  values.
- **Entry points and configuration strings** in this repository that name a moved module are
  rewritten in the move change.
- **At 2.0**: the table goes (D4), so neither old imports nor old class paths resolve at run time.
  Before the table is deleted it is published as a fixed, versioned old-to-new mapping, with a
  standalone conversion procedure for the supported stored formats. The procedure covers nested
  class paths and declared class, type and callable fields outside `lion_class`, works on files it
  is given, keeps the originals, reports references it cannot resolve, and never replaces matching
  text wholesale. The mapping is data for that procedure; it registers no import alias. Loading and
  saving under a 1.x release is a migration only for formats whose conversion has been shown
  complete; the 2.0 release notes list those formats and the manual steps for other configuration
  and third-party entry-point strings. Removing the table is not evidence that stored data has
  migrated.

### D6 — Order: three waves by import graph

| Wave | Packages | Imported today by | Prerequisite |
|------|----------|-------------------|--------------|
| 1 | `orchestration`, `engines`, `dispatch`, `work` (7,801 lines) | `cli`, `studio` and each other | none: the core never imports them |
| 1, held | `integrations` (548) | nothing | open outside contributions that edit it land or close first, so their authors do not rebase across a move |
| 2 | `cli`, `mcp`, `studio` | each other; `state` imports `cli` in 2 files | the `cli`/`studio` cycle broken (Studio imports private `lionagi.cli` modules 32 times) and the `state`-to-`cli` inversion removed; the Studio backend goes to `apps/studio/backend`, outside the wheel; the daemon and its scheduler go into `lionhub` |
| 3 | `providers`, `tools`, `agent`, `casts`, `hooks`, `plugins`, `lndl` | the core: `providers` by `service` (3 files) and `operations` (2); `tools`, `agent`, `casts` by `session` (1 each); `hooks` by `operations` (4) and `session` (2); `plugins` by `service` (1) and `protocols` (1); `lndl` by `operations` (2) | a seam in the core for each, first. For providers (amended by Amendment 3), the record before the move specifies discovery, key ownership and duplicates, contract versions, lazy selection, behaviour when a provider is missing or fails, and context-window metadata. Both endpoint registration (`lionagi/service/connections/registry.py`) and token-budget lookup (`lionagi/service/token_budget.py`) use that contract instead of the module paths they list today. A fresh process resolves today's `iModel(provider=...)` call throughout 1.x with no earlier `lionhub` import and no new registration step for the caller, shown with `lionhub` present and absent; a core-only 2.0 install reports an absent provider at the public boundary rather than falling back silently |

Wave 1's destinations are `lionhub.orchestration`, `lionhub.orchestration.engines`,
`lionhub.orchestration.dispatch` and `lionhub.orchestration.work`.

Every move change carries:

1. The files moved with no edits beyond import rewrites, a package docstring that names its own
   path, and the logger names D5 keeps, so the change shows them as renames.
2. Its table rows and CHANGELOG lines.
3. Every caller in this repository on the new path (D4 item 4).
4. The import graph at its head in its description: which packages import the moved one, showing the
   core is not among them, and D2's set at that head.
5. D5's persisted-name search, and whether D5's class condition applies, with the tests or the AST
   read.
6. Citations of moved files in records a test checks against the code (ADR-0120's outbox lines) moved
   to the new path. Other records, archived records and recorded benchmark runs keep the paths they
   were written with.

`lionhub`'s paths become public with the first release that ships them. Until then they can change on
the development line at no cost to anyone; from then on they are kept until 2.0, and the table maps
only paths under `lionagi.` (`test_no_old_module_is_still_on_disk` asserts it). D7's layout for the
wave-1 packages, and the lookup behaviour D3 states for old names, are therefore settled before
0.37.0 ships.

Waves 2 and 3 start with their own change lists. Packages move one change each, or one change per
set that must move together.

### D7 — lionhub's layout

Everything in `lionhub` lives under `lionhub/src/lionhub/`, and `lionhub/src/` holds nothing else. An
editable install makes every directory in `lionhub/src/` a top-level package (D1), and generic names
such as `tools`, `hooks` or `providers` there would collide with unrelated packages of the same name.

The target layout follows the maintainer's draft; this record's departures are listed after it.

```text
lionhub/src/lionhub/
  orchestration/   patterns, prompts                       <- lionagi.orchestration       wave 1
    engines/                                               <- lionagi.engines             wave 1
    dispatch/                                              <- lionagi.dispatch            wave 1
    work/                                                  <- lionagi.work                wave 1
    flow, spawn, job, wait, kill, team, notify             <- CLI orchestration, MCP jobs wave 2
    schedule, scheduler/                                   <- the Studio scheduler        wave 2
  serve/           cli, daemon, mcp, server                <- lionagi.cli, lionagi.mcp    wave 2
  providers/       anthropic/, openai/, gemini/, openrouter, groq, ollama, ...
                                                           <- lionagi.providers           wave 3
  tools/           context, skill, memory, web, watch, computer/
                                                           <- lionagi.tools               wave 3
  agent/           cast, role, seat, communication, inbox  <- lionagi.agent, lionagi.casts  wave 3
  hooks/           gate, guard, hooks.toml, scripts/       <- lionagi.hooks               wave 3
  resources/       registry, plugin                        <- lionagi.plugins             wave 3
  lndl/                                                    <- lionagi.lndl                wave 3
  integrations/    ruvector/, khive/ (new)                 <- lionagi.integrations        held
  canon/           charter, policy, evidence, governance, enforcement, authorization,
                   runtime/, dsl/, services/               new code (concept ports, below)
```

Departures from the draft:

- **Wave-1 packages keep their package form and module names** (`engines/`, `dispatch/`, `work/`)
  instead of the draft's single-file slots `engine.py`, `notify.py` and `task.py`. A whole-package
  move needs one table row and changes no module's content. Folding 5,998 lines of engines into one
  file, or the outbox into a notify module, is a refactor; riding on a move, it would make the move's
  rename diff unreadable, and after 0.37.0 it would cost a second set of compatibility paths.
- **Spelling**: the draft's `integerations` becomes `integrations` and `spwan` becomes `spawn`. These
  become public import paths, kept until 2.0 once shipped.
- **The draft's directories directly under `lionhub/src/`** move under `lionhub/src/lionhub/` (the
  rule above).
- **Slots with no code yet** (`orchestration/escalate`, `orchestration/task`, `tools/discovery`,
  `tools/knowledge`, `resources/mod`, the `e2b` and `fly` computer backends) are created with their
  first code, not as empty modules: an empty public module is an import path that promises nothing
  and still has to be kept until 2.0.

**Concept ports.** `tools/watch` from lion1, `tools/computer/` following khive-computer, and the
`canon/` slots (governance, guard, enforcement) from a governance library enter as new code written
from those concepts (ADR-0125 D1), not as copies. Each port is its own change with its own tests. A
port is not a move, so it adds no table row.

**DEFERRED: the CLI's home and who owns `li` at 2.0.** The draft puts it in `serve/cli`. Until 2.0 the
`lionagi` distribution declares `li` wherever the code sits, and only the entry point's target
changes when the CLI moves. Before the wave-2 CLI move, its record chooses the CLI's public path, the
one distribution that declares `li` from 2.0, and what a core-only 2.0 install has: no `li`, or a
launcher with no dependencies that says what to install. It documents the install command for the
working CLI and tests fresh installs and upgrades for missing, stale and doubly owned scripts. Where
the code sits does not by itself decide who owns the script. D2 already fixes that the CLI leaves the
core package, because it imports `lionhub`.

**DEFERRED: the Studio backend's form.** The Studio application (operator, routes, app shell, frontend
connection) goes to `apps/studio/backend`, outside the wheel, and not into `lionhub`; the daemon and
its scheduler go into `lionhub`. Two forms remain:

- A plugin the daemon loads: one port and one lifecycle, and simpler pairing for a browser page.
  Against it: backend code runs inside the daemon's process.
- Its own process beside the daemon: a clean boundary. Against it: two ports and two lifecycles.

There is no plugin host today. The fixed HTTP module list in `lionagi/_auto.py` and its loader have
no caller yet, unlike the CLI declarations in the same module, and are a candidate starting point
for one; the running app mounts its routes through `lionagi.studio.registry`.

A separate record chooses the form before any wave-2 change depends on the backend boundary, even a
change filed as a CLI move. It defines activation and absence, version compatibility, route
ownership, start, stop and readiness, failure and cancellation isolation, state ownership, and how
the browser and the CLI connect and authenticate. The same record defines how the free container
image gets and installs its backend and frontend from versioned public sources, reproducibly and
without private credentials, with its supported versions and release and rollback steps: moving the
backend outside the wheel needs an explicit install or build step, since `.[studio]` adds server
dependencies but does not install the backend. Wave 1 does not depend on the backend's form.

## Consequences

- The core's package loses 7,801 lines in wave 1 and, through waves 2 and 3, the CLI, MCP, Studio and
  integration code; ADR-0125's core changes are read against a smaller package.
- A layer inversion is a test failure (D2), before ADR-0122's full checker exists.
- Code outside this repository gets one warning per old module, naming the new path and the removal
  release, and imports, patches and pickles that name an old path keep working before 2.0. What
  changes without a warning is listed in D3: listing `lionagi.__path__`, static type checking of old
  paths, and the new path in `__name__`, `__module__`, `__file__` and tracebacks.
- Code that depends on `lionhub` adds it to its requirements at 2.0 (D1).
- Contributors work with two import roots, and the source roots are listed in every place D1 names;
  a new place that lists roots and misses `lionhub` is a tool that silently skips it.
- The finder is import machinery that runs first for every import in a process that imported
  `lionagi`. Its per-import cost is one scan of the table, which grows with the waves.
- The warning's attribution depends on CPython's import frames (the finder skips the frozen
  importlib frames when it computes `stacklevel`). It is tested on 3.10 to 3.14; a Python that
  changes that frame layout misattributes the warning, and the tests catch it.
- Reversal costs: D1 is cheap to reverse until 2.0. A table row is cheap to add; removing one before
  its version is a compatibility break. D4's 2.0.0 is a promise to callers once 0.37.0 ships. D7's
  paths for shipped packages are fixed from the release that ships them until 2.0.

## Alternatives considered

- **Separate `lionagi` and `lionhub` distributions now.** It would have given `lionhub` its own
  release cadence from the start, and declaring the dependencies both ways could keep old callers
  working while the compatibility layer and `lionhub` depend on each other. That needs compatible
  version constraints, coordinated publication and upgrade tests while the boundary is still
  changing (P4). No benefit in independent releases or install size has been shown that would
  justify that cost before ADR-0122 D7's gates are met.
- **A `lionagi[hub]` extra installing a separate `lionhub`.** A separate distribution with a name on
  it: an old path then fails for anyone without the extra, a break in the first release.
- **Old paths that resolve only when `lionhub` is installed, else an `ImportError` naming the
  install.** A good shape for two distributions; inside one wheel there is no install without
  `lionhub`, so it has nothing to do.
- **A subpackage, `lionagi.hub`.** Moves every caller twice (D1).
- **Namespace packages.** ADR-0122 D7 does not approve them, and two regular packages need no
  namespace machinery.
- **Star-import files, module `__getattr__` facades, eager aliasing.** D3, "Why this way".
- **No removal, keeping the old paths indefinitely.** Against ADR-0122 D8; the table and the finder
  would become permanent import machinery.
- **Rewriting persisted data on upgrade.** ADR-0122 D8: persisted data is not rewritten merely because
  Python ownership moved. The library also cannot find every file its users saved.
- **Moving every non-core package in one change.** Too large to read as one change, and it would
  move packages the core imports before the core has seams for them, making the core import
  `lionhub`. One wave per layer of the import graph keeps each change's reach readable.
- **An import-linting tool for D2.** A new dependency and configuration for one rule; ADR-0122 D6's
  checker subsumes the test when it lands.

## Notes

- Moves land on the development line; 0.37.0 is the first release that contains them (D4).
- Implementation, per decision: D1 to D5 are implemented for the four wave-1 packages by #3752, #3753
  and #3754, merged on the development line (see Implementation-status). D4's removal and waves 2 and 3
  remain.
