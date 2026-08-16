# ADR-0118: Declared entity schema as the single authority for state and studio persistence

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: persistence-state
- **Date**: 2026-08-15
- **Relations**: extends ADR-0117 (normalized progression membership); amends ADR-0056
  (StateDB SQLAlchemy Core backend) and ADR-0077 (studio/state filesystem boundary); depends on
  ADR-0119 (deterministic declaration substrate); delegates dispatch mechanics to ADR-0120 and
  provider placement to ADR-0122; requires ADR-0123's Run/entity decision before target-registry freeze;
  touches every ADR that added a StateDB column or table

## Context

The persistence layer describes its own schema five times in the package and a sixth time in
its own test fixture, and the guard that checks agreement between them compares names only.

**P1 — five parallel schema descriptions, plus a sixth in the test.** One logical schema is
written down in these hand-maintained forms:

1. `lionagi/state/schema_meta.py` (1,224 lines): a SQLAlchemy `MetaData` declaring 32 tables,
   380 columns and 83 `Index(...)` objects. Its own docstring calls it the "single source of
   truth for schema DDL". It is what production actually creates from:
   `await conn.run_sync(metadata.create_all)` at `state/db.py:981`.
2. `lionagi/state/schema.sql` (1,021 lines): 32 `CREATE TABLE` and 87 `CREATE INDEX`
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

- `schema.sql` declares four indexes that `schema_meta.py` does not, and `schema_meta.py`
  declares none that `schema.sql` does not: the metadata is a strict subset. The four are
  `idx_def_unique_version`, `idx_plays_show_name`, `idx_session_signals_seq` and
  `idx_sessions_run_id`. Absolute counts on 2026-08-15: 87 and 83.
- `schema.sql` declares 15 indexes with `DESC` key direction (`:93,198,204,283,311,378,…`);
  `schema_meta.py` declares zero. Production databases, built by `create_all`, therefore have
  none of those descending indexes. (SQLite and PostgreSQL can both scan an ascending index
  backwards, so the query-plan consequence is unmeasured; the definitional drift is proven.)

A guard that passes while its two subjects disagree is worse than no guard, because it is
cited as the reason the duplication is safe.

**P3 — DDL issuance and connection ownership are not confined to the state layer.**
`studio/operator/store.py` (2,349 lines) declares and creates 6 tables of its own and is in
effect a second persistence layer beside `StateDB`. Four `studio/services/*` modules create 7
more tables of their own. That DDL does not run at import: each module holds its
`CREATE TABLE` text as a module constant and executes it from inside request-handling functions —
20 of them, reached from 26 call sites — so the tables are re-asserted on the request path rather
than once at startup.
The lazy creation this produces has already leaked across module boundaries — `sessions.py`
reaches into `run_tags` for its private `_ensure_table` before a tag filter can run, because a
store that has never been tagged has no `run_tags` table. Separately, studio opens the store
directly — 38
`async with _open_db(...)` contexts across 9 files — rather than going through `StateDB`. The
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
wraps its inspection in `except Exception: continue` (`state/db.py:1045-1046`), and the open sequence
then stamps `SCHEMA_VERSION` unconditionally — so a database whose columns could not be
inspected still reports the current version. `version = 3` means "this build opened it", not
"this shape is known". Notably, the schedules rebuild already *derives its target table from
`schema_meta.py`* via `to_metadata` (`state/db.py:1631`, and again verbatim at `:1737`) — the
generated path half-exists and works on SQLite.

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
by a frozen class-level declaration composed from the hardened `Spec`, `Operable`, and `Params`
substrate in ADR-0119 (fields with types, nullability and defaults; foreign keys at the type
annotation; primary keys with composite ordering; indexes with explicit key direction;
per-entity toggles for audit columns, lifecycle-policy ownership, and legacy-facade exposure),
registered through one explicit composition root.
Everything else — table objects, insert/update builders, update allow-lists, the DDL snapshot,
migration plans — derives from the registry. No other module may independently author or issue DDL
for a managed table. An optional feature/extension may export an immutable registry/manifest
fragment, but only explicit store composition gives it ownership and only the state migration
service materializes it.

Importing an entity module never mutates a process-global registry. A caller composes a registry
from ordered declarations, and duplicate names or incompatible declarations fail at composition.
The registry's canonical form is deterministic across fresh processes.

Migration uses two named declaration snapshots, but only one can become runtime authority:

- `LegacyBaselineRegistry` is a fixture-only transcription of production `schema_meta.py` at a
  pinned source revision. It proves that the new compiler can reproduce the selected legacy
  SQLAlchemy objects exactly. Runtime vocabulary, CRUD, lifecycle projections, and migration
  targets may not import or derive from it.
- `TargetRegistry` is the prospective runtime authority. It freezes only after ADR-0123 and every
  other accepted logical-schema decision are incorporated; current aliases and table absence do
  not decide whether canonical Run is managed.
- the compiler produces `LegacyBaselineManifest` and `TargetSchemaManifest`. Every declaration
  difference between those complete manifests appears in an `AuthoredTargetChangeSet`, with its
  decision source, per-dialect risk, data transform or explicit `none`, and landing phase. An
  undeclared difference fails the gate.
- historical `schema.sql` and populated deployed shapes are `LegacyPhysicalVariant` fixtures, not
  more registries. Each recognized variant maps to a distinct preapproved
  `AuthoredMigrationPlan`. No gate requires
  `TargetRegistry` to equal a legacy authority.

**D2 — structural emission targets SQLAlchemy `MetaData`; everything else is an explicit
operation spec.** The specs generate the `Table` objects that `schema_meta.py` hand-writes today,
into one `MetaData`. This preserves `metadata.create_all`, dialect handling, and the
`to_metadata`-driven rebuild machinery, and it is the point where this design deliberately
diverges from the reference implementation's Postgres-DDL-text emitter. Compiling the current
metadata for both dialects confirms that partial index predicates, named check constraints and
server defaults survive the round trip, so the structural half is sound.

