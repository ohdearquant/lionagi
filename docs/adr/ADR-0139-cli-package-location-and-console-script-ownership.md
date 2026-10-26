# ADR-0139: CLI package location and console-script ownership

- **Status**: Proposed; D1 and D5 stay Proposed pending a project decision on where the CLI lives, as does the date of the 2.0 release, which this record does not set
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: cli-surface
- **Date**: 2026-10-26
- **Relations**: resolves the CLI decisions deferred by ADR-0136 D7; amends its A4 item 6, B2 step 6 and D7 layout; amends the location references in ADR-0062 D1–D5; specializes the optional base-launcher choice in ADR-0122 D8; complements ADR-0138.

**Source HEAD**: `1717674a2140d3571fcb90e3f68204845048ef80`.

All source citations refer to that revision.
Destination paths, interfaces and tests below are requirements, not claims of existing implementation.
The decision takes effect only when adopted. ADR-0138 is also Proposed at this revision
(`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:3`).

## Context

**P1. Code location and executable ownership are different contracts.**
The current project declares `li = "lionagi.cli.main:main"` and packages both import roots
(`pyproject.toml:78`, `pyproject.toml:276`).
ADR-0136 already requires one distribution through 1.x and a separate `lionhub`
distribution depending on core at 2.0. Its release record must establish exclusive
package-file and console-script ownership and test installation transitions
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:352`,
`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:396`).
It explicitly leaves the CLI path, script owner and core-only executable policy open
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:786`).

**P2. Relocating the CLI without extracting shared code preserves the wrong dependency.**
A static AST inventory at the source HEAD finds 46 Python files in `lionagi/cli`,
32 Studio-to-CLI import statements in 14 files, and 11 CLI-to-Studio statements in
four files. These match Amendment 1's counts
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:202`).
Representative edges include shared run persistence, scheduler parser construction,
and schedule declaration/export
(`lionagi/studio/operator/coordinator.py:539`,
`lionagi/studio/scheduler/subprocess.py:279`,
`lionagi/cli/machine_schedule.py:322`,
`lionagi/cli/machine_schedule.py:382`).
There is additionally a literal dynamic import of the mixed Studio CLI module;
it is not one of the eleven static statements (`lionagi/cli/main.py:33`).
The full 46-file partition is specified in D2.

**P3. Command declarations are not command discovery.**
The populated catalog currently contains 21 canonical commands and a token map;
a decorator only attaches metadata, and loading imports the module of an already
selected seed (`lionagi/_auto.py:144`, `lionagi/_auto.py:274`,
`lionagi/_auto.py:291`, `lionagi/_auto.py:388`).
Moving only handlers or decorators would leave fresh startup dependent on a core
catalog or accidental import order. A4 already requires the populated catalog and
selection/loading entry points to leave core before the first command target moves
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:218`).

**P4. Import compatibility cannot establish installer correctness.**
The alias loader supports longest-prefix lookup, module identity and code loading,
but the current table has no CLI row
(`lionagi/_moved.py:46`, `lionagi/_moved.py:55`,
`lionagi/_moved.py:118`, `lionagi/_moved.py:132`).
A working old script does not prove that an upgrade installed the intended new
entry point or left a single file owner.

**P5. A package move must not restore an older command snapshot.**
ADR-0062 records composition, routing, execution locus, parsing and output
contracts. The current implementation additionally has explicit machine-mode
routing and lazy selected-command construction
(`docs/adr/ADR-0062-cli-command-surface-ownership.md:66`,
`lionagi/cli/main.py:291`, `lionagi/cli/main.py:353`).
Relocation preserves the behavior at this HEAD and subsequent separately adopted
behavior changes.

| Concern | Decision |
| --- | --- |
| Public CLI location | D1, Proposed pending a project decision: `lionhub.cli`, with `lionhub.cli.main:main` as composition root. |
| Shared code and move form | D2: eight whole-module moves plus shared-function extraction in step 3; one residual CLI-package row in step 6. |
| Fresh command discovery | D3: `lionhub.cli.catalog` owns the populated catalog and loading APIs. |
| 1.x executable and aliases | D4: `lionagi` remains script owner; its target changes at step 6; old imports survive through 1.x. |
| 2.0 executable and installation | D5, Proposed pending a project decision: `lionhub` alone owns `li`; core-only installs have no `li`. |
| Distribution transition | D6: prove metadata and file ownership across fresh installs, upgrades, removals and rollback. |
| Existing command contract | D7: amend ADR-0062's paths without replacing its behavioral decisions. |

Out of scope:

- Changing command spelling, parsing, output, status codes, execution locus,
  dependency defaults or optional-feature behavior. Actual moves remain relocation
  and import rewrites, as required by ADR-0136 D6 and B2
  (`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:86`,
  `docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:712`).
