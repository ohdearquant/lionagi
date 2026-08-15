# ADR-0118: Declared entity schema as the single authority for state and studio persistence

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: persistence-state
- **Date**: 2026-08-15
- **Relations**: extends ADR-0117 (normalized progression membership); revisits ADR-0056
  (StateDB SQLAlchemy Core backend) and ADR-0077 (studio/state filesystem boundary); touches
  every ADR that added a StateDB column or table

## Context

The persistence layer describes its own schema five times in the package and a sixth time in
its own test fixture, and the guard that checks agreement between them compares names only.

**P1 — five parallel schema descriptions, plus a sixth in the test.** One logical schema is
written down in these hand-maintained forms:

1. `lionagi/state/schema_meta.py` (1,224 lines): a SQLAlchemy `MetaData` declaring 32 tables,
   380 columns and 82 `Index(...)` objects. Its own docstring calls it the "single source of
   truth for schema DDL". It is what production actually creates from: `metadata.create_all`
   at `state/db.py:966`.
2. `lionagi/state/schema.sql` (1,021 lines): 32 `CREATE TABLE` and 86 `CREATE INDEX`
   statements declaring the same schema in DDL text. Its header line reads
   `-- lionagi state schema v1` while line 22 seeds `version` `'3'`. It is bound to
   `_SCHEMA_PATH` (`state/db.py:122`) and read only by tests; production open never executes
   it.
3. 16 hand-written `CREATE TABLE` statements across 6 files in 3 packages that bypass both, of
   three distinct kinds: 3 rebuild targets in `state/db.py` (`sessions_new`, `invocations_new`,
   `schedule_runs_new`); 6 tables owned by `studio/operator/store.py` alone
   (`studio_operator_conversations`, `_views`, `_turns`, `_frames`, `_proposals`, `_effects`);
   and 7 re-declarations of tables the state layer already owns, in
   `studio/services/attention.py` (3), `approvals.py` (2), `run_tags.py` (1) and
   `projects.py` (1). Counting the operator's six, the shared database file holds 38 tables
   while the schema authority describes 32.
4. `MIGRATION_COLUMNS` / `MIGRATION_INDEXES` in `state/schema_migrations.py`: a per-table
   ledger of additive columns (the `sessions` list alone carries ~30 entries) and a per-dialect
   index list, both re-stating what 1 and 2 already declare.
5. Five `_*_COLUMNS` frozensets in `state/db.py` (`_SESSION_COLUMNS` 217, `_INVOCATION_COLUMNS`
   287, `_SHOW_COLUMNS` 301, `_PLAY_COLUMNS` 315, `_BRANCH_COLUMNS` 338) guarding the dynamic
   UPDATE builders, plus the hand-written column lists inside each `_build_*_insert_stmt`.
6. `ALL_TABLES` in `tests/state/test_engine_schema.py:192`: a hand-typed set of all 32 table
   names, which is the population every parity assertion iterates over. A table added to the
   package and not to this set is not checked by any of them.

Adding one column today can require four or five separately-edited, separately-reviewed
locations. Recent regressions in this area (a backfill writing a guessed `ended_at` that
downstream read as measured; an audit default misfiling automatic disables as operator
requests) are the failure mode this multiplicity produces: each copy encodes a slightly
different belief about the schema.

**P2 — the parity guard compares names, so the drift that exists today passes it.**
`tests/state/test_engine_schema.py` builds one database from `MetaData` and another from
`schema.sql` and compares: the table-name set (`test_metadata_creates_all_tables`), the
per-table column-*name* sets (`test_metadata_column_parity_vs_schema_sql:246`), the columns of
exactly one index (`test_branches_index_matches_runtime_migration_definition:287`), and CHECK
enum value-sets (`test_metadata_check_constraint_parity_vs_schema_sql:344`). Column types,
nullability, defaults, primary and foreign keys, the other 85 indexes, and index direction are
outside what it pins. Two divergences are live in the tree right now and green:

- `schema.sql` declares 86 indexes; `schema_meta.py` declares 82.
- `schema.sql` declares 15 indexes with `DESC` key direction (`:93,198,204,283,311,378,…`);
  `schema_meta.py` declares zero. Production databases, built by `create_all`, therefore have
  none of those descending indexes. (SQLite and PostgreSQL can both scan an ascending index
  backwards, so the query-plan consequence is unmeasured; the definitional drift is proven.)

A guard that passes while its two subjects disagree is worse than no guard, because it is
cited as the reason the duplication is safe.

**P3 — DDL issuance and connection ownership are not confined to the state layer.**
`studio/operator/store.py` (2,349 lines) declares and creates 6 tables of its own and is in
effect a second persistence layer beside `StateDB`. Four `studio/services/*` modules create 7
more tables at service-import time. Separately, studio opens the store directly — 73
`async with _open_db(...)` contexts across 10 files — rather than going through `StateDB`. The
path those contexts open is chosen by `studio/services/_db.py:27`, whose own docstring records
that for a server-backed store it falls back to the default SQLite file and is "equally wrong
for that deployment". `require_file_store` exists as the guard against exactly that case and is
referenced 37 times, but `services/shows.py`, `services/signals.py` and `services/stats.py`
never call it.

**P4 — SQL is hand-built, and identifier safety is convention rather than construction.**
There are 361 lexical `text(` sites across `state/` and `studio/` and zero in `protocols/` or
`service/`. There are 88 `# noqa: S608` suppressions across 12 files (45 in `state/db.py`, 17
in `studio/services/db_maintenance.py`, 7 in `studio/services/sessions.py`, the rest in
single digits), of which 22 are direct interpolations of an identifier into a statement being
constructed (14 `text(f"…")`, 8 `execute(f"…")`). No injection of a caller-supplied *value*
was found: every site is guarded by a nearby allow-list, fixed tuple, or enum check. The
weakness is that each guard is re-established by hand at each site, and at least one path has
none: `LifecyclePolicy.table` and `patch_fields` are plain `str`/`frozenset[str]`
(`state/lifecycle/models.py:77-89`), the registry does not validate them as identifiers, and
they are interpolated into SQL at `state/lifecycle/service.py:115` and elsewhere.

**P5 — migration is an additive ledger that fails open, and the version is not a shape
identity.** Schema evolution is `MIGRATION_COLUMNS` applied at startup plus bespoke `*_new`
copy-and-rename rebuilds for anything SQLite's `ALTER TABLE` cannot express (`sessions_new`,
`invocations_new`, `schedule_runs_new`, `schedules_new`). There is no introspection of the live
schema, no diff against the declared schema, and no risk classification. `_reconcile_columns`
wraps its inspection in `except Exception: continue` (`state/db.py:1023`), and the open sequence
then stamps `SCHEMA_VERSION` unconditionally — so a database whose columns could not be
inspected still reports the current version. `version = 3` means "this build opened it", not
"this shape is known". Notably, the schedules rebuild already *derives its target table from
`schema_meta.py`* via `to_metadata` (`state/db.py:1490`) — the generated path half-exists and
works on SQLite.

**P6 — the layer is decoupled from the rest of the codebase and oversized for what it does.**
`state/` (13,677 lines, 25 files) plus `studio/` (37,197 lines, 79 files) together exceed the
entire core layer — `providers/` + `service/` + `protocols/` + `ln/` ≈ 29,600 lines — while
re-implementing primitives those packages already provide. `StateDB` returns raw row dicts
rather than typed objects, so every consumer re-parses the same fields; that broken return
contract, not the presence of duplicate helper functions, is where the primitives are being
bypassed. A reference implementation of the full destination design provides DDL generation,
typed CRUD, schema introspection, schema diffing, risk-classified migration planning,
identifier validation and typed error mapping in roughly 4,000 lines.

**P7 — misplaced modules.** Provider transcript mirrors live in the state package
(`state/claude_mirror.py`, `state/codex_mirror.py`, `state/_mirror_common.py`) although they
are provider-format concerns. `state/reasons.py` mixes four unrelated vocabularies in 248
lines: reason-code namespaces, the canonical entity-type vocabulary, a frontend route alias,
plural table-name aliases, a code-format validator, and the entity-type → physical-table map
that `update_status()` uses.

