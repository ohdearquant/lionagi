# Support libraries — design notes

Extracted rationale for `lionagi/casts/`, `lionagi/lndl/`, `lionagi/testing/`,
`lionagi/libs/`, `lionagi/adapters/`, `lionagi/models/`, `lionhub/src/lionhub/orchestration/`
(including `lionhub/src/lionhub/orchestration/dispatch/` and
`lionhub/src/lionhub/orchestration/work/`), and the top-level `lionagi/*.py`
modules — material that a maintainer needs but that doesn't belong as an
in-source essay. Source points here with `# See docs/internals/support-libs.md#<anchor>`.

<a id="spec-limits"></a>

## _spec_limits: MAX_SPEC_PROMPT_CHARS

`MAX_SPEC_PROMPT_CHARS` is one number, read by every admission surface that
validates an agent or orchestration prompt. The readers are named rather than
counted, because a count goes stale silently when a reader is added while a
missing name is visible:

- `lionagi/_flow_spec.py`
- `lionagi/cli/agent.py`
- `lionagi/cli/orchestrate/__init__.py`
- `lionhub/src/lionhub/serve/mcp/dispatch.py`
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

`get_class` resolves a name in this order: `LION_CLASS_REGISTRY`, a dotted path
whose module is already imported (read from the module's namespace, so a
module-level `__getattr__` is not called), a dotted path under an allowed
module prefix (`lionagi.`, the new prefix of each package moved out of
lionagi such as `lionhub.orchestration.`, and any prefix added with
`lionagi.ln.register_type_prefix`, loaded through `load_type_from_string`),
then the short-name lookup above, then a `LION_CLASS_REGISTRY` key ending in
the short name. Of dotted names, only one under an allowed prefix reaches the
short-name steps, which keeps a lionagi class whose module has moved loadable,
and there it matches only a class defined under that same prefix; any other
dotted name the first steps do not resolve raises `ValueError` rather than
resolving to a built-in or registered class that shares its short name. A bare
short name matches a class defined under `lionagi.` in the built-in modules
(never one they merely import, such as `Path`) or a registered class. A
dotted path through an already-imported module resolves to whatever class that
module's namespace holds under the name, including one the module only imports
(`lionagi.protocols.generic.pile.Path` is `pathlib.Path`). It imports a module
only under an allowed prefix, together with the parent packages Python imports
first, so a prefix naming a subpackage (`pkg.sub.`) lets it import the package
above (`pkg`) as well. A name with an empty part (`.Foo`, `pkg.`, `pkg..Foo`)
is refused before any import, unless that exact name is a `LION_CLASS_REGISTRY`
key. When a dotted name under an allowed prefix fails to load and no later step
resolves it, the `ValueError` says why loading failed and chains that error.
`Element.from_dict` accepts only an Element subclass from this lookup and
raises `ValueError` otherwise, and a `Pile`'s `item_type` names
resolve the same way, so data naming a class from a package of your own needs
its defining module imported first or its package prefix registered. The error
gives the remedy that fits the name: `register_type_prefix()`, given the
package's prefix with its trailing dot (`"mypkg."`), for a dotted name
outside the allowed prefixes whose module is not imported, or whose imported
module has a module-level `__getattr__` (called only under an allowed prefix);
the dotted form for a bare name; why loading failed for a name under an allowed
prefix; the current path for a class that has moved; and a
`LION_CLASS_REGISTRY` entry under its dotted name for a class nested in another
class, which no dotted path reaches. The message shows at most 160 characters
of the name and of the reason, which can repeat the module path; the error it
is chained from carries the whole name. A bare `item_type` name is refused, a
single name counts as a list holding it, and a union counts as each of its
members.

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