- The Studio attachment mechanism, application distribution and lifecycle
  corrections. ADR-0138 owns those boundaries; its attachment form remains Proposed
  (`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:45`).
- A general third-party CLI discovery protocol. Automatic plugin bootstrap is
  expressly deferred (`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:278`).
- Exact release versions, compatibility constraints, platform/installer support and
  artifact publication. ADR-0136 D1's distribution release record owns them
  (`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:402`).

## Decision

### D1 — The public CLI package is lionhub.cli

Proposed, pending a project decision on where the CLI lives. Placing it in `lionhub` means
`pip install lionagi` alone no longer provides a CLI from 2.0 (D5), and every script that calls
`li` then depends on the hub distribution. D2, D3, D4, D6 and D7 are this record's engineering
decisions; where they name `lionhub.cli` paths, those paths follow D1.

The canonical targets from step 6 are:

```text
lionhub/src/lionhub/cli/
    __init__.py
    __main__.py
    main.py
    catalog.py
    schedule.py
    studio.py
    ... remaining command adapters ...

console target: lionhub.cli.main:main
module command: python -m lionhub.cli
```

```python
# Proposed canonical interface: lionhub/src/lionhub/cli/main.py
def main(argv: list[str] | None = None) -> int: ...
```

`__main__` calls `main()` and exits with
its return code, preserving the existing launcher pattern
(`lionagi/cli/__main__.py:9`).

The CLI composes local commands, observation, launchers and service clients.
It does not require a running daemon merely because it is installed. This follows
ADR-0062's independent execution loci
(`docs/adr/ADR-0062-cli-command-surface-ownership.md:112`).
The existing `serve` package describes server processes
(`lionhub/src/lionhub/serve/__init__.py:4`); a sibling `cli` is a clearer home
for the broader operator surface and yields the shorter module invocation.

This amends the `serve/cli` illustration and spelling in ADR-0136 D7, A4 item 6,
and B2 step 6. Those spellings were subject to the deferred CLI decision, not a
closed destination
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:275`,
`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:786`,
`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:163`).
Neither `lionhub.cli` nor `lionhub.serve.cli` violates D7's generic-name rule:
that rule prohibits sibling directories directly under `lionhub/src/`
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:737`).

**Failure tests:** `test_cli_canonical_target` and
`test_cli_module_invocations_in_fresh_processes` fail if the package, target,
relative imports, argument handling or exit propagation differs from this contract.

### D2 — Shared implementations leave before the remaining CLI package

