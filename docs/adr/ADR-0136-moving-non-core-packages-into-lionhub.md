# ADR-0136: Moving the packages built on the core into lionhub, and removing their old import paths in 2.0

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started. Changes implementing D1 to D5 for the four wave-1 packages
  (D6) are open and wait on this record; the removal release (D4) and waves 2 and 3 (D6) remain.
- **Area**: governance
- **Date**: 2026-10-25
- **Relations**: follows ADR-0122 D1 (layer order), D7 (one distribution first) and D8 (compatibility
  is explicit and finite); is the explicitly versioned compatibility decision that ADR-0125 D4
  requires before a public import path is removed; amends ADR-0001 (Amendment 2: `lionhub.` joins
  the class-path allow-list); updates ADR-0120's citations of the dispatch outbox to its new file

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

**P4. A second distribution now forms a cycle.** Old paths kept in `lionagi` would forward to
`lionhub`, and `lionhub` imports `lionagi`, so two distributions would each need the other. ADR-0122
D7 also measured a cycle between `cli` and `studio` (14 files one way, 3 the other) and ruled that a
split waits for a clean graph. Once providers move, a separate `lionhub` distribution would make
`iModel(provider="openai")` fail for every user who installed only `lionagi`.

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
| Deprecation plan | D4: every row is deprecated in 0.37.0 and removed in 2.0.0; a test fails the release that reaches the removal version; the CHANGELOG carries the table. |
| Persisted names | D5: serialized class paths load under either path until 2.0 and are saved under the new one; logger names do not change; each move searches persisted data first. |
| Order | D6: three waves by import graph: packages the core never imports, then the CLI, MCP and Studio once their cycle is broken, then packages the core imports, each after a core seam. |
| Layout | D7: all of `lionhub` lives under `lionhub/src/lionhub/`; the target layout and where each part comes from. |

Out of scope:

- **The Studio backend's form** (its own process beside the daemon, or a plugin the daemon loads):
  DEFERRED to its own record before wave 2 moves Studio (D7 records both forms).
- **Where the CLI lives**: DEFERRED to wave 2. In one distribution `pip install lionagi` provides
  `li` wherever the code sits (D7).
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
  module under both names, in the CI `core-install` job.
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
  `tests/test_moved_modules.py` and `tests/operations/test_graph_entrypoint_conformance.py`. A root
  missing from one of them is a package that tool does not see.
- ADR-0122 D7 keeps the `lionagi` top-level namespace intact. It stays intact: every existing
  `lionagi.*` path imports until 2.0 (D3). `lionhub` is a second regular package, not a namespace
  package, which that record does not approve.
- At 2.0 `lionhub` gets its own project file and distribution and depends on `lionagi`. By then no
  table row is left (D4), so `lionagi` imports nothing from `lionhub` and no cycle exists. From 2.0,
  `pip install lionagi` alone no longer installs `lionhub`; the 2.0 record decides the release
  mechanics against ADR-0122 D7's seven gates.

Why this way:

- **A separate distribution now** fails P4: old paths in `lionagi` would need `lionhub`, which needs
  `lionagi`, and once providers move every user who installed only `lionagi` breaks in the first
  release, however good the error text.
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
- The class-path allow-list in `lionagi/ln/_utils.py` holds the prefix string `"lionhub."`, which the
  pattern does not match. That is deliberate and recorded here rather than hidden: the allow-list is
  trust policy, and the core imports a `lionhub` module through it only when serialized data names
  one (ADR-0001 Amendment 2). The core imports no `lionhub` module on any path of its own.

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
- **First import of an old name**: the loader imports the new name and returns that module object,
  which the import system then also registers under the old name. One `DeprecationWarning`,
  attributed to the code that wrote the import, for an `import` statement and for
  `importlib.import_module`.
- **Later imports of the old name**: a `sys.modules` hit, no warning.
- **Spec**: the module keeps its real `__spec__` and `__name__` (the new name), so
  `importlib.reload` and the relative imports inside it resolve against the new name.
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
- **Scope**: the finder serves imports only. Logger names, entry points and other strings that name a
  module are D5's.
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
- **One table** makes removal the deletion of rows (D4), and the tests enumerate the table, so a move
  without a row, or a row without its tests, cannot pass.

### D4 — The deprecation plan

1. **Deprecated in 0.37.0**, the first release cut from the development line that contains the moves.
   If that release takes another number, the rows change with it before it ships.
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
   module object as its new path, from a non-editable install with base dependencies only.
5. **CHANGELOG**: the release that adds rows has a Changed entry that says what moved and why, and a
   table of old path, new path and removal release, one line per row.
6. **The removal**: one change at 2.0 deletes every row, `lionagi/_moved.py`, its `install()` call, the
   old-path resolution in `Element` (D5) and the tests that enumerate the table. The CHANGELOG's
   Removed section repeats the table. Item 4's version test is what forces the change.

### D5 — Persisted names

The class-path allow-list is `{"lionagi.", "lionhub."}` (ADR-0001 Amendment 2), and the metadata check
in `lionagi/protocols/generic/element.py` reads an old path as the class's path at its new module:

```python
def _current_path(lion_class: Any) -> Any:
    """The dotted path a ``lion_class`` names today: one written under a module that has
    since moved (``lionagi._moved.MOVED``) reads as the class's path at its new module."""
    if isinstance(lion_class, str):
        return _resolve_moved(lion_class) or lion_class
    return lion_class

# in the validator
if "lion_class" in val and _current_path(val["lion_class"]) != cls.class_name(full=True): ...
```