The boundary matters more than the target. `MetaData` describes the shape a table should have;
it cannot describe how a store gets there. These are explicitly outside it and become frozen
operation specs with per-dialect implementations, not things inferred from a desired shape:

- connection configuration and PRAGMAs, which `state/engine.py` owns today;
- seed and reference data, which `schema.sql` carries and `state/db.py` repeats outside
  `MetaData`;
- ordered data transformations. The existing attention migration installs a placeholder default
  and then derives real values from history, and a desired-minus-live structural diff cannot
  produce that;
- trigger bodies.

The whole-store authority is a composed `SchemaManifest`, not `MetaData` alone. It contains the
managed entity tables and table-owned constraints/indexes from `TargetRegistry`; managed operation
objects such as triggers and views that `MetaData` cannot express completely; explicitly declared
extension bundles; and an exact transitional ownership manifest that Phase 5 empties. Every
user-visible catalog object—table, index, trigger, view or materialized view, and PostgreSQL
trigger-function dependency—classifies exactly once as managed, declared extension, transitional,
dialect internal, or unknown. Dialect-internal objects are identified by catalog-adapter evidence,
never a caller-supplied wildcard or naming convention.

Two corrections to the reasoning that first motivated this choice, both from running it:

- `to_metadata()` is not a free primitive. The schedule-run rebuild already avoids the isolated
  form because cross-table foreign keys do not resolve in it, so a rebuild must be specified
  against the full metadata rather than a detached copy.
- Dependency-ordered creation is *useful, not mandatory*. An earlier draft justified `MetaData`
  partly on SQLite requiring parents before children. That is false: creating a child table
  before its referenced parent succeeds with `PRAGMA foreign_keys=ON`, a valid row inserts, and
  `foreign_key_check` returns empty, while an invalid row is still rejected. SQLite requires the
  foreign key to be *in the table definition*, which is the real constraint, and it is why
  `ALTER TABLE` cannot add one and why the rebuild pattern exists. Topological ordering remains
  the right default; it is not what decides this call.
- Kahn ordering needs a self-edge rule before it is adopted literally, because this schema
  already has one: `schedule_runs.chain_parent_id` references `schedule_runs(id)`, so a
  dependency graph built straight from the foreign keys contains a self-loop. `schedule_runs`
  then never reaches in-degree zero, so it never enters the queue and is reported as a residual
  cycle on a schema that is perfectly well-formed. Every other table still sorts normally, which
  is what makes this easy to miss: the failure is one unplaceable table at the end of a run that
  otherwise looks like it worked, not an ordering that refuses to start. The
  rule is to drop self-edges when building the graph. A table's dependency on itself carries no
  ordering information: it is satisfied by its own `CREATE TABLE`, which is exactly the
  `ALTER TABLE` limitation above, and the same holds for the rebuild path, where the copy target
  is created with the self-reference in its definition. Mutual cycles between *different* tables
  are a genuinely different case and remain an error rather than being dropped along with it.

`schema.sql` becomes a *generated* artifact emitted from the same specs plus the bootstrap
operation specs, retained for the compatibility tests that build old-style databases; it stops
being an authored authority. `ALL_TABLES` in the parity test is replaced by the registry's own
table list, so the test population can no longer drift from the package.

**D3 — parity is proven on physical semantics, through dialect catalog adapters.** The gate
compares column types, nullability, defaults, primary and foreign keys, unique and check
constraints, and the full index set including key direction — not the name sets the current test
compares. The 15 descending indexes, the four-index membership gap and the one default divergence
are resolved by an explicit recorded decision each, before either authority is deleted.

Stating the comparison is not enough, because the obvious way to implement it silently cannot
make two of those distinctions. Against the installed SQLAlchemy, generic SQLite reflection
returns *identical* `get_indexes()` output for an ascending and a descending index, while
`PRAGMA index_xinfo` differs in its direction bit; and `UUID`, `JSONB` and
`TIMESTAMP WITH TIME ZONE` columns all reflect as `NUMERIC`. A gate built on generic inspection
would therefore report equality across exactly the changes it exists to catch. Partial predicates
and named check text do survive the same reflection, so the defect is field-specific rather than
a reason to distrust reflection wholesale.

So the gate is defined as a frozen `PhysicalSchema` populated by per-dialect catalog adapters.
It includes normalized identity, definition digest, ownership, and dependency edges for tables,
indexes, constraints, triggers, views, extension objects, and PostgreSQL trigger functions. Five
separate comparisons have distinct purposes:

1. `LegacyBaselineRegistry` compiled to SQLAlchemy objects equals the frozen production
   `MetaData` at the pinned revision;
2. `TargetRegistry` compiled to `TargetSchemaManifest`/generated objects equals its target
   declarations;
3. `declaration_diff(TargetSchemaManifest, LegacyBaselineManifest)` equals exactly the
   `AuthoredTargetChangeSet`;
4. target-generated objects equal create-and-introspect results through each dialect adapter;
5. the canonical declaration and `PhysicalSchema` serializations are deterministic across fresh
   processes.

The SQLite adapter must read declared type text and effective affinity, index direction from
`PRAGMA index_xinfo`, partial predicates, constraint names and expressions, foreign key actions,
and normalized server defaults. It may use generic `Inspector` fields only where those are shown
sufficient for that field. PostgreSQL supplies the same inventory from its live catalog. The
cross-process comparison (5) is over the canonical serialization, not compiled DDL bytes:
formatting is not a physical semantic.

*The gate ships with a per-field must-fail fixture arm.* This is a landing condition on Phase 2,
not a later refinement. P2 is the whole problem: a parity guard that has never been shown a
divergence is indistinguishable from a broken one, and it gets cited as the reason the
duplication is safe. The same objection is what disqualified the reference implementation's
schema hash. So the gate carries one deliberately-divergent fixture per compared physical
semantic:

column type · nullability · default · primary key, including composite key *order* · foreign
key, including its actions · unique constraint · check constraint · index membership · index
key *direction* · unknown table · unknown trigger · unknown view · trigger target/event/body ·
view definition/dependency · zero or multiple owners for one catalog object.

Each arm must go red on its own. The acceptance test is a mutation, not a passing run: reverting
an adapter's read of one field must redden exactly that field's arm and no other, which proves
both that the arm is live and that the arms are independent. The two divergences already in the
tree — 15 `DESC` indexes against zero, and four indexes declared in one authority and not the
other — supply the direction and membership fixtures for free; the rest are constructed.

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
risk-classified plan: additive columns become `ALTER TABLE ADD COLUMN` *where SQLite will accept
one*; everything else becomes
a generated rebuild — the pattern `state/db.py` already implements by hand as seven rebuild
paths over five tables (`sessions`, `schedules` three times, `invocations`, `schedule_runs`,
`definitions`), each keyed to a different legacy shape it has to recognise and replace.
The generated rebuild derives the target table and every recreated dependent object from the
authored `SchemaManifest`. Catalog-returned DDL is never executed. Before mutation the planner
computes the reverse dependency closure of every rebuilt object. An unknown or opaque dependent
object blocks the plan before DDL or version writes; a declared extension affected by the rebuild
is recreated from its authored extension spec and re-introspected afterward. `CASCADE` is never
used to bypass ownership.

The current rebuilds do the opposite: they read the live catalog and replay it, assembling the
copy statement's column list from catalog-read names and re-executing index DDL strings taken from
`sqlite_master` (`state/db.py:1418-1422`, and the same shape in the six other rebuild paths).
That faithfully preserves whatever the database happens to contain, including objects nothing
declares, so a rebuild carries drift forward instead of resolving it — a database built from the
old DDL path keeps its 15 descending indexes through every future migration. It is also the one
place where identifiers reach SQL from a source no allow-list covers.

"Additive" is not a safe synonym for "cheap", and the classifier has to say so. D1 lets a field
be declared non-null without a default, and SQLite refuses `ADD COLUMN` for exactly that shape;
so does an expression default, which is why `artifacts.updated_at` is spelled differently in the
two authorities today. An additive column therefore classifies as `ADD COLUMN` only when it is
nullable, or non-null with a constant default that SQLite accepts. A non-null column without a
default, or with an expression default, is either routed to the generated rebuild with an
authored backfill supplying the value for existing rows, or rejected before execution when no
backfill is declared. Rejecting is the fail-closed answer and is preferred to inventing a value:
a migration that silently backfills a column the declaration says must be meaningful is a data
decision the schema layer is not entitled to make. The classifier is proven on a populated
fixture table, because on an empty one the distinction does not appear.

For each supported deployed variant, the separate physical gate is:

```text
physical_diff(TargetPhysicalSchema, LegacyPhysicalVariant[v])
    == AuthoredMigrationPlan[v]
```

A runtime live diff is only a candidate. Writable migration is authorized only when the complete
live snapshot equals one recognized variant and the candidate plan digest equals that variant's
preapproved plan. An unknown live shape remains quarantined; observing a diff never approves it.

The mirror of that sentence has to be stated too, because it is the arm that fails toward looking
safe. A live diff never *clears* a store either. An empty or non-matching diff is not evidence
that the deployed shape is current; it is evidence only about what the comparison could resolve.
An unrecognized variant is `Quarantined` when the snapshot is complete and `Unavailable` when it
is not, and neither is "no migration needed". A diff engine that reports nothing when it cannot
classify produces exactly the reading an operator most wants to see, which is why the disposition
is decided by whether a variant matched and never by whether the diff came back empty.

Risk is classified per dialect, not once: the same logical change has different mechanics on
each backend, so a type widening that is a cheap `ALTER` on PostgreSQL is a full table rebuild
on SQLite, and a plan that reports one risk for both is lying to whoever approves it. The
reference's two-phase execution model (transactional operations, then `CONCURRENTLY` /
`VALIDATE` outside the transaction) collapses on SQLite, which serializes all DDL and supports
neither; the port keeps the phase field for the PostgreSQL leg and treats rebuild-versus-alter
as the axis that actually carries risk here.
`MIGRATION_COLUMNS` is retired. The version/hash row advances only after post-apply introspection
confirms the resulting shape. The engine lands in observe-only mode first (classify and report,
apply nothing) so that unknown deployed shapes surface before any of them is migrated.

Failing closed has four explicit dispositions. A dropped connection cannot honestly promise a
readable quarantine handle:

- **`ReadyReadWrite`** — one complete, verified snapshot and a writable handle;
- **`RepairRequired`** — one complete recognized legacy snapshot, an authored migration plan, and
  an enforced read-only handle until that plan is explicitly applied;
- **`Quarantined`** — one complete unknown snapshot, findings, and an enforced read-only handle for
  inspection/export;
- **`Unavailable`** — no store handle because connectivity, complete inspection, or read-only
  enforcement failed. It promises neither inspection nor export.

Offline repair may apply the preapproved plan only from `RepairRequired`. `Quarantined` permits
inspection/export until an operator authors and approves a new `LegacyPhysicalVariant` plus
`AuthoredMigrationPlan`; exact reclassification then moves it to `RepairRequired`. No plan executes
directly from an unknown shape. Repair is an operation, not a fifth open disposition.

Quarantine has to be *enforced by the connection*, not by callers agreeing to behave, and that
needs a named mechanism per dialect or the state is read-only in name only:

- **SQLite** — the existing `mode=ro` URI open plus `PRAGMA query_only`, which is what
  `make_readonly_engine()` already does.
- **PostgreSQL** — a server-enforced form only: a role without write privilege on the schema, or
  a connection to a hot standby, which rejects writes at the server. `default_transaction_read_only`
  does **not** qualify. It is a session setting, and anything holding the connection can turn it
  off with one statement, so it protects against accident and not against the case quarantine
  exists for. An earlier draft of this decision listed it as the mechanism and named a role as
  merely the "stronger form where the deployment can provide one" — that is a fail-open dressed
  as a preference, and it is recorded here because it is the exact failure this whole decision
  was written to remove, reintroduced one paragraph after removing it. Setting it as well is
  harmless defence in depth; it never satisfies the requirement.