Follow Amendment 2 B2's order. The catalog moves first within step 3.
Preparatory extraction is separate from each mechanical move; no service behavior
changes are hidden in a relocation. Steps 4 and 5 retain their scheduler and daemon
boundaries, and step 6 moves the remaining CLI
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:62`).

**Eight complete source modules move during step 3:**

| Source at this HEAD | Step-3 destination | Scope |
| --- | --- | --- |
| `lionagi/cli/_argtypes.py:23` | `lionhub.orchestration.arguments` | Entire module, including both argument classes. |
| `lionagi/cli/_code_identity.py:55` | `lionhub.serve.diagnostics` | Entire module, including its snapshot and diagnostic helpers. |
| `lionagi/cli/_mcp_resolve.py:22` | `lionhub.orchestration.mcp_config` | Entire module and its resolution data type. |
| `lionagi/cli/_project.py:19` | `lionhub.orchestration.project` | Entire module and project-detection helper closure. |
| `lionagi/cli/_providers.py:124` | `lionhub.orchestration.spawn` | Entire module: construction, profiles, and common argument declarations. |
| `lionagi/cli/_runs.py:36` | `lionhub.orchestration.runs` | Entire module, including its sole run-allocation and persistence state. |
| `lionagi/cli/_util.py:14` | `lionhub.orchestration.job` | Entire module: status codes, invocation state, process identity and entity lookup. |
| `lionagi/cli/orchestrate/_checkpoint.py:21` | `lionhub.orchestration.flow.checkpoint` | Entire module and checkpoint state. |

Each row above becomes one exact old-module-prefix row in `MOVED`, with the first
release containing that move as `deprecated_in` and `2.0.0` as `removed_in`.
No forwarding file remains at those eight source paths. This preserves module
identity, including mutable globals: the current run allocation and invocation
state live in `_runs` and `_util`
(`lionagi/cli/_runs.py:78`, `lionagi/cli/_util.py:36`).
Copying these bindings into a facade is not equivalent.

**Shared definitions extracted from modules that retain CLI adapters:**

| Source at this HEAD | Step-3 public owner | Extraction; retained adapter |
| --- | --- | --- |
| `lionagi/cli/orchestrate/__init__.py:394`, `lionagi/cli/orchestrate/__init__.py:425` | `lionhub.orchestration.flow.parsing` | Orchestration parser construction and its MCP-argument helper; retain command dispatch and execution wrappers. The common argument declarations come from `orchestration.spawn`. |
| `lionagi/cli/orchestrate/_orchestration.py:2137` | `lionhub.orchestration.runs` | `register_branch_hook` and its dependency on the same message-handler implementation; retain the orchestration execution adapter. |
| `lionagi/cli/status.py:37`, `lionagi/cli/status.py:55` | `lionhub.orchestration.inspection` | Status constants and the resolution functions from `_fetch_by_id` through `_resolve_primary_session` (`lionagi/cli/status.py:262`); retain the subsequent progress, presentation and dispatch helpers. |
| `lionagi/cli/kill.py:32`, `lionagi/cli/kill.py:509`, `lionagi/cli/kill.py:752` | `lionhub.orchestration.kill` | Process identity, child lookup, termination, cancellation persistence and degraded-run handling through the helper group preceding `_do_kill`; retain command selection, parser and presentation. |
| `lionagi/cli/team.py:42` | `lionhub.orchestration.team` | `read_team_json`, the concrete shared reader; retain other team command functions. |
| `lionagi/cli/engine.py:23`, `lionagi/cli/engine.py:56` | `lionhub.orchestration.engine_definitions` | The shared kind metadata and class lookup; retain parser and execution adapter. |
| `lionagi/cli/mirror.py:25`, `lionagi/cli/mirror.py:111`, `lionagi/cli/mirror.py:856` | `lionhub.serve.mirror` | Mirror constants, state and implementation through `mirror_forever`; retain argument parsing, `_run` and `run_mirror`. Extract its wheel-owned configuration dependency before or with it. |

The table fixes ownership and the source groups, not new command semantics.
Extraction introduces public service names for private helpers used across the
boundary; old callable/class exports retained by a mixed module refer to the
same implementation. Mutable state has one service owner. CLI registration
decorators remain on the adapter functions defined in the catalog's selected
module: a reexported decorated handler is not a replacement for a correctly
defined adapter, because the existing loader checks the defining module and
exactly one matching marker (`lionagi/_auto.py:395`).

The shared dependency closure is part of step 3:

- `_runs` uses the moved MCP resolution type, project resolver, job utilities
  and shared process-identity helper
  (`lionagi/cli/_runs.py:21`, `lionagi/cli/_runs.py:33`,
  `lionagi/cli/_runs.py:1484`, `lionagi/cli/_runs.py:1882`,
  `lionagi/cli/_runs.py:2081`).
- Checkpoint resolution uses public inspection services instead of the retained
  status adapter (`lionagi/cli/orchestrate/_checkpoint.py:227`).
- Shared code and Studio use the existing neutral log emitters. Their identity
  already comes from `lionagi._log_channels`; CLI-specific formatter setup stays
  with the CLI (`lionagi/cli/_logging.py:10`, `lionagi/cli/_logging.py:91`).
- Mirror settings become wheel-owned before or with mirror extraction, preserving
  the current configuration value and defaults rather than importing application
  configuration (`lionagi/cli/mirror.py:21`;
  `docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:168`).
- Shared parser construction must not import execution handlers. The scheduler
  currently reaches the full orchestration adapter for this parser
  (`lionagi/studio/scheduler/subprocess.py:279`,
  `lionagi/cli/orchestrate/__init__.py:29`).

In step 3, after catalog creation, extract the wheel-owned adapters
`lionhub.cli.schedule` and `lionhub.cli.studio` from the mixed
`lionagi.studio.cli` module and reseed their catalog targets. The former
uses public schedule parsing/validation/export services and daemon client helpers;
the latter alone selects the retained application. Their old exports remain
available in the retained Studio module through 1.x; there is no broad
`lionagi.studio` move row. Schedule/timezone/action-kind services follow
`lionhub.orchestration.schedule` and `lionhub.orchestration.scheduler`
as already allocated by the adjacent records
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:47`,
`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:172`,
`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:173`).
The implementation defines the detailed schedule-service API there.
Ordinary schedule handling must no longer use the dynamic mixed-module import
at `lionagi/cli/main.py:33`.

**The step-6 residual set is these 38 existing files**, relative to
`lionagi/cli/`. Mixed files contain their retained adapters after extraction.
The eight whole-module rows above and this list partition the 46-file AST inventory.
Each source citation below identifies the module header.