### The target shape

The destination is the design where **the class definition is the table**. A working
implementation of it was studied before writing this ADR; the design below adopts its structure
and departs from it where the departures are called out. The shape is:

- A persisted entity is a class carrying a small configuration object: table name, content type,
  and per-entity toggles for audit columns such as soft delete, versioning, content hashing and
  updated-at tracking. Persistable classes self-register in one registry.
- Foreign keys are declared at the field's type annotation rather than in a separate table
  definition, are discovered by metadata extraction, and drive topological ordering of table
  creation.
- Schema generation composes field specifications — content fields, audit fields, flattened
  nested models — and emits the table definition. Row serialization and the emitted schema agree
  by construction, because both derive from the same specification.
- The migration engine introspects the live schema, diffs it against the declared schema, and
  produces a plan whose operations each carry a risk classification.
- Identifiers assembled at runtime pass a validator; driver errors map to typed errors.

### What that implementation gets wrong

Adopt the pattern, and do not copy the code: the implementation studied has defects that a bulk
port would import wholesale, several of them observable by running it.

- **Generated output is not deterministic, measurably.** Field selection filters by iterating the
  caller's collection rather than filtering the stored ordered tuple, and the composition path
  converts its ordered field list to a set at the call site. Column order therefore follows string
  set iteration order, which varies per process. Composing one entity under four different
  `PYTHONHASHSEED` values produced four different column orders and four different
  `CREATE TABLE` statements. That is survivable for a one-off table creation and fatal for a
  generated migration or a schema hash, which are exactly what this design depends on.
- **Type information is lost between the field spec and the emitter.** The type mapper discards
  the resolver's "is a list" flag, so `list[str]` unwraps to `str` and emits `TEXT`, and the
  vector dimension is dropped: an entity declared with an eight-dimensional embedding emits an
  untyped JSON column. Both were reproduced end to end. Two versions of that implementation
  disagree with each other here, one converting lists to JSON early and one not, so there is not
  even a single reference behavior to copy.
- **Literal defaults are interpolated without escaping**, so a default containing an apostrophe
  produces invalid SQL, and the two emission paths disagree about whether a default suppresses
  `NOT NULL`.
- **The schema hash omits most of what it must detect, and the two sides of the comparison hash
  different things.** The projection drops primary key and unique flags, foreign key actions, and
  index method and predicate, so materially different schemas hash equal. Worse, the hash computed
  from the *declared* schema includes triggers, check constraints and unique constraints while the
  hash computed from the *live* schema includes only columns, foreign keys and indexes. Comparing
  them compares different field sets, which is not a weak detector but a meaningless one. This is
  the same defect this repository already has in its name-only parity test (P2), arrived at
  independently.
- **Two derivations claim to be the single source.** Schema generation flattens the configured
  content model while the entity-to-table factory iterates the outer class's fields, so the
  implementation that exists to end multiple sources of physical truth has two of its own.
- **Whole-schema emission ignores dependency order** and constraint names are assembled without
  re-validating the assembled length.

The parts that survive contact with all of this are the ideas: declaration at the class, foreign
keys at the annotation, a registry, frozen comparable schema objects, risk-classified diffs, and
Kahn ordering. Those are what this ADR adopts.

### The decisive constraint

The reference implementation targets PostgreSQL via asyncpg (JSONB, pgvector, RLS, roles,
`DEFERRABLE`), emits Postgres DDL text, and its hash omits enough physical semantics
(composite keys, checks, index method and predicate) that it cannot serve as a drift detector
unchanged. This codebase is SQLAlchemy-based and SQLite-first with PostgreSQL support
(`StateDB.dialect` branches throughout). SQLite's `ALTER TABLE` cannot drop constraints, change
column types, or add foreign keys, which is exactly why the `*_new` rebuild pattern exists. Any
port that assumes PostgreSQL is dead on arrival.