If no server-enforced form is available on a PostgreSQL store, the result is `Unavailable` rather
than a quarantine handle or a writable connection. This is the specific hole the current code names in its own
docstring: `read_only_open_supported()` returns False for server-backed stores, and it warns
that callers needing read-only *for safety* must not use it, because it hands them a writable
connection precisely there. A dual-dialect fail-closed contract cannot be built on a helper with
that shape, so the enforcement above replaces it rather than wrapping it. The fault-injection
suite runs on both dialects and asserts a write is refused *by the connection* under quarantine,
not merely that no write was attempted.

Unknown is a classification of a complete, internally consistent catalog snapshot;
`Quarantined` is a connection disposition. A partial snapshot is discarded and is never
structural evidence. The discriminator is whether inspection *returned*:

- **Transient** — the catalog read itself raised (I/O error, lock timeout, dropped connection).
  Nothing was learned about the shape. Retry, bounded: **3 attempts total, backing off 250ms then
  500ms**. Each attempt must return one complete snapshot; no partial result is merged into the
  next. If all three fail, dispose every connection and return `Unavailable`. A one-off I/O error
  must not lock a healthy production store out of writable open.
- **Structural** — inspection succeeded and returned a shape the registry does not recognize.
  Quarantine immediately, with no retry. Retrying here is pure delay: the answer is known and
  will not change, and a retry loop would only make an unknown-shape store look like a slow one.

The retry applies to the *transient* class only. A complete structural finding yields
`Quarantined` only after a new server-enforced read-only handle is established; otherwise it also
yields `Unavailable`. No failed or formerly writable handle is returned. `Unavailable` is not
cached across later opens, so restored connectivity or credentials can reach `ReadyReadWrite`.
The distinction is enforced at the catch site because the defect this replaces is exactly a
blanket `except Exception: continue` that could not tell the two apart and then stamped the
version anyway.

One consequence of "unknown shape means quarantine" has to be settled before Phase 4 turns
application on, because the phasing creates the condition on purpose. Migration applies in Phase
4; the operator-store tables do not enter the registry until Phase 5. Between those two points a
normal, healthy store contains the operator's six tables, which the registry has never heard of,
and a literal reading quarantines every deployment that has ever opened the operator. The seven
studio-service tables are not part of this: they re-declare tables the registry already owns, so
the diff recognises their shapes and only their ownership is unresolved until Phase 5.

The `TransitionalCatalogManifest` therefore distinguishes transitional from unknown objects. It
enumerates exact object identities and owners—not table-name patterns—and Phase 5 empties it by
moving each entry into the managed registry or a declared extension bundle. Transitional objects
remain dependency-inspected even while their internal shape is excluded temporarily from managed
diffing. Fixture presence never grants ownership. The end-state gate requires an empty
transitional manifest while every non-internal table, index, trigger, view, and trigger-function
dependency is still classified exactly once. An object in neither the manifest nor an authored
owner is genuinely unknown and quarantines the store.

The existing `trigger_log`/`schedule_runs_audit` rebuild fixture remains a Phase 0 statement of
legacy behavior, not permission to replay arbitrary live DDL. Phase 4 replaces it with four arms:
an unregistered bundle quarantines before rebuild with data and version unchanged; a registered
extension bundle rebuilds from its authored spec and still fires; a registered dependent view
remains valid; and mutation of live trigger/view SQL without a declaration change fails parity and
is never replayed.

No flag turns an unverified store writable; a `force` escape hatch would rebuild the fail-open
path this decision exists to close. Fault injection covers tables, columns, constraints, indexes,
triggers, views, dependency reads, connection loss, and read-only enforcement. Failure after
tables but before triggers discards the partial snapshot; failures on attempts one and two followed
by a complete third attempt classify only the third; three failures return typed `Unavailable`
with no handle, DDL, or version write. A complete unknown shape takes zero retries and returns a
connection whose write fails at the backend, while the same PostgreSQL shape without safe
read-only credentials returns `Unavailable`. A later open after restoration is evaluated anew.

**D6 — DDL issuance and store access become the state layer's exclusive right.** The six
operator-store tables and the 7 studio-service re-declarations register in the registry (the
latter as the state-owned tables they already duplicate). Studio loses its
per-request `CREATE TABLE` re-assertions and its own connection path; the 38 direct contexts move
to one `StateStore`/`TransactionRunner` port over the state engine, which removes the "equally
wrong" fallback and the three services that never call `require_file_store` along with it.
`StateStore` is persistence vocabulary; it is not the generic Event driver, ActionExecutor, flow
scheduler, or native-agent harness defined elsewhere.

**D7 — provider mirror placement is delegated to the feature-boundary decision.**
`state/claude_mirror.py`, `state/codex_mirror.py`, and `state/_mirror_common.py` create a real
state→provider dependency inversion, but moving them to a guessed provider folder is not a
persistence-schema decision or a proven pure move. ADR-0122 decides the provider transcript,
projection, and persistence port boundaries and their compatibility path. ADR-0118 removes the
state import inversion only through that accepted boundary; Phase 0 does not move these files.

**D8 — `reasons.py` is keyed two different ways at once; separate them.** The module holds two
vocabularies that look like one. The seven code classes are keyed on *producer domain*, and each
owns exactly one code prefix: `RunReasons` → `run.` (30 codes), `SessionReasons` → `session.`
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