- `lionagi/cli/__init__.py:1`
- `lionagi/cli/__main__.py:1`
- `lionagi/cli/_agent_depth.py:1`
- `lionagi/cli/_api_usage.py:1`
- `lionagi/cli/_context_from.py:1`
- `lionagi/cli/_logging.py:1`
- `lionagi/cli/agent.py:1`
- `lionagi/cli/dispatch.py:1`
- `lionagi/cli/doctor.py:1`
- `lionagi/cli/engine.py:1`
- `lionagi/cli/hooks.py:1`
- `lionagi/cli/invoke.py:1`
- `lionagi/cli/kill.py:1`
- `lionagi/cli/machine.py:1`
- `lionagi/cli/machine_schedule.py:1`
- `lionagi/cli/main.py:1`
- `lionagi/cli/mcp.py:1`
- `lionagi/cli/mirror.py:1`
- `lionagi/cli/monitor.py:1`
- `lionagi/cli/orchestrate/__init__.py:1`
- `lionagi/cli/orchestrate/_common.py:1`
- `lionagi/cli/orchestrate/_control.py:1`
- `lionagi/cli/orchestrate/_finalize.py:1`
- `lionagi/cli/orchestrate/_harvest.py:1`
- `lionagi/cli/orchestrate/_manifest.py:1`
- `lionagi/cli/orchestrate/_notify.py:1`
- `lionagi/cli/orchestrate/_orchestration.py:1`
- `lionagi/cli/orchestrate/_quiescence.py:1`
- `lionagi/cli/orchestrate/_round_records.py:1`
- `lionagi/cli/orchestrate/fanout.py:1`
- `lionagi/cli/orchestrate/flow.py:1`
- `lionagi/cli/plugin.py:1`
- `lionagi/cli/skill.py:1`
- `lionagi/cli/state.py:1`
- `lionagi/cli/stats.py:1`
- `lionagi/cli/status.py:1`
- `lionagi/cli/team.py:1`
- `lionagi/cli/wait.py:1`

Step 6 relocates that residual package to `lionhub.cli` with one parent row:

```python
Moved("lionagi.cli", "lionhub.cli", first_cli_move_release, "2.0.0")
```

Here `first_cli_move_release` means the actual release string established when
step 6 ships, not a new runtime variable. Keep the eight more-specific rows:
`lionagi.cli._runs` must still resolve to `lionhub.orchestration.runs`,
rather than a nonexistent `lionhub.cli._runs`.
The loader already selects the longest matching prefix and supports non-package
specs (`lionagi/_moved.py:55`, `lionagi/_moved.py:149`).

Before adding these rows, extend the move-test enumerator to accept a module
target without `__path__`; it currently assumes every target is a package
(`tests/test_moved_modules.py:48`).
Enumerate both specific rows and the residual package, test each import order,
and retain stale-file detection. This is a test-support change, not permission to
replace the alias loader or weaken identity requirements.

Preserve logger names when implementations move. The source includes
`logging.getLogger("lionagi.cli")` in the run module
(`lionagi/cli/_runs.py:791`).
The existing source checker admits old logger strings only when its resolved
destination contains the current module; split destinations require a narrow
original-logger-ownership check, not renaming the logger or ignoring arbitrary
old strings (`tests/test_moved_modules.py:613`).

**Failure tests:** `test_step3_shared_dependency_closure`,
`test_move_table_handles_module_and_package_rows`,
`test_specific_cli_rows_survive_parent_move`,
`test_shared_state_has_one_owner`, and `test_preserved_logger_names`.
The first requires zero Studio imports of private CLI modules and zero shared
service imports of CLI adapters, including relative, local, type-only and literal dynamic imports.
Local schedule validation/export also runs with application imports blocked.

### D3 — The CLI owns its populated catalog and startup

Step 3 creates `lionhub.cli.catalog`. It owns the populated seed tuple,
token index, realized-command cache and these existing API shapes:

```python
def iter_cli_seeds() -> tuple[CliSeed, ...]: ...
def seed_for(command_name_or_alias: str) -> CliSeed | None: ...
def command_exists(command_name_or_alias: str) -> bool: ...
def load_cli_command(seed: CliSeed) -> Registration: ...
def build_cli_parser(selected: CliSeed | None) -> CliBuild: ...
```

These signatures and their current lazy behavior are at
`lionagi/_auto.py:373`, `lionagi/_auto.py:388`, and `lionagi/_auto.py:430`.
The composition root imports this catalog directly before selection.
MCP projection and machine-command classification use the same catalog, not copies
(`lionagi/cli/main.py:13`, `lionagi/cli/main.py:353`,
`lionagi/cli/machine.py:824`,
`lionhub/src/lionhub/serve/mcp/projection.py:32`).

Keep command names, aliases, order, help metadata, special routing and error behavior
unchanged. Root discovery does not import every command implementation. Selection
loads the explicitly cataloged implementation and enforces the existing marker
contract. Unknown tokens remain unknown; duplicate tokens and missing or duplicate
markers remain errors
(`lionagi/_auto.py:257`, `lionagi/_auto.py:378`,
`lionagi/_auto.py:400`, `lionagi/_auto.py:453`).