Exact semantics:

- **Data written before a move** names the old path. `Element.from_dict` loads it through the old
  module name (D3) whether or not the process has imported the class; `model_validate` on the class
  accepts it; saving the loaded object writes the new path.
- **Data written after a move** names the new path and loads in a process that has not imported
  `lionhub`, because `lionhub.` is allowed.
- **A prefix outside the allow-list** (`lionhubx.` in the test) is still refused, the error names
  `register_type_prefix`, and nothing from `lionhub` is imported. A malformed path under the prefix
  (an empty segment or a leading dot) is refused before anything is imported.
- **What the prefix reaches**: every `lionhub` module except the top-level package, which holds only
  a docstring and `__all__`, is a moved module that the `lionagi.` prefix already reached by its old
  name, so `lionhub.` makes no new import-time code reachable from data.
  `test_the_lionhub_prefix_reaches_only_modules_the_lionagi_prefix_already_reached` fails when
  `lionhub` gains a module with no old name or the top-level package gains code, and that change
  reopens the allow-list decision (ADR-0001 Amendment 2).
- **Tests**: for every `Element` subclass a moved module defines, a fresh interpreter, asserted to have
  imported nothing from `lionhub` before reading the data, loads data naming the new path and data
  naming the old one, and checks the loaded class and that saving it gives the new-path data back;
  `model_validate` is checked with old metadata. `test_every_moved_element_class_has_a_sample` fails a
  moved class that has no sample instance. Removing `lionhub.` from the allow-list, or restoring the
  exact-match metadata check, fails these tests for both wave-1 classes.
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
- **At 2.0**: the table goes (D4), so data naming an old path no longer loads. The migration is to load
  and save it under any release from 0.37.0 through 1.x, which writes the new path; the 2.0 release
  notes say so. A data-only class-path rename table kept past 2.0 was weighed and lost: the plan
  removes every compatibility path at 2.0 and such a table is one. If data naming old paths is still
  in use near 2.0, that release's record can reopen it with the measurement.

### D6 — Order: three waves by import graph

| Wave | Packages | Imported today by | Prerequisite |
|------|----------|-------------------|--------------|
| 1 | `orchestration`, `engines`, `dispatch`, `work` (7,801 lines) | `cli`, `studio` and each other | none: the core never imports them |
| 1, held | `integrations` (548) | nothing | open outside contributions that edit it land or close first, so their authors do not rebase across a move |
| 2 | `cli`, `mcp`, `studio` | each other; `state` imports `cli` in 2 files | the `cli`/`studio` cycle broken (Studio imports private `lionagi.cli` modules 32 times) and the `state`-to-`cli` inversion removed; the Studio backend goes to `apps/studio/backend`, outside the wheel; the daemon and its scheduler go into `lionhub` |
| 3 | `providers`, `tools`, `agent`, `casts`, `hooks`, `plugins`, `lndl` | the core: `providers` by `service` (3 files) and `operations` (2); `tools`, `agent`, `casts` by `session` (1 each); `hooks` by `operations` (4) and `session` (2); `plugins` by `service` (1) and `protocols` (1); `lndl` by `operations` (2) | a seam in the core for each, first; providers are found by registry key rather than by the module paths `lionagi/service/connections/registry.py` and `lionagi/service/token_budget.py` list today |

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
wave-1 packages is therefore settled before 0.37.0 ships.

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

**DEFERRED: the CLI's home.** The draft puts it in `serve/cli`. In one distribution
`pip install lionagi` provides `li` wherever the code sits: the `li` entry point stays declared by the
`lionagi` distribution, and only its target changes when the CLI moves. The cost of a CLI in
`lionhub` appears at the 2.0 split, where `pip install lionagi` alone would no longer provide `li`.
D2 already fixes that the CLI leaves the core package, because it imports `lionhub`; wave 2 decides
where it goes and which distribution declares `li` from 2.0.

**DEFERRED: the Studio backend's form.** The Studio application (operator, routes, app shell, frontend
connection) goes to `apps/studio/backend`, outside the wheel, and not into `lionhub`. Two forms remain:

- A plugin the daemon loads: one port and one lifecycle, and simpler pairing for a browser page. The
  daemon already mounts its HTTP modules from one list (`lionagi/_auto.py`), the existing seam for
  this form. Against it: backend code runs inside the daemon's process.
- Its own process beside the daemon: a clean boundary. Against it: two ports and two lifecycles.

Either way the Studio container image builds from public sources. Decided in its own record before
wave 2 moves Studio.

## Consequences

- The core's package loses 7,801 lines in wave 1 and, through waves 2 and 3, the CLI, MCP, Studio and
  integration code; ADR-0125's core changes are read against a smaller package.
- A layer inversion is a test failure (D2), before ADR-0122's full checker exists.
- Code outside this repository gets one warning per old module, naming the new path and the removal
  release, and nothing breaks before 2.0.
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

- **A separate `lionhub` distribution now, depending on `lionagi`.** It would have given `lionhub`
  its own release cadence from the start. It lost on P4: old paths in `lionagi` would need `lionhub`,
  forming a cycle, and once providers move, users who installed only `lionagi` break in the first
  release. ADR-0122 D7's gates for a split are not met.
- **`lionagi` depending on a separate `lionhub`.** It would keep `pip install lionagi` complete. It
  lost because `lionhub` imports `lionagi`: the cycle is the same in the other direction.
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
- Implementation, per decision: D1 to D5 are implemented for the four wave-1 packages in open changes
  that wait on this record; nothing has merged. D4's removal and waves 2 and 3 remain.