Worse, it fails quietly. SQLite accepts `UUID`, `JSONB`, `DOUBLE PRECISION` and
`TIMESTAMP WITH TIME ZONE` as column type names and gives them none of the corresponding
semantics: arbitrary text stores into all of them, and a `UUID PRIMARY KEY` accepts two NULL
rows. Running the reference's generated DDL against SQLite therefore succeeds while producing a
table that means something else. Acceptance is not portability, so the equality gates below
compare compiled schema semantics rather than checking that a statement executed. **Port the pattern, not the file**: the design
below adopts frozen specs, identifier types, generated schema, registry ownership and
deterministic hashing, and rejects the DDL-string emitter, the incomplete hash, and the
raw-default handling.

## Decision

**D1 — one schema authority: a declared entity spec.** Each persisted entity is described once,
by a frozen class-level declaration (fields with types, nullability and defaults; foreign keys
at the type annotation; primary keys with composite ordering; indexes with explicit key
direction; per-entity toggles for audit columns), registered in one central registry.
Everything else — table objects, insert/update builders, update allow-lists, the DDL snapshot,
migration plans — derives from the registry. No other module may declare a table.

**D2 — emission target is SQLAlchemy `MetaData`, not DDL strings.** The specs generate the
`Table` objects that `schema_meta.py` hand-writes today, into one `MetaData`. This preserves
`metadata.create_all`, dialect handling, and the proven `to_metadata`-driven rebuild machinery,
and it is the point where this design deliberately diverges from the reference implementation's
Postgres-DDL-text emitter. It also inherits correct creation ordering for free:
`create_all` sorts tables topologically by foreign key. That matters more here than it does in
the reference, because SQLite cannot add a foreign key with `ALTER TABLE` at all — FKs must be
inline in `CREATE TABLE`, so dependency-ordered creation is mandatory rather than a nicety. The
implementation studied applies a topological sort on its initial-create path but emits per-table
operations in plain alphabetical order on its diff path, which is one of the places this design
must not follow. `schema.sql` becomes a *generated* artifact emitted from the same
specs, retained for the compatibility tests that build old-style databases; it stops being an
authored authority. `ALL_TABLES` in the parity test is replaced by the registry's own table
list, so the test population can no longer drift from the package.

**D3 — parity is proven on physical semantics, not names.** The Phase 1 gate compares generated
against current for column types, nullability, defaults, primary and foreign keys, unique and
check constraints, and the full index set including key direction — not the name sets the
current test compares. The 15 descending indexes and the 86-vs-82 index gap are resolved by an
explicit decision per index, recorded, before either authority is deleted.

**D4 — generated statement builders replace hand-built SQL for row CRUD.** Insert column lists,
update SET clauses, and update allow-lists derive from the spec's field set. The 22 identifier
interpolation sites and the five `_*_COLUMNS` frozensets are retired; "only declared columns
reach SQL" holds by construction rather than by 88 hand-placed suppressions. `LifecyclePolicy`
carries validated identifier types instead of `str`. Hand-written SQL remains legitimate and
expected for genuinely bespoke work — locking, CTEs, JSON operators, window aggregates, atomic
CAS transitions, retention sweeps — which moves behind named typed owners with an explicit
escape hatch that validates identifiers, rather than being scattered through route code.

**D5 — migration is a diff between declared and introspected schema, and it fails closed.** At
open, introspect the live database, diff against the declared schema, and produce a
risk-classified plan: additive columns become `ALTER TABLE ADD COLUMN`; everything else becomes
a generated rebuild — the pattern `state/db.py` already implements by hand for four tables.
The generated rebuild derives the target table, its indexes and its triggers from the specs. The
current rebuilds do the opposite: they read the live catalog and replay it, assembling the copy
statement's column list from catalog-read names and re-executing index DDL strings taken from
`sqlite_master` (`state/db.py:1418-1422`, and the same shape in the six other rebuild paths).
That faithfully preserves whatever the database happens to contain, including objects nothing
declares, so a rebuild carries drift forward instead of resolving it — a database built from the
old DDL path keeps its 15 descending indexes through every future migration. It is also the one
place where identifiers reach SQL from a source no allow-list covers.