Built-in commands implemented outside core, including scheduling and plugin
management, are discoverable because their declarations are in the hub-owned
catalog. They need neither an earlier hub import nor caller registration.
`casts`, `studio` and `schedule` are included; update targets as their owners
move, including later waves (`lionagi/_auto.py:158`,
`lionagi/_auto.py:173`, `lionagi/_auto.py:224`).
Do not invent third-party command discovery: the current plugin capability schema
has no CLI-command field (`lionagi/plugins/manifest.py:131`).
The step-3 implementation chooses internal compiler factoring and fixtures;
a later extension protocol must separately define external contributions,
selection, conflicts, authorization and compatibility.

`lionagi._auto` remains importable through 1.x with permitted hub-neutral
declaration types/decorator machinery. It contains no populated CLI target list,
catalog state or forwarding loader that imports the hub. This follows A4 and the
retained-module requirement in ADR-0138
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:221`,
`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:188`).
Unused HTTP machinery is handled in step 5, not converted into CLI discovery.

**Failure tests:** `test_fresh_cli_catalog_discovery`,
`test_catalog_loading_stays_lazy`, `test_cli_projection_uses_one_catalog`,
and `test_core_contains_no_cli_targets`. Run the discovery cases in fresh
processes with no preliminary import or registration.

### D4 — The 1.x script changes target without changing distribution owner

Before step 6, the current target remains. From step 6 through the final 1.x release,
the sole `lionagi` project declaration is:

```toml
[project.scripts]
li = "lionhub.cli.main:main"
```

There is still no separately published hub distribution during this window
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:373`).
All repository import callers use canonical moved paths. External old imports
continue to resolve through the table until 2.0; each row carries its own first
deprecation release and the common removal version
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:565`).

A saved older generated wrapper importing `lionagi.cli.main` must continue to
work against a post-move 1.x installation using the aliases. This does not promise
to repair a stale interpreter path, shell command cache, or a wrapper in a different
environment. The installed replacement wrapper must actually target the new entry;
an old wrapper that merely still runs is insufficient.

Both `python -m lionagi.cli` and `python -m lionhub.cli` work throughout the
post-move 1.x window. Compare argument delivery, package-relative imports, stdout,
stderr and process status in fresh subprocesses. Permit only the specified
deprecation diagnostics on the old path. At 2.0 the old module invocation fails.

Rewrite executable module strings, not just imports: the background launch names
the old CLI at `lionagi/cli/orchestrate/__init__.py:1239`, the MCP fallback names it at `lionhub/src/lionhub/serve/mcp/config.py:116`,
and the diagnostic probe names it at `lionagi/cli/doctor.py:28`.
Update the dynamic adapter load at
`lionhub/src/lionhub/serve/mcp/projection.py:526` as well.
Self-recognition must accept both canonical and compatibility spellings during 1.x,
rather than silently dropping old subprocess recognition. Its current module/script
sets and path-tail comparison are at
`lionagi/cli/orchestrate/_orchestration.py:621` and
`lionagi/cli/orchestrate/_orchestration.py:664`.
Old spellings retained solely for recognition are compatibility data, never import
or launch targets. The existing old-reference checker distinguishes imports/call
arguments from set data (`tests/test_moved_modules.py:593`); keep canonical
execution targets and test both recognition forms explicitly.

The D2 exemption is exactly `{cli, studio}` before step 6, `{studio}` afterward
through 1.x, and empty at 2.0. The core gains no new exception or hub-dependent facade
(`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md:29`).

**Failure tests:** `test_1x_upgrade_replaces_li_target_once`,
`test_saved_1x_wrapper_uses_alias`,
`test_cli_module_invocations_in_fresh_processes`,
`test_background_cli_target_and_compatibility_recognition`,
and the existing D2/D4 boundary and removal guards.

### D5 — The hub distribution owns li at 2.0

Proposed, pending the same project decision as D1; the date of the 2.0 release is also a project
decision, which this record does not set.

From the 2.0 core/hub split:

| Distribution | Package ownership relevant here | Declares `li` |
| --- | --- | --- |
| `lionagi` | Core `lionagi` only | No |
| `lionhub` | `lionhub`, including CLI and wheel services | Yes: `lionhub.cli.main:main` |
| Extracted Studio application | Its separately specified application files | No |

A core-only install has neither a CLI script nor a dependency-free stub.
The supported installation command for the working CLI is:

```text
python -m pip install lionhub
```

The hub distribution declares a compatible core dependency that excludes bundled
pre-2.0 releases. Exact version constraints belong to the distribution release
record; this decision does not assign a version series to the hub.
CLI availability does not promise every optional command prerequisite or the
separately installed Studio application. Preserve existing feature selection and
actionable missing-capability behavior; document required optional installations
with the release matrix.

ADR-0122 D8 permits a base-owned diagnostic launcher; it does not require one
(`docs/adr/ADR-0122-feature-boundaries-and-optional-runtime-profiles.md:446`).
This decision selects absence for core-only 2.0. During any earlier window in
which a base wheel still owns the script while CLI dependencies become optional,
D8's dependency-free diagnostic requirement still applies. Moving the script
owner does not authorize eager optional imports or a new dependency-error policy.

A direct core launcher importing `lionhub` would violate D2. A neutral activation
protocol is possible, but would add a second composition boundary solely to retain
a core-owned executable. A diagnostic stub and a hub-owned script cannot both own
the same `li` file. Ownership by the distribution containing the CLI avoids both
problems.

**Failure tests:** `test_core_only_has_no_li`,
`test_hub_owns_exactly_one_li`, and
`test_optional_command_feature_errors_are_unchanged`.

### D6 — Installation transitions prove both declaration and file ownership

The CLI requirements below are inputs to ADR-0136 D1's distribution release record.
A passing command alone is insufficient: inspect `console_scripts` metadata and
the normalized installed-file records for every distribution. Require one owner
of each installed `li` launcher file and zero in a core-only installation.
Account for the launcher's actual platform-specific representation.

| Scenario | Required final result |
| --- | --- |
| Fresh pre-move 1.x | One `lionagi`-owned script with the old target. |
| Fresh post-move 1.x | One `lionagi`-owned script with the new target; old/new module invocation works. |
| Earlier 1.x to post-move 1.x upgrade | One replaced script with new metadata; saved old wrapper still works separately. |
| Fresh core-only 2.0 | No `li`, no hub files, no retained application files. |
| Fresh hub installation, or compatible core then hub | One hub-owned script; compatible core installed; basic CLI works. |
| Both compatible split distributions upgraded | One hub-owned script; no core ownership claim. |
| `pip install -U lionagi` from bundled 1.x to core-only 2.0 | Old script and bundled hub files removed; no CLI remains. This is the documented core-only result, not a missing-script defect. |
| Hub installed after a completed core-only upgrade | One hub owner only, after prior bundled ownership is gone. |
| Remove hub while retaining core | Script and hub files disappear; core remains usable. |
| Remove core while retaining hub | Hub files and its script remain owned by hub, but the declared core dependency is missing. Do not claim the CLI still works; dependency validation must report the break. |
| Reinstall compatible core after its removal | The same hub-owned script works again; no second script owner appears. |
| Remove both distributions in either order | No orphan launcher or shared package file remains. |
| Repair/reinstall the same supported installation | Correct target and exactly one owner remain. |
| Resolver request pins bundled pre-2.0 core together with a separate hub | Fails resolution before installation because the constraints are incompatible. |
| Rollback from split installation | Return to the preserved complete 1.x environment; no file overlay downgrade is assumed safe. |

Adopt a new environment as the documented 2.0 migration path, retaining the
previous complete environment for rollback. This aligns with ADR-0138's proposed
transition (`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:214`).
Exercise the core-only upgrade and sequential hub-install cases as explicit
installer tests; do not promise arbitrary in-place overlay or downgrade sequences.
Installing hub into an existing 1.x environment may cause a resolver to select a
core upgrade; compatible-version metadata alone does not prove that install sequence
safe. Treat it as unsupported until the release record tests the complete transfer.
Exact supported in-place procedures, if any, require evidence in that record. Never run old and new schedulers concurrently against the same state.

The release record must prove fresh installation, upgrade, uninstall and rollback
with real built wheels in isolated environments outside the checkout. Its evidence
must include package-file ownership as well as the script; this CLI record does
not discharge the seven distribution-split conditions in ADR-0122 D7
(`docs/adr/ADR-0122-feature-boundaries-and-optional-runtime-profiles.md:416`).

**Failure tests:** `test_cli_distribution_transition_matrix`,
`test_console_script_metadata_matches_file_owners`,
`test_mixed_bundled_and_split_install_is_rejected`, and
`test_cli_environment_rollback`.

### D7 — ADR-0062 keeps its behavioral decisions

Amend ADR-0062 in place as each destination becomes canonical:

| Decision | Amendment | What remains unchanged |
| --- | --- | --- |
| D1 | Cite `lionhub.cli.main:main`, its catalog and relocated registration adapters. Distinguish composition ownership from distribution ownership. | One `li` namespace and `main(argv) -> int`; top-level composition still owns discovery/routing. |
| D2 | Update pre-parser helper anchors to their moved files. | Ordered interception and rewriting, including later separately established routing. |
| D3 | Cite `lionhub.cli.schedule`, `lionhub.cli.studio` and the public services those adapters call. | Command spelling remains independent of local, observational, launcher or HTTP-client execution. |
| D4 | Update parser/helper anchors. | Sentinel handling, argument folding, diagnostics and exit behavior. |
| D5 | Update output-handler anchors. | Each explicit output contract, including the current machine surface; no renderer redesign. |

The source decisions are at
`docs/adr/ADR-0062-cli-command-surface-ownership.md:66`,
`docs/adr/ADR-0062-cli-command-surface-ownership.md:139`,
`docs/adr/ADR-0062-cli-command-surface-ownership.md:193`,
`docs/adr/ADR-0062-cli-command-surface-ownership.md:260`, and
`docs/adr/ADR-0062-cli-command-surface-ownership.md:294`.
None is superseded behaviorally by this record.

The source HEAD, not the older illustrative command tree, is the characterization
baseline. Preserve early machine routing, invocation accounting, missing-module
handling and command-specific errors
(`lionagi/cli/main.py:291`, `lionagi/cli/main.py:356`,
`lionagi/cli/main.py:496`).
`li --version` continues reporting the core version. Its current source is
`lionagi.version.__version__`; independent hub version reporting requires a
separate behavior decision (`lionagi/cli/main.py:272`,
`lionagi/_auto.py:424`).

**Failure tests:** `test_cli_move_preserves_behavior` and
`test_cli_version_source_is_unchanged`.

## Consequences

- CLI imports and module invocation gain one permanent public hub path; distribution
  ownership can change at 2.0 without another Python-path migration.
- Shared run state, process identity and parser declarations acquire public owners.
  The migration requires eight exact module rows and module-aware compatibility
  tests, rather than pretending that all 46 unchanged files move together.
- The alias table temporarily contains both specific CLI-module rows and a parent
  package row. Removal at 2.0 deletes all of them together; the release must publish
  the mapping and retain persisted-name conversion obligations from ADR-0136 D5.
- Core-only users must install the hub to regain `li`. A bare core upgrade does
  not leave a diagnostic executable. This trades discoverability for a simpler
  dependency and file-ownership contract.
- Removing core underneath hub leaves an invalid dependency installation. Keeping
  the script file is correct ownership behavior, not a promise of operation.
- Fresh-process catalog tests become necessary alongside import-boundary tests.
  A command working after an unrelated hub import is not sufficient.
- Reversing D1 after publication costs another public-path transition. Reversing
  D5 costs another console-script ownership migration. Changing shared-service
  placement must preserve the one implementation and state owner.

## Alternatives considered

### Keep the draft's lionhub.serve.cli destination

This is the strongest location alternative: it groups all public process adapters
beside daemon and MCP and follows the existing layout illustration.
It has the same alias-prefix compatibility cost as `lionhub.cli`.
It loses because the current server-oriented meaning of `serve` is narrower than
the CLI's local and client command surface, and the extra nesting does not enforce
a useful dependency boundary. An explicit product definition of `serve` covering
all process adapters, or a measured import-isolation benefit unavailable to a
sibling CLI, would reverse this choice.

### Move all 46 files unchanged with only one row

This minimizes compatibility-table entries and fits the current test enumerator.
It loses because it leaves the Studio/shared-command cycle and the core catalog
in place, contrary to A4 step 3. It becomes viable only if an independently completed
extraction has already removed those dependencies; it is not the source HEAD.

### Extract definitions but retain every old helper file

Thin adapters can be appropriate in mixed command modules, and this decision uses
them there. Retaining emptied helper modules would add redundant forwarding
surfaces and risk copied mutable state. Exact module aliases preserve both module
and object identity for the eight complete moves. A demonstrated requirement for
old on-disk module enumeration would require changing ADR-0136's stale-file rule,
not silently retaining those files.

### Core-owned dependency-free launcher

This keeps `li` discoverable after a core-only upgrade and after hub removal,
avoids transferring script ownership, and can print the hub install command.
It is permitted by the prior records. A working CLI behind that launcher needs a
core-neutral activation interface with absence and broken-target semantics;
a second hub-owned script is forbidden. The added bootstrap mechanism and
separation of script ownership from implementation are not justified here.
An explicit requirement for core-only executable discovery plus a tested,
dependency-free, D2-compliant activation contract would reverse the decision.

### Third distribution for the executable

This gives the CLI an independent dependency and release boundary, but a core-plus-hub
installation would still need a third install to obtain `li`. It adds another
ownership transition and release contract without evidence that the CLI deserves
a separate distribution. A measured benefit satisfying ADR-0122 D7's split
conditions could justify it later.

### Automatic discovery of third-party CLI declarations

This could allow independently installed command extensions. It requires new
conflict, selection and activation contracts and is not supplied by today's marker
decorator or plugin schema. The fixed hub-owned catalog solves current fresh-process
discovery without deciding that separate feature.

### Overlay a split installation onto the bundled environment

This can save environment setup, but a single visible `li` does not establish
exclusive ownership of the launcher or the bundled hub package files. It remains
unsupported without a tested installer sequence in the distribution release record.
The new-environment migration has a clear rollback target.

## Tests

The following are proposed acceptance tests, not results from executed tests.
Place packaging cases in
`tests/packaging/test_cli_distribution_ownership.py`, relocation behavior cases in
`tests/packaging/test_cli_relocation.py`, and extend existing moved-module and
catalog suites.

1. **Canonical targets and module execution (D1, D4):**
   `test_cli_canonical_target`,
   `test_cli_module_invocations_in_fresh_processes`.
   Invoke actual installed scripts and old/new `python -m` paths with help,
   version, unknown arguments, sentinel cases and deterministic machine commands.
   Check stdout, stderr, exit status, package-relative imports and environment paths.
   The existing alias test uses `run_name="not_main"`, so it is not this proof
   (`tests/test_moved_modules.py:315`).

2. **Shared closure, identity and rows (D2):**
   `test_step3_shared_dependency_closure`,
   `test_move_table_handles_module_and_package_rows`,
   `test_specific_cli_rows_survive_parent_move`,
   `test_shared_state_has_one_owner`,
   `test_preserved_logger_names`.
   Exercise old-first/new-first imports, one warning, real specs, reload, patched
   state, pickle/class paths where applicable, absent stale files, and the specific
   rows before and after the parent row. Reject new shared-to-adapter edges.
   Retain all existing D2, D4 and persisted-name checks.

3. **Fresh catalog composition (D3):**
   `test_fresh_cli_catalog_discovery`,
   `test_catalog_loading_stays_lazy`,
   `test_cli_projection_uses_one_catalog`,
   `test_core_contains_no_cli_targets`.
   Compare all 21 canonical seeds, aliases, order and help metadata; exercise
   scheduling, plugin-management and casts selection with no earlier import.
   Preserve duplicate-token/marker failures and selected-only loading.
   Existing catalog tests provide characterization fixtures
   (`tests/contracts/test_auto_registry.py:131`,
   `tests/contracts/test_auto_registry.py:334`).

4. **1.x upgrade and subprocess targets (D4):**
   `test_1x_upgrade_replaces_li_target_once`,
   `test_saved_1x_wrapper_uses_alias`,
   `test_background_cli_target_and_compatibility_recognition`.
   Save the actual old wrapper before upgrading, inspect new metadata and file
   ownership, invoke the environment's script directly without PATH fallback or
   a skip-on-missing branch, then separately run the saved wrapper.
   Assert background argv uses the canonical module and self-recognition accepts
   the old supported forms through 1.x.

5. **2.0 ownership and lifecycle (D5, D6):**
   `test_core_only_has_no_li`,
   `test_hub_owns_exactly_one_li`,
   `test_cli_distribution_transition_matrix`,
   `test_console_script_metadata_matches_file_owners`,
   `test_mixed_bundled_and_split_install_is_rejected`,
   `test_cli_environment_rollback`.
   Cover every D6 row using real wheels, both uninstall orders, dependency
   validation, platform launcher artifacts, and deliberate stale/duplicate
   ownership fixtures that the checks must reject.

6. **Behavior and feature parity (D5, D7):**
   `test_cli_move_preserves_behavior`,
   `test_optional_command_feature_errors_are_unchanged`,
   `test_cli_version_source_is_unchanged`.
   Characterize pre-parser order, sentinel safety, machine output, environment
   failures and per-command statuses without starting execution services.
   Reuse the adjacent record's proposed
   `test_fresh_wheel_schedule_validate_and_export_without_backend` with application
   imports refused during 1.x and the application physically absent at 2.0
   (`docs/adr/ADR-0138-studio-backend-attachment-daemon-ownership-and-extraction.md:139`).

## Notes

**UNVERIFIED:** future generated wrappers, installer deletion/order behavior,
platform launcher representation and the proposed wheel transition results have
not been executed. After implementing these tests and preparing the required wheels,
run:

```text
python -m pytest tests/packaging/test_cli_distribution_ownership.py tests/packaging/test_cli_relocation.py
python -m pytest tests/test_moved_modules.py tests/contracts/test_auto_registry.py
```

These commands are acceptance procedures; the two proposed packaging files do not
exist at the source HEAD. The source inventory and dependency counts are static
observations, not a claim that the proposed relocation already works.