*Do not rename the suffix.* An earlier draft proposed renaming all seven to `*Outcomes`, on the
strength of `RunReasons`' own docstring calling them "Outcomes of session execution". That is
wrong, and it is worth recording why rather than quietly dropping it. The module states its code
grammar as `<domain>.<status_or_outcome>.<cause>`, so an outcome is one *segment* of a code and
not the category. `SessionReasons` holds health-derived causes and a resume attribution, and
`PlayReasons` documents itself as holding lifecycle reasons. The physical columns are named
`status_reason_code` and the public validator reports `reason_code`. Renaming would trade one
mixed vocabulary for a new contradiction, across 497 sites, for no behavior change. The names
stay `*Reasons`; if the vocabulary is ever unified, a neutral `*Codes` is the candidate and it
belongs to its own decision, not to this one.

The `run.` prefix likewise stays grouped despite there being no `runs` table. Producer-domain
code groups and table-backed entity types are different axes, and the class owns persisted
strings spelled `run.` whatever the Python name is.

*Move the entity vocabularies to the registry only after the registry exists.*
`VALID_ENTITY_TYPES` and `ENTITY_TYPE_TO_TABLE`
are the fourth and fifth hand-maintained restatements of "which entities exist and what table
each lives in"; under D1 both are derived from the registry rather than typed. The aliases stay
as an explicit compatibility map. This is Phase 1 work, not a pre-registry Phase 0 move. The
domain reason classes remain separate from both registry-derived projections throughout.

"Derived from the registry" has to mean derived from a *projection* of it, and the projection
needs declaring, because the two sets are not the same size and never were. The registry holds
32 persisted entities. `VALID_ENTITY_TYPES` holds six — `session`, `show`, `play`, `invocation`,
`team`, `schedule_run` — and is the compatibility exposure of generic
`StateDB.update_status()`. It is **not** the complete set whose transitions LifecycleService
validates: the live policy registry also owns `dispatch`, and dispatch/outbox production code
actively invokes that policy through the service. Deriving either set from the full registry, or
pretending the two sets are equal, would widen or break a write-time contract.

So D1 grows two independent per-entity declarations alongside the audit-column toggles:

- `lifecycle_policy_managed` means one ADR-0058 `LifecyclePolicy` exists and the typed owner may
  invoke LifecycleService;
- `legacy_status_facade_exposed` means the generic compatibility `StateDB.update_status()` accepts
  the entity kind and can derive `ENTITY_TYPE_TO_TABLE` for it.

`LegacyBaselineRegistry` must reproduce today's **seven** lifecycle-policy-managed entities—the
six facade values plus `dispatch`—and today's **six** facade-exposed values exactly.
`TargetRegistry` adds canonical `Run` to the policy projection, producing eight, while leaving the
legacy facade projection at six. RunRepository calls the typed LifecycleService port; it is not
silently added to a generic string facade. Exposing either `dispatch` or `run` through that facade
requires a later explicit compatibility decision and tests. The old constants are not deleted
until both projections, including their intentional difference, pass.

**D9 — what carries over, what is reworked, what is left behind.** The target design splits
cleanly along a dialect seam, and that seam is what makes adopting it tractable. The reference is
evidence and a source of patterns, not a code donor: its defaults handling is useful, while its
post-init validation path and mutable content-hash model have their own defects. LionAGI ports
semantics through ADR-0119 and proves them locally.

- *Reimplement the semantics on LionAGI primitives* — immutable column, foreign-key, index,
  trigger, check, unique, entity, and physical-schema declarations; entity→spec and
  registry→schema composition; migration operation/plan values; operation/risk enums;
  spec-comparison and type-change classification; identifier validation and order-by
  sanitization. These use hardened `Params`, `Spec`, `Operable`, Sentinel semantics, and canonical
  serialization rather than adding a parallel frozen-dataclass/schema hierarchy. Their data
  carries no dialect syntax; `to_ddl` methods belong to the rework bucket below.
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

### Ordering against ADR-0117

ADR-0117 declares its canonical schema target as hand-written `CREATE TABLE` text for
`progression_items` and `progression_storage_state`. Under D1 that is one more authored
authority, so the two ADRs have to agree on which absorbs which rather than discovering it later.
Naming it here because this document moves first:

- **If ADR-0117 implements first**, its two tables are hand declarations that this registry folds
  in during Phase 1, on the same footing as the 16 already inventoried. They join the Appendix A
  population at that point rather than being treated as exceptions.
- **If this ADR implements first**, ADR-0117's tables are authored as entity specs from the
  outset and its DDL block becomes illustrative rather than the authority.

Either way the D3 gate inherits three ADR-0117 decisions verbatim, and all three are exactly the
physical semantics the gate exists to compare: the composite primary key `(progression_id,
ordinal)` including its *order*, the foreign key's `ON DELETE CASCADE` action, and the
**deliberate absence** of a unique constraint on `(progression_id, message_id)`. That last one is
the trap. A registry that helpfully derived a unique index from "these two identify a row" would
break ADR-0117's backfill, which must faithfully preserve legacy collections that already contain
the same identifier more than once. An absent constraint is a declared decision here, not an
omission to be corrected, and the gate has to be able to tell those apart.

### Amendment boundary with ADR-0056

On acceptance, this ADR supersedes these specific ADR-0056 clauses and no others:

- D2's statement that hand-authored `schema_meta.py` `MetaData` is the runtime schema authority;
  generated `MetaData` becomes a projection of the EntitySpec registry;
- D2's authored `schema_migrations.py` additive ledger, `schema.sql` compatibility authority,
  fixed open-sequence reconciliation list, and bespoke SQLite rebuild ownership; these become
  generated or authored operation-spec projections under D3–D5;
- D6's PostgreSQL read-only guidance where safety/quarantine is required: an application request
  for a read-only role is not itself enforcement, and an unavailable server-enforced role or hot
  standby denies quarantine open.

ADR-0056 D1's normalized asynchronous `StateDB` compatibility façade, D3/D4 transaction and
locking semantics, D5's explicit dialect seams, and D6's shared-instance lifecycle remain in
force unless a later accepted ADR names a replacement.

## Phasing

Each phase lands behavior-preserving, gated by equality proofs, and independently valuable.