Risk is classified per dialect, not once: the same logical change has different mechanics on
each backend, so a type widening that is a cheap `ALTER` on PostgreSQL is a full table rebuild
on SQLite, and a plan that reports one risk for both is lying to whoever approves it. The
reference's two-phase execution model (transactional operations, then `CONCURRENTLY` /
`VALIDATE` outside the transaction) collapses on SQLite, which serializes all DDL and supports
neither; the port keeps the phase field for the PostgreSQL leg and treats rebuild-versus-alter
as the axis that actually carries risk here.
`MIGRATION_COLUMNS` is retired. An inspection failure blocks writable open instead of being
swallowed; the version/hash row advances only after post-apply introspection confirms the
resulting shape. The engine lands in observe-only mode first (classify and report, apply
nothing) so that unknown deployed shapes surface before any of them is migrated.

**D6 — DDL issuance and store access become the state layer's exclusive right.** The six
operator-store tables and the 7 studio-service re-declarations register in the registry (the
latter as the state-owned tables they already duplicate). Studio loses its
import-time `CREATE TABLE` side effects and its own connection path; the 73 direct contexts move
to one executor over the state engine, which removes the "equally wrong" fallback and the three
services that never call `require_file_store` along with it.

**D7 — provider mirrors move to the provider packages.** `state/claude_mirror.py` →
`providers/anthropic/claude_code_mirror.py`; `state/codex_mirror.py` →
`providers/openai/codex_mirror.py`; `state/_mirror_common.py` → a shared module under
`providers/`. `cli/mirror.py` stays as the CLI adapter. Pure moves with import updates across
roughly 17 files; no compatibility shims (own-use policy).

**D8 — `reasons.py` is keyed two different ways at once; separate them.** The module holds two
vocabularies that look like one. The seven code classes are keyed on *producer domain*, and each
owns exactly one code prefix: `RunReasons` → `run.` (29 codes), `SessionReasons` → `session.`
(6), `PlayReasons` → `play.` (8), `ShowReasons` → `show.` (5), `ScheduleReasons` → `schedule.`
(7), `TeamReasons` → `team.` (1), `DispatchReasons` → `dispatch.` (8). Everything around them —
`VALID_ENTITY_TYPES`, `ENTITY_ROUTE_ALIASES`, `ENTITY_TABLE_ALIASES`, `ENTITY_TYPE_TO_TABLE`,
and the per-entity `reason_prefixes` in the lifecycle policy — is keyed on *entity type*. The
two keyings do not line up, and every mismatch is load-bearing somewhere:

- `run` is the largest domain and is not an entity at all. `reasons.py:23-25` declares it a
  frontend route alias for `session`, because `/runs/<id>` renders a session.
- `invocation` is a canonical entity type with no domain of its own; invocation rows carry
  `run.` codes.
- `dispatch` has a domain class and a lifecycle policy but is absent from `VALID_ENTITY_TYPES`.
- `schedule_run` rows legitimately carry codes from two domains at once, which
  `ScheduleReasons`' own docstring documents: the `schedule.skipped.` prefix "is NOT the full
  set of reasons a skipped `schedule_run` can carry", since `RunReasons` codes land there too.

That is the oddity worth naming: the file reads as an entity vocabulary and is not one, so a
reader looking for "the reason codes for entity X" finds a class named for something else. The
decisions follow from it.

*Keep the domain classes together and keep their names aligned to their prefixes.* One class per
prefix is the single invariant this module currently holds, and it is worth keeping. That rules
out renaming `RunReasons` to something like `ExecutionOutcomes`, which would leave a class named
for execution owning strings spelled `run.`, and it rules out relocating the mirror-liveness
codes into `SessionReasons`, since they too are spelled `run.` and the strings are persisted in
`status_reason_code` and cannot change.

