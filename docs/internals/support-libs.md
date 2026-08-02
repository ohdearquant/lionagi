# Support libraries — design notes

Extracted rationale for `lionagi/casts/`, `lionagi/lndl/`, `lionagi/testing/`,
`lionagi/libs/`, `lionagi/adapters/`, `lionagi/dispatch/`, `lionagi/models/`,
`lionagi/work/`, `lionagi/orchestration/`, and the top-level `lionagi/*.py`
modules — material that a maintainer needs but that doesn't belong as an
in-source essay. Source points here with `# See docs/internals/support-libs.md#<anchor>`.

<a id="spec-limits"></a>

## _spec_limits: MAX_SPEC_PROMPT_CHARS

`MAX_SPEC_PROMPT_CHARS` is one number, read by every surface that validates
an orchestration spec (the CLI spec validator, two Studio services) — a
single place to raise the bound rather than three copies that can disagree.

The module deliberately imports nothing: two of its three readers are
request-path services whose import cost is paid on startup.

Bound is `256 * 1024` — set for the pathological file, not the long prompt.
An orchestration prompt carries the whole task (brief, constraints, exit
criteria); the limit is far enough out that no honest spec reaches it while
still refusing a file that isn't a prompt.

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

`LIONAGI_ANTIGRAVITY_PRINT_TIMEOUT` is the Antigravity print-mode subprocess
cap (seconds), default one hour. Override by name for a different ceiling.

<a id="field-model-to-spec"></a>

## models/field_model: `FieldModel.to_spec`

`to_spec()` forwards every metadata entry as-is so unknown keys survive, an
explicit `default=None` is preserved (not gated on `is not None`), and
`json_schema_extra` stays a nested value rather than being flattened into
field-level kwargs (a `"default"` key inside it must never become the
runtime default).

Metadata is passed as a `Meta` tuple, not `**kwargs`, so a key that collides
with a `Spec.__init__` parameter (`self` / `base_type` / `metadata`) survives
instead of raising. `nullable`/`listable` are derived flags: supply them
explicitly and drop any stored duplicates so `CommonMeta.prepare()` sees each
key exactly once.