The ordering constraint that decides this list: the instrument that reads *deployed* shapes has
to exist before anything generated depends on being right about them. An earlier draft put
generated writers before introspection, which is backwards. `create_all` does not add a newly
declared index to a table that already exists — the separate index ledger exists precisely for
that — so a schema change made in the writer phase would still have to be applied to the spec and
the old migration ledger together, and a missed edit points a generated writer at a shape the
upgraded store does not have.

- **Foundation gate:** accept and implement the relevant ADR-0119 contracts for dataclass
  defaults/default factories, Sentinel states, ordered field identity, structural equality/hash,
  and canonical serialization. Pin current legacy behavior with fixtures before changing it.
  ADR-0118 may not introduce a second declaration substrate to bypass this gate.
- **Phase 0:** freeze `LegacyBaselineRegistry`, all six legacy schema authorities, populated
  `LegacyPhysicalVariant` databases, and every table/column/index/default/constraint/trigger/view
  as a fixture corpus; record an explicit decision for each current divergence. The
  `trigger_log`/`schedule_runs_audit` case is labeled discovered-but-unowned. No production
  behavior, provider mirror, target-registry, or registry-derived vocabulary moves in this phase.
- **Phase 1:** land the shared compiler, `LegacyBaselineRegistry`, `TargetRegistry`,
  their compiled manifests, `AuthoredTargetChangeSet`, storage codecs, and canonical declaration
  serialization in shadow mode. Prove legacy-baseline-to-current-`MetaData` equality separately
  from target-to-declaration equality, and prove the declaration-manifest diff equals the authored
  change set. Generate D8's two target projections in shadow: `lifecycle_policy_managed` contains
  today's seven policies including dispatch plus accepted canonical Run (eight), while
  `legacy_status_facade_exposed` remains today's six. No target-to-legacy equality, live catalog
  parity, generated writer, or accidental generic-facade expansion is claimed here.
- **Phase 2:** implement the SQLite and PostgreSQL physical-catalog adapters, all per-field
  and object-ownership/dependency adapters, all per-field/object must-fail mutation fixtures,
  create-and-introspect parity, cross-process `PhysicalSchema` determinism, the full
  ready/repair/quarantine/unavailable fault matrix, observe-only deployed diff, and an explicit
  per-`LegacyPhysicalVariant` `AuthoredMigrationPlan` catalog while every old authority still
  exists. **Landing condition:
  reverting one adapter field read reddens exactly that semantic's fixture arm.** The
  `DESC`/index-count divergences are decided here. The phase applies nothing. The data-migration
  catalog is authored rather than derived because desired-minus-live cannot produce
  placeholder-then-backfill or a semantic correction.
- **Phase 3:** generated statement builders for insert/update, in shadow/equality mode, with a
  generated dict-compatibility adapter; retire the interpolation sites and `_*_COLUMNS`;
  validated identifier types in `LifecyclePolicy`. Equality proofs pin generated SQL against
  current SQL on fixtures.
- **Phase 4:** migration application under the four-disposition contract; forbid replay of raw
  catalog DDL; run the registered/unregistered extension and dependent-view matrix; retire `MIGRATION_COLUMNS`;
  delete the old authorities; the seven bespoke rebuild paths become instances of the generated
  rebuild, ported in risk order (the three generated schedules rebuilds first, literal
  sessions/invocations next, then definitions, and schedule-runs last because it carries backups,
  indexes and triggers). The `definitions.kind` rebuild is one of the seven and is easy to miss
  when reading the phase as "port the CHECK-constraint rebuilds": it drops a legacy two-value
  `kind` CHECK that has to be gone before a `kind='skill'` row can be saved, so it carries a
  behavioural precondition and not only a shape change.
- **Phase 5:** fold operator-store and studio-service tables into the registry and route the 38
  direct connections through `StateStore`/`TransactionRunner` (largest blast radius; last).
  Operator's atomic CAS SQL is preserved verbatim and moved, never rewritten in the same change
  as its schema. The transitional manifest must be empty across tables, triggers, and views.
  ADR-0122 owns any provider mirror relocation after its dependency boundary is accepted.

### Implementation issue rescope

- #3213 is Phase 0 fixtures/inventory only. Remove D7 mirror moves and the registry-derived D8
  projection from its acceptance criteria.
- #3214 owns the shared compiler, the fixture-only legacy baseline, the target registry and authored
  change set, their separate shadow gates, and D8's policy-managed/facade-exposed projections. It
  depends on the ADR-0119 foundation issue and absorbs #3205/#3227 as exact parity fixtures. It
  does not claim target-to-legacy or live physical-catalog parity.
- #3215 owns dialect catalog and object-ownership/dependency adapters, per-field/object
  mutation-proof gates, the unavailable/quarantine matrix, observe-only deployed inventory/diff,
  and preapproved per-variant migration/transformation catalog. #3206 remains an independently
  actionable fail-open defect linked to this phase rather than delayed by it.
- #3216 remains generated CRUD/codecs in shadow equality mode.
- #3217 must require a server-enforced PostgreSQL read-only role or hot standby for quarantine; it
  may mention `default_transaction_read_only` only as defense in depth and must return
  `Unavailable` when no safe handle exists.
- #3218 is split after approval into registry/table ownership and Studio/operator routing through
  `StateStore`/`TransactionRunner`. #3207 remains a standalone wrong-store safety fix linked to
  the latter.
- #3201–#3204 remain lifecycle truth, durable delivery, and audit-ordering work. They are not
  absorbed into a schema-declaration epic.

**D10 — lifecycle reuses foundation mechanics while dispatch taxonomy stays in ADR-0120.**
"This layer re-implements primitives" is true of the package as a whole and false in the specific
places it is most tempting to change. This persistence ADR owns only the invariant that required
lifecycle evidence is committed before post-commit delivery. ADR-0120 owns interception,
observation, fan-out, callback policy, and compatibility façades.