*Fix the word that is actually wrong.* These are outcome codes, not reasons — `RunReasons`' own
docstring already calls them "Outcomes of session execution". Rename the suffix across all
seven: `RunOutcomes`, `SessionOutcomes`, `PlayOutcomes`, `ShowOutcomes`, `ScheduleOutcomes`,
`TeamOutcomes`, `DispatchOutcomes`. Every string constant stays byte-identical, so no persisted
value moves; the change is mechanical across 497 occurrences in 58 files (176 in the package,
321 in tests), and none of these names is exported from any package `__init__`, so the public
surface is untouched.

*Move the entity vocabulary to the registry.* `VALID_ENTITY_TYPES` and `ENTITY_TYPE_TO_TABLE`
are the fourth and fifth hand-maintained restatements of "which entities exist and what table
each lives in"; under D1 both are derived from the registry rather than typed. The aliases stay
as an explicit compatibility map. Whether `dispatch` is an entity type gets decided at that
point rather than inherited.

**D9 — what carries over, what is reworked, what is left behind.** The target design splits
cleanly along a dialect seam, and that seam is what makes adopting it tractable:

- *Port as-is* — the frozen spec dataclasses (column, foreign key, index, trigger, check,
  unique, table, schema), the entity→spec and registry→schema constructors, the migration
  operation/plan data model, the operation-type and risk enums, the spec-comparison logic
  including type-change classification, and identifier validation plus order-by sanitization.
  These model schema state and compare it. Their data carries no dialect syntax; the `to_ddl`
  methods hanging off the same classes do, and those belong to the rework bucket below.
- *Port with rework* — everything that emits SQL text: the Python→SQL type mapping (`UUID`,
  `JSONB` and `TIMESTAMP WITH TIME ZONE` have no SQLite equivalents), the spec adapter, and the
  DDL strings attached to each diff operation. In this design that rework is largely a deletion:
  emission goes to SQLAlchemy constructs (D2) rather than to a second hand-written emitter.
- *Does not port* — row-level security, database roles, pgvector columns and tenant scoping have
  no place in a single-tenant SQLite-first store; `CONCURRENTLY`, `NOT VALID`/`VALIDATE`,
  non-btree index methods and function-body triggers have no SQLite counterpart and are
  PostgreSQL-leg-only where they are kept at all.

The reference hash is reimplemented, not ported: it must cover every physical semantic the diff
can detect, or it will report agreement across a real divergence — the failure mode P2 already
demonstrates in this repository.

## Phasing

Each phase lands behavior-preserving, gated by equality proofs, and independently valuable.

- **Phase 0 (immediately, independent of the engine):** D7 mirror moves; D8 reasons split and
  rename. Small mechanical PRs.
- **Phase 1:** entity specs + registry + generated `MetaData`, in shadow mode. The D3 gate pins
  the generated schema against today's, on physical semantics, for both dialects, before any
  hand-written body is deleted. The `DESC`/index-count divergences are decided here. The gate
  also pins determinism: generating twice in separate processes must produce byte-identical
  output, since the whole design rests on a schema hash and generated migrations.
- **Phase 2:** generated statement builders for insert/update; retire the interpolation sites
  and `_*_COLUMNS`; validated identifier types in `LifecyclePolicy`. Equality proofs pin
  generated SQL against current SQL on fixtures.
- **Phase 3:** introspect-and-diff engine in observe-only mode; then fail-closed application;
  retire `MIGRATION_COLUMNS`; the four bespoke rebuilds become instances of the generated
  rebuild, ported in risk order (generated schedules first, literal sessions/invocations next,
  schedule-runs last because it carries backups, indexes and triggers).
- **Phase 4:** fold operator-store and studio-service tables into the registry and route the 73
  direct connections through the shared executor (largest blast radius; last). Operator's atomic
  CAS SQL is preserved verbatim and moved, never rewritten in the same change as its schema.

**D10 — lifecycle reuses the general primitives where the semantics actually match, and not
otherwise.** "This layer re-implements primitives" is true of the package as a whole and false
in the specific places it is most tempting to change, so this decision names both sides.

Already correct, leave alone: the terminal-callback path uses `ln.concurrency` for task groups,
cancellation and shared deadlines. That is the canonical primitive and it is not duplicated.

