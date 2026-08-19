# Support libraries — design notes

Extracted rationale for `lionagi/casts/`, `lionagi/lndl/`, `lionagi/testing/`,
`lionagi/libs/`, `lionagi/adapters/`, `lionagi/dispatch/`, `lionagi/models/`,
`lionagi/work/`, `lionagi/orchestration/`, and the top-level `lionagi/*.py`
modules — material that a maintainer needs but that doesn't belong as an
in-source essay. Source points here with `# See docs/internals/support-libs.md#<anchor>`.

<a id="immutable-registry"></a>

## ln.types.registry: explicit immutable composition

Declaration modules export `RegistryFragment` values; importing one never changes an active
registry. Each fragment names one owner and contract version and contains an ordered tuple of
explicitly keyed `RegistryEntry` values. A named application root calls `Registry.compose()` with
the fragments selected for that profile. The resulting snapshot retains source-bearing records,
the exact fragments, and any authorized override history, so two profiles can coexist without a
process-global collector.

Duplicate owners always fail. Duplicate keys fail unless the concrete `Registry` subtype declares
one exact, version-bound `RegistryOverrideRule`; callers cannot pass an ad hoc rule to composition.
The first declaration fixes a key's output slot, while an allowed replacement changes the active
record in that slot and records both entries. Values must be recursively immutable enough for the
runtime structural-key contract. Mutable lists, mappings, `DataClass` instances, and unresolved
sentinels are rejected rather than hidden inside a frozen snapshot.

Composition-authority subtypes use one `Registry[T]` base (including `Registry[ItemT]` for a
reusable generic subtype), without ABC or mixin bases. They may declare frozen, structurally stable
`ClassVar` metadata; it is excluded from snapshot fields and projection. They cannot add instance
fields or methods. A subtype is a declaration-only authority for override rules and metadata, not
a domain facade. Domain lookup helpers and cross-entry validation live in a separate facade; when
such invariants exist, that facade is the exported composition root and keeps the marker subtype
and raw `Registry` snapshot encapsulated. It returns domain projections or the facade itself, not
the marker instance as public authority, so callers cannot recover inherited `compose()` through
`type(snapshot)` and bypass domain validation.

This layer does not discover plugins, import optional modules, or decide what a missing feature
means. Domain loaders choose fragments and own feature-specific errors. It also does not provide a
durable digest: `Registry.version` is a contract version for override admission, while the strict
snapshot envelope and canonical content digest are separate ADR-0119 D7 work.

<a id="spec-limits"></a>

## _spec_limits: MAX_SPEC_PROMPT_CHARS

`MAX_SPEC_PROMPT_CHARS` is one number, read by every admission surface that
validates an agent or orchestration prompt. The readers are named rather than
counted, because a count goes stale silently when a reader is added while a
missing name is visible:

- `lionagi/_flow_spec.py`
- `lionagi/cli/agent.py`
- `lionagi/cli/orchestrate/__init__.py`
- `lionagi/mcp/dispatch.py`
- `lionagi/studio/scheduler/subprocess.py`
- `lionagi/studio/services/run_resume.py`

Schedule create/update and ad-hoc launch validation delegate to the scheduler
subprocess validator; playbook validation delegates to the flow-spec validator.

It was written out separately in each of them, which meant that many chances for
the copies to disagree and no single place to raise the bound.

The module deliberately imports nothing. Most of its readers are Studio services
whose import cost is paid on startup, and a constant that arrives with a module
graph behind it charges every one of them for a number.

The bound exists for the pathological file, not for the long prompt. An agent
or orchestration prompt carries the whole task — the brief, the constraints,
the exit criteria — and a real one had already been squeezed to fit the old
8192 limit, close enough to normal writing that an ordinary edit could push a
working spec over it and kill the run at submit. `256 * 1024` is set far enough
out that no honest prompt reaches it, while still refusing a file that isn't a
prompt. Single-agent runs use the same bound because their prompt enters the
same provider and persistence substrate; transporting it through a file removes
the operating system's argv ceiling but is not a reason to admit unbounded
request memory. File readers consume at most the cap plus one character, which
is enough to distinguish an accepted prompt from an oversized one without
loading the rest.

<a id="class-registry-builtin-modules"></a>

## _class_registry: `_BUILTIN_MODULES`

`_BUILTIN_MODULES` lists the built-in modules that define Element/Node
subclasses. Persisted `lion_class` metadata written before the
fully-qualified-name convention was adopted stores a bare class name (e.g.
`"Instruction"`) instead of a dotted path. Importing these modules on a
short-name lookup miss (a) triggers `Node.__pydantic_init_subclass__`
registration into `LION_CLASS_REGISTRY` for Node subclasses, and (b) makes
every built-in class directly attribute-lookupable on its module, without
scanning the filesystem.

<a id="path-safety-contain-relative-path"></a>

## libs/path_safety: `contain_relative_path`

`contain_relative_path(value, root, field_name)` is the one containment
predicate shared by workspace-relative consumers (sandbox seed-input and
artifact-manifest paths, among others) — extend this function rather than
writing a new local check at each call site.

It rejects absolute paths (including Windows drive letters), NUL bytes, and
`..` traversal in the raw string via `check_path_safe()`, then resolves the
candidate against `root` (following symlinks) and rejects any result that
escapes `root` via `contain_and_resolve()`. Raises `ValueError` on any
violation and returns the resolved absolute `Path` on success.

<a id="config-liveness-timeouts"></a>

## config: liveness timeouts

`LIONAGI_WORKER_LIVENESS_TIMEOUT` is the first-output liveness window
(seconds) for CLI-streaming `run()` turns: a worker whose subprocess produces
no first stream chunk within this window is retried once (fresh subprocess),
then fails loud with `WorkerLivenessError` instead of hanging as a zombie
"running" leg. `0` disables the watchdog (deterministic / test runs).

`LIONAGI_WORKER_IDLE_TIMEOUT` is the between-chunk idle window for
early-streaming CLI turns (default 600 seconds). It resets after every chunk.
The window has to clear the worker's slowest single tool call, not its slowest
chunk, because a worker emits nothing for the duration of any tool it runs.
A miss after partial output raises `WorkerLivenessError` with reason
`worker.stream_idle` without retrying the subprocess. `0` disables this window.
Buffered transports receive neither default watchdog because silence is their
normal behavior; callers may still opt in with per-run `idle_timeout`.

`LIONAGI_ANTIGRAVITY_PRINT_TIMEOUT` is the Antigravity print-mode subprocess
cap (seconds). One hour is comfortably above expected caller deadlines while
retaining a finite subprocess bound; deployments that need another ceiling
override this setting by name.

<a id="field-model-to-spec"></a>

## models/field_model: `FieldModel.to_spec`

`FieldModel` is a compatibility facade. New declarations should construct `Spec` directly;
`to_spec()` exists to move legacy inputs immediately onto the neutral authority.

`to_spec()` preserves unknown keys and values, an explicit `default=None`
(rather than gating on `is not None`), and a nested `json_schema_extra` value
(a `"default"` key inside it must never become the runtime default). Repeated
legacy keys are compatibility-normalized before entering strict `Spec`: the
first owning `name` survives, while ordinary keys and validators use their last
effective value, as defined by ADR-0050.

Metadata is passed as a `Meta` tuple, not `**kwargs`, so a key that collides
with a `Spec.__init__` parameter (`self` / `base_type` / `metadata`) survives
instead of raising. `nullable`/`listable` are derived flags and are emitted once
from their effective legacy values, so `CommonMeta.prepare()` sees every key
exactly once.