Already correct, leave alone: the terminal-callback path uses `ln.concurrency` for task groups,
cancellation and shared deadlines. That is the canonical primitive and it is not duplicated.

Worth converging:

- Wire-model serialization. `RunTerminalEnvelope`, `Correlation` and `EntityRef` hand-write
  `to_dict` where `Element` already provides typed identity and `to_dict(mode=...)`. Small, and
  it removes a hand-maintained serializer from a message contract.
- Retry. There is no retry in the delivery path today; when durable delivery lands (see the
  unreconciled ledger, P-adjacent), it uses `ln.concurrency.retry` through
  `service/resilience.py` rather than a fourth backoff loop.
- Registration and dispatch mechanics may use ADR-0120's policy-declared fan-out kernel.
  `TerminalCallbackRegistry` remains state-owned because its envelope, filtering, shared deadline,
  override precedence, and post-commit ordering are persistence semantics.

Explicitly rejected, because the vocabulary matches and the semantics do not:

- Persisted lifecycle rows do not inherit `Element`. Removing dataclass boilerplate is not a
  reason to give database rows an identity model built for in-memory objects.
- `protocols.generic.Event` is not the persistent status-transition model. It shares the words
  (status, completion) and means something else: in-memory execution state, not durable history.
- The callback registry is not replaced by `Broadcaster` wholesale. Lifecycle dispatch needs
  per-registration filtering by entity kind and id, override precedence, a shared handler
  deadline, and post-commit ordering; the broadcaster has none of those, and adding them to a
  generic singleton pub/sub to avoid a duplicate registry would push lifecycle semantics into a
  shared primitive. ADR-0120 shares registration/dispatch mechanics without moving those four
  properties.

The genuine consolidation for this layer is the one this ADR is about: the registry owns storage
shape, one lifecycle service owns transition semantics, and the general primitives supply
concurrency, serialization and retry underneath both.

## Scope boundary: typed rows

`StateDB`'s read surface returns untyped rows — 51 methods annotated
`dict[str, Any] | None` or `list[dict[str, Any]]` — so every consumer re-parses the same fields
and no reader is checked against the schema. It is the same root cause as P1: nothing connects
the declared schema to the code that uses it.

A decoder does *not* fall out of the physical type, and an earlier draft claimed it nearly did.
The physical column cannot distinguish a JSON document stored as `JSON` from one stored as
`TEXT`, or a generic `BLOB` from a packed finite-float vector: `messages.embedding` is
`LargeBinary` and `progressions.collection` is `Text`, while their logical values need float32
packing and JSON serialization respectively, and the existing row adaptation already does
tolerant JSON decoding over a named field set. The same gap runs the other way, on writes:
generated statements have to preserve bind types, domain defaults, validation and value
transformations that the hand-written builders currently perform *around* the SQL.

So codecs are not a read-side convenience to be deferred with the read surface. A spec names a
logical type, a physical type per dialect, a bind codec, a result codec and a validation policy,
and generated CRUD may not infer any of those from the SQLAlchemy type alone. That lands in
Phase 1, with a generated dict-compatibility adapter in Phase 3 so existing callers keep the
shape they read today.

What stays out of scope is migrating the 51 public read signatures to typed returns. That
touches every caller in `cli/` and `studio/`, and bundled in here it would make the equality
gates unprovable. It needs its own decision, taken once the generated decoders exist.

## What this deletes

Measured file by file at the current head. "Deleted" means the hand-authored source stops
existing, because the same facts are generated from the registry.

Two kinds of figure appear below and they carry different weight, so the method is stated rather
than left to the reader. **Structural counts** — whole-file line counts, table and column counts,
method counts, the number of `_*_COLUMNS` frozensets — are derived from the AST or from the
declared `MetaData`, never from a line-oriented pattern. That distinction is not pedantic: a
regex requiring the value on the same line as the name undercounts anything spelled across
lines, which is exactly how an earlier draft of this document reported 29 `RunReasons` codes
instead of 30. Every structural figure here was re-derived that way and reconciles.
**Region-bounded counts** — the rebuild machinery's 956 lines, the operator schema region's 244,
the 286 duplicated lines in three service modules — depend on where the region is judged to
start and stop. They are honest measurements of a boundary someone drew, they are not
reproducible from a command alone, and they should be treated as sizing rather than as facts.

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

The larger cut is smaller than the module sizes suggest. The six studio service modules carrying
persistence total 2,387 lines. Within `projects.py`, `shows.py` and `signals.py` (1,209 lines of
that), the code reimplementing a `StateDB` method that already exists measures 286 lines: 159 of
`projects.py`'s 346, 78 of `shows.py`'s 801, and 49 of `signals.py`'s 62. `StateDB` already
carries six project methods, five show methods and two signal methods. The bulk of `shows.py` is
filesystem show-import and presentation logic that no store method covers, so it is not a
candidate for this cut at all. `run_tags`, `approvals` and `attention` own real domain logic and
only their DDL and connection handling go.

Phase 5 cannot treat those 286 lines as a mechanical delete, because the pairs have drifted in
response shape. `signals.py` coalesces `op_id` to `""` and `payload` to `{}` where `StateDB`
returns whatever the row holds, and a client reading the signal stream sees the difference.
`projects.py`'s `update_project` reports success for a patch carrying no mutable fields when the
project exists, where `StateDB` returns `False`, which is the difference between a 200 and a 404
at the route. Each swap is an API contract decision with a caller behind it, so the
caller-by-caller pass Phase 5 opens with is what sizes this work.

Add to it the 38 direct store connections across 9 modules that collapse to one
`StateStore`/`TransactionRunner`, the
repeated session-by-id lookups in the operator package, and the admin-event insert duplicated
between `studio/operator/store.py` and `StateDB.insert_admin_event`. Sizing the rest is Phase 5's
opening pass, not something to estimate from here.

## Consequences