Worth converging:

- Wire-model serialization. `RunTerminalEnvelope`, `Correlation` and `EntityRef` hand-write
  `to_dict` where `Element` already provides typed identity and `to_dict(mode=...)`. Small, and
  it removes a hand-maintained serializer from a message contract.
- Retry. There is no retry in the delivery path today; when durable delivery lands (see the
  unreconciled ledger, P-adjacent), it uses `ln.concurrency.retry` through
  `service/resilience.py` rather than a fourth backoff loop.
- Registration and dispatch. `service/broadcaster.py` and `TerminalCallbackRegistry` are the same
  shape at the registry level — subscribe, unsubscribe, count, fan out.

Explicitly rejected, because the vocabulary matches and the semantics do not:

- Persisted lifecycle rows do not inherit `Element`. Removing dataclass boilerplate is not a
  reason to give database rows an identity model built for in-memory objects.
- `protocols.generic.Event` is not the persistent status-transition model. It shares the words
  (status, completion) and means something else: in-memory execution state, not durable history.
- The callback registry is not replaced by `Broadcaster` wholesale. Lifecycle dispatch needs
  per-registration filtering by entity kind and id, override precedence, a shared handler
  deadline, and post-commit ordering; the broadcaster has none of those, and adding them to a
  generic singleton pub/sub to avoid a duplicate registry would push lifecycle semantics into a
  shared primitive. Share the registration/dispatch mechanics only if that can be done without
  moving those four properties.

The genuine consolidation for this layer is the one this ADR is about: the registry owns storage
shape, one lifecycle service owns transition semantics, and the general primitives supply
concurrency, serialization and retry underneath both.

## Scope boundary: typed rows

`StateDB`'s read surface returns untyped rows — 51 methods annotated
`dict[str, Any] | None` or `list[dict[str, Any]]` — so every consumer re-parses the same fields
and no reader is checked against the schema. It is the same root cause as P1: nothing connects
the declared schema to the code that uses it.

A registry makes the fix nearly free to *offer*, since the spec already knows each field's name
and type, so a generated row decoder falls out of it. Converting the read surface is a different
matter: it touches every caller in `cli/` and `studio/`, and it is the kind of change that, bundled
in here, would make the equality gates unprovable. This ADR therefore makes typed decoding
available and uses it on the generated CRUD paths (D4). Migrating the existing read surface is
explicitly out of scope and needs its own decision, taken after Phase 2 has shown what the
generated decoders actually look like.

## What this deletes

Measured file by file at the current head. "Deleted" means the hand-authored source stops
existing, because the same facts are generated from the registry.

| Deleted outright | Lines |
|---|---:|
| `state/schema.sql` (becomes a generated artifact) | 1,021 |
| `state/schema_migrations.py` (`MIGRATION_COLUMNS` + `MIGRATION_INDEXES`) | 284 |
| `state/db.py` rebuild machinery, seven inspect/copy/drop/rename/replay paths | 956 |
| `state/db.py` `_*_COLUMNS` frozensets | 135 |
| `state/db.py` hand-written insert builders (the two largest; more exist) | 139 |
| `studio/operator/store.py` schema + migration region | 244 |
| `studio/services/_db.py` (second connection path) | 114 |
| `studio/services/*` fallback DDL blocks, 4 files | 93 |
| **Subtotal, measured** | **2,986** |

`state/schema_meta.py` (1,224 lines) is not in that column because it is *replaced* rather than
removed: its 32 tables become entity specs. A spec form of the same 380 columns should land near
900 lines, so call it another 300 net.

Against that, the generator is new code: specs, registry, MetaData compiler, introspection, diff,
plan, rebuild generator, identifier validation, error mapping. A comparable implementation does
more than this in roughly 4,000 lines, including CRUD and PostgreSQL-only concerns this design
does not adopt, so 1,500 to 2,500 is the honest band.

Net on the schema layer: somewhere between 800 and 1,800 lines smaller, and that is the least
interesting part of the answer. The change that matters is that one column stops being four or
five edits, and "what is the schema" stops having six answers.

The larger cut is one I have not measured and will not estimate: the six studio service modules
carrying persistence total 2,387 lines, of which `projects.py`, `shows.py` and `signals.py`
(1,209 lines) reimplement `StateDB` APIs that already exist (7, 5 and 2 methods respectively),
while `run_tags`, `approvals` and `attention` own real domain logic and only their DDL and
connection handling go. Add the 73 direct store connections collapsing to one executor, five
duplicated session-by-id lookups, eight repeated frame inserts, six repeated admin-event inserts,
and two separate implementations of the same lock-read-append ledger. That work is Phase 4, and
sizing it needs the caller-by-caller pass Phase 4 starts with.

## Consequences

- One column addition becomes one edit in one place, and "what is the schema" has one answer.
- The identifier-interpolation class disappears rather than being re-guarded site by site.
- Migration gains introspection, risk classification and a fail-closed contract, which the
  current hand-ledger cannot express; SQLite's `ALTER TABLE` limits are honored by making the
  rebuild a generated operation instead of four bespoke ones.
- Studio stops being able to open the wrong database file.
- Cost: a multi-phase epic touching the widest-blast-radius files in the repository. The
  equality-proof gates are what make it safe; skipping them to move faster re-creates the
  problem this ADR exists to end.
- Risk concentrations: the data-preserving rebuild generator (Phase 3) and the operator-store
  fold (Phase 4). Both are sequenced last deliberately.
- What this ADR does *not* claim: no current SQL injection was found, and no query-plan or
  performance consequence of the index divergence has been measured. The case for the change is
  drift and ownership, not a live exploit.

## Appendix A — inventory

Counts are source-site counts, not runtime frequencies, and the units are deliberately not
summed: a declaration, a statement string, and an execution call are different things.

| Where | Declares schema | Issues DDL | Builds SQL | Notes |
|---|---|---|---|---|
| `state/schema_meta.py` | 32 tables, 380 columns, 82 indexes | via `create_all` | — | what production builds from; also 34 PK columns, 27 FKs, 20 CHECKs, 8 uniques, 29 partial indexes, 37 server defaults — the surface the Phase 1 gate must compare |
| `state/schema.sql` | 32 tables, 86 indexes, 5 pragmas, 3 seeds | tests only | — | `_SCHEMA_PATH` at `db.py:122`; not executed by writable open |
| `state/schema_migrations.py` | 127 additive column declarations over 14 tables | 10 indexes per dialect | — | the two dialect tuples are textually identical |
| `state/db.py` | 3 inline `CREATE TABLE` (rebuild targets) | yes | 256 execution calls | 6,683 lines; 5 `_*_COLUMNS` allow-lists; 45 `S608` |
| `state/lifecycle/` | — | — | 8 execution calls | policy table/patch identifiers unvalidated |
| `studio/operator/store.py` | 6 tables (+2 indexes) + own migration | yes | 119 sites | 2,349 lines; a second persistence layer |
| `studio/services/*` | 7 re-declarations of state tables, 4 modules | at import time | 213 query sites | 73 direct store connections in 10 files |

Divergences found between the two full-schema authorities, all currently green:

| Divergence | `schema.sql` | `schema_meta.py` |
|---|---|---|
| Index count | 86 | 82 |
| Descending index direction | 15 indexes | 0 |
| `idx_sessions_run_id` | present | absent (also in the migration registry) |
| Header version | `v1` in the comment | seeds `version` `3` |

Cross-cutting: 361 lexical `text(` sites in `state/` + `studio/` against zero in `protocols/` and
`service/`; 88 `S608` suppressions across 12 files; 22 identifier interpolations; 34
`BEGIN IMMEDIATE` sites; 51 `StateDB` methods returning untyped row dicts.

Duplicate query ownership worth its own issues: session-by-id is reimplemented five times across
the operator modules against `state/db.py`'s own; projects, shows and signals each duplicate a
`StateDB` API in `studio/services/`; the operator store repeats one frame insert eight times and
one admin-event insert six times; approvals and attention each implement the same
lock-read-append ledger separately.