- One column addition becomes one edit in one place, and "what is the schema" has one answer.
- The identifier-interpolation class disappears rather than being re-guarded site by site.
- Migration gains introspection, risk classification and a fail-closed contract, which the
  current hand-ledger cannot express; SQLite's `ALTER TABLE` limits are honored by making the
  rebuild a generated operation instead of seven bespoke ones.
- Studio stops being able to open the wrong database file.
- Cost: a multi-phase epic touching the widest-blast-radius files in the repository. The
  equality-proof gates are what make it safe; skipping them to move faster re-creates the
  problem this ADR exists to end.
- Risk concentrations: the data-preserving rebuild generator (Phase 4) and the operator-store
  fold (Phase 5). Both are sequenced last deliberately.
- What this ADR does *not* claim: no current SQL injection was found, and no query-plan or
  performance consequence of the index divergence has been measured. The case for the change is
  drift and ownership, not a live exploit.

## Appendix A — inventory

Counts are source-site counts, not runtime frequencies, and the units are deliberately not
summed: a declaration, a statement string, and an execution call are different things.

| Where | Declares schema | Issues DDL | Builds SQL | Notes |
|---|---|---|---|---|
| `state/schema_meta.py` | 32 tables, 380 columns, 83 indexes | via `create_all` | — | what production builds from; also 34 PK columns, 27 FKs, 20 CHECKs, 8 uniques, 29 partial indexes, 37 server defaults — Phase 1 compares generated objects and Phase 2 compares physical catalogs |
| `state/schema.sql` | 32 tables, 87 indexes, 5 pragmas, 3 seeds | tests only | — | `_SCHEMA_PATH` at `db.py:122`; not executed by writable open |
| `state/schema_migrations.py` | 127 additive column declarations over 14 tables | 10 indexes per dialect | — | the two dialect tuples are textually identical |
| `state/db.py` | 3 inline `CREATE TABLE` (rebuild targets) | yes | 256 execution calls | 6,683 lines; 5 `_*_COLUMNS` allow-lists; 45 `S608` |
| `state/lifecycle/` | — | — | 8 execution calls | policy table/patch identifiers unvalidated |
| `studio/operator/store.py` | 6 tables (+2 indexes) + own migration | yes | 119 sites | 2,349 lines; a second persistence layer |
| `studio/services/*` | 7 re-declarations of state tables, 4 modules | on the request path, in 20 request-handling functions | 93 execution calls | 38 direct store connections in 9 files |

Divergences found between the two full-schema authorities, all currently green.

The index rows are stated as *membership* rather than as a pair of totals, because the totals
move under ordinary schema traffic and the divergence does not. Both totals rose by one within a
day of this document being written, together, leaving the gap and its four member names
unchanged. Anything downstream that needs a stable figure — an acceptance threshold, a fixture
count — takes it from the membership row and the direction row, never from the totals. The
totals are dated for that reason. Method, so it can be repeated rather than trusted: index names
from `schema.sql` by matching `CREATE [UNIQUE] INDEX` with line comments stripped, index names
from `schema_meta.py` by walking its syntax tree for `Index(...)` calls, both parsers asserted
non-empty, then compared as name sets in both directions rather than by subtracting counts.

| Divergence | `schema.sql` | `schema_meta.py` |
|---|---|---|
| Index membership | 4 declared here and absent there | 0 declared here and absent there |
| Index count (2026-08-15) | 87 | 83 |
| Descending index direction | 15 indexes | 0 |
| `idx_sessions_run_id` | present | absent (also in the migration registry) |
| `artifacts.updated_at` default | `DEFAULT (strftime('%s','now'))` | no `server_default` |
| Header version | `v1` in the comment | seeds `version` `3` |

The `artifacts.updated_at` divergence is deliberate and carries its reason in the code: `ALTER
TABLE` rejects an expression default, so the column that `schema.sql` creates with one is
declared without it on the `MetaData` side, and the insert path sets the value explicitly. D3
treats defaults as physical semantics, so this is a divergence the parity gate must decide,
which means deciding it here rather than noting it.

**The decision: the generated authority carries the server default**, matching `schema.sql`. The
reason the `MetaData` side omits it is a *migration* constraint, not a *declaration* one — the
column could not be added by `ALTER TABLE` with an expression default — and the phase above now
routes exactly that shape through the generated rebuild with an authored backfill. The constraint
that forced the omission is the one this ADR removes, so preserving the omission would be
carrying a workaround past the thing it worked around. The insert path keeps setting the value
explicitly; a default is a floor for writers that forget, not a licence to stop being explicit.

Existing stores split cleanly and neither case needs data written. A store built from
`schema.sql` already has the default and diffs clean. A store built from `create_all` lacks it,
which is a physical-semantics difference the diff now detects, classified as a rebuild rather
than an `ALTER` for the reason above. No row changes value in either case: the column is `NOT
NULL` in both authorities and every existing row already carries a value the insert path wrote.
That is what makes this one safe to decide now instead of deferring it with the index decisions —
it is a declaration change with an empty data migration, which the `DESC` index questions are not. Naming it is also a correction: this table previously implied the
two authorities differed only in index count and direction.

The defaults dimension was then enumerated across all 32 tables rather than spot-checked, since
a dimension proven to be missing an entry is not usefully repaired one row at a time.
`schema.sql` carries 38 column defaults and `schema_meta.py` 37; every other pair matches once
SQLite's quoted rendering is normalised against SQLAlchemy's. `artifacts.updated_at` is the
whole of the gap.

Cross-cutting: 361 lexical `text(` sites in `state/` + `studio/` against zero in `protocols/` and
`service/`; 88 `S608` suppressions across 12 files; 22 identifier interpolations; 34
`BEGIN IMMEDIATE` sites; 51 `StateDB` methods returning untyped row dicts.

Duplicate query ownership worth its own issues: session-by-id is reimplemented five times across
the operator modules against `state/db.py`'s own; projects, shows and signals each duplicate a
`StateDB` API in `studio/services/`; the operator store repeats one frame insert eight times and
one admin-event insert six times; approvals and attention each implement the same
lock-read-append ledger separately.
