# Internals Reference

Invariants, protocol contracts, and design rationale for lionagi's core
packages that don't belong inline as long-form comments. Organized by module
path. Inline comments stay short; the full contract lives here.

## `operations/`

**`flow.py`** — `run_dag()` returns `{completed_operations, operation_results,
final_context, skipped_operations}` always; with `reactive=True` also
`spawned_operations` (successful-spawn count), `escalated_operations` (emitter
ids), and `dropped_spawns` (rejected spawn/inject attempts as `{reason,
assignee, emitter_id, ...}`; reasons: builder_error, null_child, cycle,
max_spawn_exceeded, duplicate). `spawn_branch_setup`, when given, runs after
each reactively-spawned node's branch is cloned (reactive mode only) — the
seam `cli/orchestrate/flow.py` uses to retarget a CLI-backed chat_model's
writable workspace to the spawn's own artifact dir (the clone otherwise
inherits the emitter's `repo` kwarg). `on_op_complete` (reactive mode only)
runs synchronously at the tail of every node's execution — the only
race-free point for a caller's `inject()` against the task group's
convergence; `cli/orchestrate/flow.py`'s team-round wakeup logic is wired here.

**`lndl_middle/lndl_middle.py`** — LNDL seam Middle (ADR-0024 §1-2): advances
a branch one LNDL round per inner chat call, looping internally up to a round
budget (default 3). Opt-in via `branch.operate(instruction=..., middle=lndl_middle)`;
nothing changes for callers who don't pass it. `_classify_round` returns
`(outcome, pending_action_calls, assembled_dict)` — `pending` is every lact
for `Continue`, only OUT{}-reachable lacts for `Success`; `assembled` is set
only on `Success`. `lndl_middle/__init__.py` is a new opt-in public symbol
(unlike the internal `communicate`/`run`/`act` submodule dispatch paths).

**`operate/step.py`** — `Step.request_operative` / `respond_operative`:
identically-constructed Operatives may share one request/response model
**type** (a process-wide cache); instances and their state stay per-call.
Never mutate a returned model class. `LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0`
restores per-call classes (disables sharing). See also `models/_build_model.py`
and `adapters/spec_adapters/pydantic_field.py` below — same cache, different layer.

**Graph-entrypoint conformance suite** (`tests/operations/test_graph_entrypoint_conformance.py`) —
pins every graph-shaped production surface in the shipped package to the
`Session.flow`/streaming-kernel execution authority. A manifest classifies
every graph-shaped function the suite can find: whether it delegates to
`Session.flow` (directly or through an adapter), reaches the sanctioned
streaming kernel, is itself the kernel, or is a pure builder/alias that never
executes anything. The suite statically scans the real source tree for
qualified `.flow`/`.flow_stream`/`.run_dag` calls, bare calls to a
locally-imported kernel function, and executor/graph-builder construction
sites, and fails (with file:line and reason) on anything not in the
manifest — so a new graph entrypoint that isn't registered breaks the build
instead of silently growing a second executor.

The executor-construction scan recognizes a direct
`DependencyAwareExecutor(...)`/`ReactiveExecutor(...)` call, an
`from ... import X as Y` alias, a one-level-deep bare assignment alias, and a
literal `getattr(<flow module>, "DependencyAwareExecutor")` lookup (only when
the receiver statically denotes `lionagi.operations.flow`). Import provenance
is tracked per lexical scope rather than as one flat module timeline: each
function/lambda scope starts from a copy of its enclosing scope's provenance,
first masks every name any local import, assignment, `for`/`async for`
target, match-capture, or augmented assignment binds anywhere in the body
(Python function scope isn't statement-ordered, so a name bound only inside
an `if`/`try`/`for`/`match` is still a lexical local for the whole function),
discards any parameter-shadowed name, then replays its own body's
*unconditional* binding events on top of that masked copy. A binding nested
inside `if`/`try`/`except`/`for`/`while`/`with`/`match`-case is conditional:
it may only discard provenance (never establish or restore it), since a
conditional import might not execute and trusting it risks a false positive
either way. Class bodies get their own add-only environment
(`_SinkVisitor.visit_ClassDef`). The scanner does not perform general
data-flow analysis — resolution through a factory return value, a
non-literal string argument, a multi-hop alias, or a binding inside a
comprehension/walrus is untracked and is a known residual imprecision.

`global`/`nonlocal` declarations are handled separately from ordinary
masking, since they resolve against different target scopes: `global`
overlays provenance from a pristine module-scope snapshot (skipping every
intermediate function scope, including one whose own parameter shadows the
name), while `nonlocal` resolves against the nearest enclosing function
scope, which the ordinary lexical inheritance chain already reproduces. A
`global`/`nonlocal` statement nested inside a class body does not count as a
declaration of the surrounding function — a class body is its own namespace
for this purpose. The scanner's governing invariant is zero false negatives:
a missed executor-construction site is a coverage hole, while a spurious one
only costs a review. Where closing a remaining false positive would require
reasoning about whether a declared name's own binder form actually executes,
that reasoning is deliberately not attempted — the scanner keeps the
(possibly stale) inherited/overlaid provenance and reports a site. This is a
documented conservative over-approximation, not a bug.

Registering a manifest row is necessary but not sufficient: a row naming an
`expected_target` must also name a `delegation_test` (the exact pytest node
id of the test that asserts the delegation — call count, argument identity,
or a mocked target reached), and a row with `persistence="required"` must
name a `persistence_evidence` node id backed by a real StateDB write. Both
are validated against real source, not just checked for non-emptiness, so a
stale or nonexistent reference fails the suite. Known limitation: resolving
a `delegation_test` id to a real test function does not check what that
test's body actually asserts — a row can cite a real, passing test that
exercises an entirely different code path than the one it's cited for. A
weaker structural companion check (does the cited test's source at least
mention a token from `expected_target`) catches the obvious case but is not
a substitute for reading the cited test.

## `session/`

**`signal.py`** — Signal types and per-node lifecycle projection for the
reactive bus (ADR-0033), `schema_version=1`. Payload fields per signal kind
(`RunStart`, `RunEnd`, `RunFailed`, `NodeSpawned`, `NodeQueued`, `NodeStarted`,
`NodeCompleted`, `NodeFailed`, `NodeAwaitingApproval`, `NodeEscalated`,
`NodePaused`, `GateDenied`, `MessageAdded`, `DispatchSignal` (ADR-0059)) are
enumerated in the module. Version policy: `schema_version` bumps only on
breaking field removal/rename; adding nullable fields is non-breaking.

- `RunEnd.total_cost_usd` is `None` (unknown) unless a provider actually
  reports a dollar cost — providers that don't (bare API endpoints) must
  never be recorded as free (`0.0`). Same for `_collect_branch_usage` /
  `_collect_multi_branch_usage`: cost accumulation checks *presence*, not
  truthiness (`x or y` would silently drop an explicit `0.0`).
  `_collect_multi_branch_usage` deliberately excludes `duration_ms` — wall-clock
  across parallel legs isn't simply summable.
- `DispatchSignal` (ADR-0059): one stable envelope (`to_dict(mode="json")`)
  shared by every dispatch kind, so the transport template never churns per-kind.
- `NodeEscalated.route` is `"higher_tier"` (retry), `"give_up"` (terminal), or
  `"notify"` (soft help signal — informational only, node's own lifecycle
  unaffected). Classification rule: a soft ("fyi") help signal must not get
  pinned into the terminal "escalated" lane — only a "blocked" urgency (default,
  matching historical give_up/higher_tier behavior) or an unaccompanied signal
  (no request attached) is treated as escalated.

**`observer.py`** — `_PAYLOAD_BYTE_CAP` bounds the persisted `payload` JSON
column in `session_signals`, not the SSE frame: the SSE generator wraps each
row in an envelope (`data: ...\n\n` + row metadata) adding ~176 bytes of
overhead, so frames can exceed the cap by that margin. Callers needing a hard
frame cap must reserve envelope overhead before calling
`_sanitize_signal_payload`. Truncation strategy in `_sanitize_signal_payload`:
measure the *final* serialized form (not the intermediate `safe_json` string —
re-serializing after wrapping in a truncation-marker dict can be up to 2x
larger due to JSON escaping); if over cap, build a truncation-marker dict
with a data slice that shrinks iteratively until the whole re-serialized dict
fits. `SessionObserver.authorize` routes through the shared `GateResult`
adapter (`lionagi.agent.gate`) so the session gate's fail-closed-on-exception
behavior matches `PermissionPolicy` and the built-in coding guards (ADR-0086).

**`session.py`** — Every new graph-execution surface must delegate through
`Session.flow` or the streaming flow kernel, and include conformance coverage.
`Session.memory` is read-only: an explicitly supplied backend, or a
lazily-created shared `InMemoryStore` on first access; the only way to give a
`Session` its own store is the `memory=` constructor parameter.

**`exchange.py`** — `Exchange.run` does not reset `_stop` on entry: a
`stop()` issued before the coroutine's first turn must make `run()` return
immediately rather than clearing the signal and looping forever. Construct a
fresh `Exchange` for a new run instead of reusing a stopped one.

## `lndl/`

LNDL (Lion Notation Definition Language) — structured-output tag format
mixing natural reasoning with structured data. Core modules (`lexer`,
`parser`, `ast`, `assembler`, `extract`, `normalize`, `types`, `errors`,
`prompt`, `diagnostics`, `round_outcome`) have no external deps beyond
lionagi + pydantic.

**`assembler.py`** — turns parsed `Program` (lvars, lacts, out_block) + a
target Pydantic type into a dict for `target.model_validate()`. Supports
scalar, nested-model, `list[scalar]`, `list[Model]` (field-repeat detection
groups aliases into instances), and `dict[str, V]` target shapes.
`_coerce_str_to_list` strict priority: JSON array → Python list literal →
newline-split → bracketed comma list → else wrap whole string as `[s]`
(deliberately avoids shredding prose by commas). `_alias_value`: an alias not
declared in the current round but present in `action_results` resolves to
that historical result — a later round's `OUT{}` can reference a lact
executed in an earlier round without re-declaring it. `_assemble_grouped_list`
salvages string-literal items (not declared aliases) onto the model's first
string-typed field, as a fallback for `[["raw text"], ["raw text 2"]]` shapes.

**`diagnostics.py`** — opt-in telemetry (`LndlTrace`) for
`branch.operate(lndl=True, trace=trace)` / `ReActStream`; `trace=None`
(default) means zero overhead. Three classification layers answer different
questions: **syntax** (`classify_chunk`: `clean`/`malformed`/`no_out` — did
the model write valid LNDL?), **outcome** (`LndlRoundRecord.outcome`, mirrors
the `RoundOutcome` ADT — what did the framework decide?), **result**
(`classify_result`: `ok`/`str`/`dict`/`empty` — what did the caller get?).
`extract_lndl_chunks(messages, since)`: pass `since=len(branch.messages)`
before a call, then call again after to isolate chunks from that call only.

**`_parse_function_call.py`** — parses `<lact>` bodies into
`{operation, service?, arguments}`. When a service prefix is present
(`svc.tool(...)`), `qualified_name` returns `"svc.tool"` — the name used for
tool-registry lookup so namespaced tools resolve correctly.

**`normalize.py`** — auto-fixes common model-invented LNDL syntax drift
(models trained on XML/HTML/JSON) before the parser runs, ported from
`krons.lndl.fuzzy`. `_fix_missing_gt` is conservative: only fires when the
opening tag has a function-call paren AND the closing tag is present.
`normalize_lndl_text` transforms: curly-brace tags → angle-bracket tags; XML
attributes stripped; missing `>` before body inserted (when body has a
parenthesized call); `Note.` namespace casing → `note.` (the note namespace
and `OUT{}` `note.x` refs are matched case-sensitively downstream, so
model-invented capitalization must be normalized here).

**`parser.py`** — `_parse_out_list` returns `list[str]` for flat refs,
`list[list[str]]` for nested-bracket groups. `_resolve_alias_to_spec`
resolution priority (most-specific first): (1) declared field on a
`Model.field` form → field name, (2) declared model on a `Model.field` form →
model name, (3) two-token hint on a `<l_ hint alias>` form → the hint;
`None` when the alias has no spec context (single-token raw form).

**`round_outcome.py`** — `RoundOutcome` ADT: a multi-round LNDL run is a
state machine, each round produces an outcome, the outer loop matches on it
(replaces ad-hoc parse-fail/validate-fail/missing-out branches). Ported from
`krons.agent.operations.round_outcome`. `Continue`: no `OUT{}` this round;
lacts that ran are already persisted as tool messages before the next round
starts. `Retry`: `OUT{}` produced but parse/resolve/validate failed — `error`
feeds back to the model next round; scratchpad and chat history from prior
rounds remain intact.

**`ast.py`** — `RLvar.extra_id` / `Lact.extra_id`: records the leading token
of a two-token raw form (`<lvar hint alias>` / `<lact hint alias>`) so the
OUT-shortcut path (`parser._resolve_alias_to_spec`) can resolve `alias` back
to the implied spec name `hint`. `None` for the single-token form.

**`types.py`** — `_coerce_result`: a legitimately-`None` result for an
`Optional` scalar must pass through untouched (coercing would corrupt it —
`scalar(None)` yields the literal `"None"` for `str`, raises for `int`/`float`).
Boolean coercion uses `validate_boolean`, not `bool()` — `bool('false') == True`
in Python; `validate_boolean` maps `'false'`/`'0'`/`'no'` → `False`.

## `libs/`

**`path_safety.py`** — `is_protected_name`: matches protected basenames
**case-insensitively**, because default macOS/Windows volumes are
case-insensitive filesystems — a case-sensitive check can be bypassed with
`.ENV` resolving to the same file as `.env`. Shared primitive for both
`resolve_workspace_path` and the deny-only hook floor. `resolve_workspace_path`
checks: expanduser, symlink detection pre-resolve, containment, denied names;
raises `PermissionError` on violation. Validation is check-time only (TOCTOU):
a concurrent filesystem mutation between check and later I/O (e.g. swapping a
file for a symlink) is out of scope — callers needing a stronger guarantee
must do final I/O through a root-anchored, no-follow file descriptor.

## `casts/`

**`emission.py`** — `EscalationRequest.urgency` (`"fyi"` | `"blocked"`) is
the single authoritative field for escalation hardness; `"fyi"` is soft (work
continues, informational), `"blocked"` is hard (work cannot continue).
`blocking` is a read-only back-compat alias for `urgency == "blocked"` — a
legacy `blocking=` constructor kwarg is still accepted and mapped onto
`urgency` for one release of grace, then removed.

**`pattern.py`** — Roles/modes are a **closed** built-in set, one inline
module per pattern, each exposing a single `ROLE`/`MODE`. Not user-definable;
users extend via packs (`casts/pack.py`), never by adding role/mode modules.
`Role.artifact_defaults` (ADR-0064 shape:
`{"expected": [{"id", "path", "required", ...}]}`) is a gate role's declared
output contract, merged per-leg into the flow's `artifact_contract` at
DAG-build time (`flow.py _build_dag`); `None` means no artifact claim.

## `adapters/`

**`spec_adapters/pydantic_field.py`** — `_model_type_cache`: model classes
(unlike Operative instances) hold no request/response state and are shared
across identical constructions. Sharing contract: callers must not mutate a
returned model class (mutation is visible to every later identical
construction); `LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0` disables sharing. LRU
bounds strong references to dynamically-created base classes and generated
models.

## `models/`

**`_build_model.py`** — `build_model_type` is deliberately **uncached**:
`FieldInfo` and validator inputs can be mutable. The Operative-construction
cache one layer up (`adapters/spec_adapters/pydantic_field.py`) caches only
immutable schemas, keyed by the actual base-class **object identity** plus
frozen build options — class-object identity (not a structural hash) keeps
distinct same-named/same-shaped classes separate; a prior structural-hash
implementation cross-wired their generated models.

## `ln/`

**`concurrency/utils.py`** — SIGTERM/SIGINT handling around `run_async`.
`_SIGTERM_RECEIVED` is a process-wide latch set by the SIGTERM handler the
moment the signal arrives; `SigtermInterrupt` is raised only after the worker
thread has joined, so persist paths consult the latch to distinguish an
external SIGTERM from an internal runtime cancel. `consume_sigterm_received`
reads-and-clears the latch so one external SIGTERM labels exactly one run
(without consuming, it would mislabel every later run/test's cancellation).
`SigtermInterrupt` deliberately subclasses `BaseException`, not
`KeyboardInterrupt` (the SIGINT/user convention) — so a bare
`except Exception:` can't silently swallow it. `run_async` installs temporary
SIGINT/SIGTERM handlers on the main thread that cancel the inner asyncio task
via `call_soon_threadsafe`: SIGINT's default raises `KeyboardInterrupt` in
`join()`, orphaning the child thread and leaving session rows stuck
"running"; SIGTERM's default is immediate process termination with no
unwind, so without a handler an external SIGTERM (timeout supervisor,
process-group kill) is silent. In `_runner`, if a signal latched before the
future existed, cancel immediately rather than running to completion (the
only path for SIGTERM, whose default disposition isn't callable as fallback).

**`_proc.py`** — `_safe_pgid`: `pid` must be `int > 1` — `pid==0` is our own
process group, `pid==1` is init/session leader on CI (would `SIGKILL` the
harness itself; also catches `MagicMock.pid==1`). `killpg` is POSIX-only;
returning `None` makes callers fall back to `proc.terminate()`/`kill()`.

**`_ssrf.py`** — `_CANONICAL_LOCAL_HOSTS`: only the exact strings
`"localhost"`, `"127.0.0.1"`, `"::1"` are accepted for `allow_local=True`.
Alternate encodings (`2130706433`, `0x7f000001`, `127.1`,
`::ffff:127.0.0.1`, etc.) are intentionally excluded to prevent
DNS-rebinding bypass.

## `engines/`

**`engine.py`** — `EngineRun.cancel_active`: waits up to
`engine.cancel_timeout_s`; tasks that don't settle in that window are
abandoned with a logged warning (lifetime guarantee preserved either way).
`wait_quiescence`: blocks until all spawned tasks settle, re-raises
non-cancellation/non-budget failures — `EngineBudgetError` is a benign
"expansion stopped" signal (discretionary work declined, not a crash) and is
swallowed like `CancelledError`. `EngineResult` (`Engine.run()` return type):
a `str` subclass carrying structured outcome; `str(result)` and `result.text`
are the same synthesized text; `.run` is a live `EngineRun` handle — don't
retain it past reading the result, it keeps the whole `Session` (and its
branches) alive. `Engine._degrade_export`: cancels in-flight spawned tasks,
then runs `_partial_export` shielded + timeout-bounded; shared by the
deadline and root-budget degrade paths in `run()`; returns `_UNSET` on
failure/timeout (logged, not raised) — an external cancel during the shielded
phase still propagates. `Engine.run`'s `(EngineBudgetError, ExceptionGroup)`
handler: a root-level `make_agent()` budget-out routes to partial-export
instead of crashing; masking guard — a non-budget leaf anywhere in the group
(including nested groups) must not be laundered into a partial, so it
re-raises instead.

**`coding.py`** — `CodingChainEvent` `eid` prefixes (`W`/`P`/`T`/`V`/`K`) are
namespaced against hypothesis engine's (`F`/`Q`/`E`/`H`/`X`/`R`/`C`/`A`) so
IDs never collide across engines; refs link a stage to its upstream stage so
the export is a walkable chain. `CodingEngine._fix_loop`: re-prompts the
implementer on failure and re-tests, bounded by `max_fix_rounds`; mechanical
rounds (fixed by auto-repair alone) skip the judge gate, substantive rounds
go through it; `fast_test_cmd` (if configured) gates intermediate rounds,
`test_cmd` is always the final ground-truth leg. `_capture_diff` candidate
set: union of the initial workspace delta (covers emission-failure rewrites)
and every file any `ChangeProposed` claimed to touch, evaluated at verify
time so fix-round additions are included; paths normalized to
workspace-relative POSIX before intersecting (`files_touched` often carries
absolute paths per the coding tool schema, while `git ls-files --others`
returns repo-relative); paths escaping the workspace are dropped.

## `protocols/`

**`context_providers.py`** — `ContextProviderRegistry`: providers register
in render order; when combined output exceeds `budget`, lowest-priority
providers are dropped first. A raising provider is warned + skipped, never
blocks the turn. `gather_writeback` (post-turn hook): providers with an
optional `writeback(branch, action_responses)` method get a chance to persist
from the turn's action responses, under the same raise-warns-skips containment.

**`messages/message.py`** — `Message._render_cached`: rendering cache keyed
by content identity + revision, served only when the stored content **is**
the current content object — an `id()`-based key alone could cross-wire two
content objects with non-overlapping lifetimes that happen to reuse the same
address.

**`generic/processor.py`** — `Processor.process`: dequeues and processes
events up to available capacity. Denied events are either terminal
(`SKIPPED`) or deferred (re-enqueued); the cycle stops when all queued events
have been deferred, to avoid busy-spin.

**`messages/instruction.py`** — `_DATA_IMAGE_RE`: only a bitmap MIME
allowlist is accepted for inline image data URIs, payload must be non-empty
base64; active-content types (HTML, JS, SVG — can carry scripts) and other
`data:` schemes are rejected by design. `InstructionContent.__init__` builds
the structure from the tracked copy, not the caller's dict — a structure
holding the caller's alias would let external mutation change rendering
without advancing the content revision. `__getstate__` excludes the private
structure (may cache a dynamically-created request-model class that can't be
serialized); `__setstate__` restores through `__setattr__` (so mutable render
inputs are re-wrapped) then rebuilds the private structure from the restored
`response_format` — keeping the copied structure would leave the renderer
reading a dict detached from the restored public field. `to_dict` includes
`response_format` only when it's a plain dict (JSON-serializable); excluded
for type/`BaseModel` references, which can't round-trip through
`to_dict` → `from_dict`.

**`action/manager.py`** — `_validate_prebuilt_mcp_tool_admission`: a
schema/description that's just the auto-generated `**kwargs` wrapper carries
no remote-server info, so it's treated as absent metadata — strong identities
fail closed instead of laundering through their own synthetic schema, and
ordinary names aren't falsely denied by the wrapper's generic docstring.
`register_mcp_server` (both the `tool_names` path and the discovered-tools
path): validates the complete list before creating/registering **any** tool —
a denial anywhere must leave the registry exactly as it was, never partially
populated with whichever names/tools happened to validate first.
`load_mcp_config`: defaults to servers declared in the config file just
loaded, not the full pool — `MCPConnectionPool` accumulates configs
process-globally across loads, so enumerating the pool here would silently
re-register every server from previously loaded, unrelated configs.
`invoke()`: every tool routed through this method (function tools, `Tool`
objects, MCP-discovered tools) passes through the same tool-pre/tool-post
hook layer; constructing `FunctionCalling` directly bypasses it entirely
(documented, tested limit). Pre hooks run before the tool's own
`preprocessor` chain and may rewrite arguments; a denial raises before the
tool is invoked. Rewritten arguments are revalidated against the tool's
declared request model inside `FunctionCalling._invoke()`, after the
spec-level chain has also had a chance to mutate them — the re-validation
step means a rewrite can never bypass the tool's declared schema, and a
tool with no `request_options` never had schema enforcement to bypass. Post
hooks run after invocation completes (success or failure) and are advisory
only — they observe final arguments/result/error and cannot change any of
them. `_resolve_plugin_tool` (ADR-0088 D3): on a registry miss, asks the
plugin registry whether a trusted, enabled, version-compatible plugin
declares the tool. Import of `lionagi.plugins` is deferred until an actual
miss (see `tests/test_import_laziness.py`); resolution and trust are
re-checked fresh on every call, never cached onto `self.registry`, so a
plugin disabled or edited mid-session stops being reachable immediately.
Raises `PluginToolCollisionError` unmodified when two enabled plugins
declare the same tool name (ADR-0088 D6) — a hard error, not a miss.

**`action/tool_hooks.py`** — Hook contract at the `ActionManager.invoke`
chokepoint: the mutation-capable layer outermost around every tool call,
deliberately separate from `lionagi.hooks.bus.HookBus` (summary-payload
audit plane) and the per-`Tool` `preprocessor`/`postprocessor` chain wired
by `lionagi.agent.spec.HooksMixin` (spec-level security/user chain, runs
innermost). A pre hook returns `None` (allow, unchanged), a plain `dict`
(allow, replace arguments), or a `ToolPreDecision` (`"allow"` optionally
with `updated_input`, `"deny"`, `"ask"` — fails closed, no interactive
approval surface exists — or any other value, which fails closed with a
diagnostic). A post hook receives the tool name, final arguments, result
(`None` on failure), and error (`None` on success); post hooks are advisory
only since the action already happened, matching the harness convention
that `block` on a post-invocation event cannot un-run the call.

## `orchestration/`

**`patterns.py`** — `role_node_builder` returns a node_builder closure
routing `SpawnRequest`s to role branches. `decorate_instruction`, when given,
receives the request and the node's freshly allocated `spawn_id` and must
return the full instruction text the child runs with. `start` seeds the
closure's spawn-id sequence past ordinals already issued in a prior generation
(e.g. a resume reconstructing completed spawns from a checkpoint) — without it, a
fresh sequence restarting at 1 would reissue an id already used by a restored
node, colliding with any live spawn this generation on the same `spawn_id`.

`_next_spawn_seq = itertools.count(start)` is closure-scoped and is the
**only** correct source of a spawned node's stable id: it must be allocated
at construction time because that's the sole point that sees the
`SpawnRequest` before the child Operation is queued. Minting the id at
completion time (the prior implementation) let an unrelated node "steal"
spawn-1 depending on which sibling finished first.

Inside `role_node_builder.build`: the operation allowlist check is
defense-in-depth even though `SpawnRequest.operation` is already a typed
`Literal` — custom operation names registered on a session branch must
**never** be reachable via model-emitted spawn requests; fails closed on
anything outside the documented allowlist. Spawn-id allocation happens only
after assignee validation succeeds, so an unknown assignee never consumes a
sequence number — ids handed to real children stay dense modulo only genuine
post-build rejections (cycle/cap) downstream. Metadata stamping (`spawn_id`,
`reference_id`) lets post-run callers (artifact contracts, DAG metadata)
attribute a reactively spawned node back to its assignee role even after the
executor overwrites `branch_id` with a per-spawn branch clone; `spawn_id`
survives the clone (it's metadata, not branch state) and is the stable
correlation key every downstream surface must use — `reference_id` mirrors it
for the executor's own display path (`DependencyAwareExecutor._run_tracked`
reads `metadata["reference_id"]` for its progress/log line).

**`prompts.py`** — Planning section (`DECOMPOSE_INSTRUCTION`): the
orchestrator decomposes the task into `TaskAssignment`s (the casts
coordination emission); `assignee` names a role from the roster, `task` is
the concrete objective. There is no bespoke plan model — a list of
`TaskAssignment`s (with `depends_on`) *is* the plan (and the DAG).

## protocols/ (additional entries)

### Pile concurrency contract

`Pile` has a two-lock concurrency contract. The sync API (`@synchronized`
methods, subscripting, iteration snapshots) is thread-safe under `_lock`. The
async API (`a`-prefixed `@async_synchronized` methods) is task-safe under
`_async_lock` AND excludes sync callers in other threads: the async wrapper
holds both locks (async lock first, then a non-blocking spin on the threading
lock) for the duration of the call. Iteration (`__iter__` / `__aiter__`)
captures a point-in-time snapshot of the *order* under the lock; item lookup
stays live, so removing a not-yet-visited item raises `KeyError` at that step
(fail-loud) instead of silently yielding a stale object. `keys` / `values` /
`items` return fully materialized snapshots. A `Pile` is iterable but is NOT
itself an iterator, matching `list` and `dict` — traversal position lives in
the object `iter(pile)` returns, so concurrent readers each get their own
cursor and a copied `Pile` never inherits a partially consumed one.

The exclusion boundary is CROSS-THREAD, not cross-task. On the event loop's
own thread, a sync call made by a different task while an async operation is
mid-await re-enters the RLock (thread-owned) and proceeds — same-thread
callers are cooperative by design; enforcing task-level exclusion for sync
calls on the loop thread would deadlock the loop. Async-side critical regions
(`async with pile`, `adump`, `adapt_to_async`, `__aiter__`) all use the
ordered both-lock protocol, so they exclude sync callers running in other
threads.

### Message render-cache safety

`Message._render_cached` caches a rendering keyed by content identity plus a
tracked revision counter, bypassing the cache entirely when content holds a
value the revision tracker cannot observe in-place mutation of.
`_content_is_render_safe` memoizes the JSON-safety verdict per (content
identity, tracked revision) so a warm JSON-safe content is walked at most once
per revision instead of on every render — but only the *safe* verdict is ever
cached. An untracked-mutable object can mutate without bumping the tracked
revision (that's the whole reason the render cache must bypass for it), so a
cached *unsafe* verdict has no revision to reliably invalidate on; recomputing
it every call is the only way to keep it honest.

`_has_untracked_mutable` walks a value looking for anything besides JSON-safe
primitives and list/dict/tuple/frozenset nesting of them — `type` objects are
exempt, since content only ever reads their class-level schema, never live
instance state. It's iterative (explicit stack, not recursion) so deeply
nested-but-safe input cannot raise `RecursionError`, and it fails safe
(returns `True` without raising) for a self-referential (cyclic) container or
once traversal exceeds a bounded depth, since neither can be proven safe to
cache.

### FunctionCalling schema revalidation

`FunctionCalling._invoke` re-validates arguments after any pre-stage rewrite
(hook layer or preprocessor) so a rewrite can never bypass the tool's declared
schema. Keys outside the schema (e.g. an audit marker a preprocessor adds) are
not covered by that validation — pydantic's default `extra="ignore"` would
otherwise drop them from `model_dump`, so they are carried through untouched
rather than silently discarded.

"Outside the schema" is judged against the model's declared input names
(field names + aliases), not against the *serialized* validated dump: a
declared field that is aliased and left unset (e.g. `Field(default=0,
validation_alias="a_alias")`) is absent from `model_dump(exclude_unset=True)`
even though it is a real, schema-covered field. Classifying it as "extra"
would let a preprocessor set it by name and forward the raw, unvalidated
value straight to the callable — a schema bypass.

### Pile row serialization

`Pile.dump`/`adump` write JSONL and CSV without a pandas dependency, via
`_serialize_records` in `generic/pile.py`. JSONL is one compact JSON object
per line, newline-terminated so `mode="a"` appends valid JSONL (matching the
old `DataFrame.to_json(orient="records", lines=True)` output). CSV writes a
header of every key seen across all rows, in first-appearance order; rows
missing a key render that cell empty, as pandas did. Row values come from
`to_dict(mode="json")` — lionagi's canonical orjson encoding (ISO datetimes,
shortest round-trippable floats) — which is value-equal and round-trippable
via `Element.from_dict`, but not byte-identical to the old pandas output for
datetime and high-precision-float fields (pandas rendered epoch-ms datetimes
and double-precision floats). `parquet` stays on the pandas `to_df` path
since it needs a columnar engine; `_serialize_records` doesn't support it.

### Progression membership sync

`Progression.order` is a public, directly-mutable deque — tests, third-party
callers, and `Pile` internals all mutate it in place (`p.order.append(x)`,
`p.order[0] = x`, `p.order.popleft()`), not just through `Progression`'s own
methods. `Progression` keeps an O(1) membership set (`_members`) in sync with
that deque so `__contains__` doesn't have to scan.

A naive staleness check comparing `len(order)` across calls misses any
length-preserving external write — `order[0] = x`, or a `popleft()` paired
with an `append()` — because the length is unchanged even though the
contents are not. `generic/progression.py` solves this two ways:

- `order` is always wrapped in a `_MembersDeque`, a `deque` subclass that
  updates the bound `_members` set eagerly inside every mutating method
  (`append`, `insert`, `__setitem__`, `__delitem__`, `extend`, ...), using a
  duplicate-aware discard rule: an id is only dropped from the set once no
  occurrence of it remains in the deque. `__imul__` with `n <= 0` clears the
  set (every element is dropped); `n >= 1` only duplicates existing ids, so
  the set of unique members is unchanged and needs no update. `rotate` and
  `reverse` permute existing entries without changing which ids are present,
  so neither touches the set.
- `Progression._ensure_synced()` (called before any read of, or incremental
  update to, `_members`/`_order_len`) additionally detects wholesale
  replacement of `order` — `p.order = deque(...)`, or a plain-deque copy
  produced by pydantic re-validation — and a wrapper that is bound to a
  *different* `Progression` instance's `_members` set. Ownership is checked
  by identity (`order._members_ref is self._members`), not just type and
  length, so a foreign or unbound wrapper of matching length can't silently
  pass as synced. Either case triggers `_rebuild_members()`, which rebuilds
  `_members` from scratch and rebinds the wrapper.

Public behavior — membership correctness after *any* direct `order`
mutation, not just length-changing ones — must not be narrowed to a
length-only check; that was tried and is exactly the case this design
covers.

### Graph adjacency cache

`Graph.get_predecessors_cached` / `get_successors_cached` memoize plain-tuple
adjacency lookups per node id until a mutator invalidates them
(`add_edge`/`remove_edge`/`remove_node`/`replace_node`/`splice_after`). The
existence check only runs on a cache miss — a cached entry is proof the node
was valid when memoized, and `remove_node()` always clears its own cache
entry in the same call that removes it from `internal_nodes`, so a stale hit
past removal cannot occur.

The result is a tuple, not a list: the memoized entry is the exact object
handed back on every cache hit, so a mutable list would let one caller's
in-place edit (append/clear/sort) corrupt what every other concurrent reader
of the `Graph` sees — tuples make that aliasing hazard impossible rather than
relying on callers to treat the result as read-only. This is also zero-copy
on a cache hit. Cache snapshots are never modified after publication, so a
hit only dereferences the current snapshot; misses take the graph lock and
publish a copied snapshot after building the result, and mutators evict
entries by the same copy-and-replace strategy under that lock, which prevents
a concurrent miss from publishing stale adjacency.

## service/

### EndpointRegistry match

`EndpointRegistry.match` finds and instantiates the best matching endpoint. A
*registered* provider is never rejected: if `provider` names a canonical
provider or provider-alias that some entry already claimed (via
`register()`/`_claim_provider_identity`), a request for an endpoint that
provider doesn't happen to expose falls through to the generic construction
the same as an explicit opt-in would — the provider identity is not in
question, only the specific endpoint name, so there is nothing to reject.
`ProviderNotFoundError` is reserved for a `provider` string matching no
registered provider or alias at all: the generic OpenAI-compatible fallback
then only builds when `openai_compatible=True` is passed explicitly, or
(deprecated migration path, warns) when a `base_url` kwarg is given — the
same signal a caller already needs to point the fallback at a real custom
host. Anything else raises `ProviderNotFoundError` naming the requested
provider and every provider currently registered.

### EndpointRegistry plugin revalidation

`_revalidate_plugin_entry` keeps plugin entries available only while their
declared target remains trusted. `PluginRegistry.activate_target()` rescans
and rehashes every installed plugin on each call, not just this one — too
expensive to pay on every `match()` hit against an endpoint that already
activated cleanly. It only re-runs when the `PluginRegistry` snapshot
generation has strictly advanced (a `reset()` happened), when this plugin's
manifest, any declared path, or user settings source changed (see
`_plugin_entry_stat`), or — when that stat signature still matches — when the
entry's own content digest (see `_plugin_entry_digest`) no longer matches;
otherwise it reuses the prior result.

The stat signature alone is not a portable content-change guarantee:
`os.utime()` restores a spoofed mtime after an edit, and on platforms where
`st_ctime_ns` is not a metadata-change token (Windows CPython documents it as
file *creation* time, which a content write or `os.utime()` never advances),
a same-length in-place edit can leave the whole stat tuple looking unchanged.
size and inode are free extra signal from the same `stat()` call and catch
same-second same-mtime same-ctime edits and delete+recreate respectively, but
a same-length in-place edit defeats size too. The content digest is only
computed on that stat-stable path — the files plugins declare are small, so
paying for the read there is cheap — and closes that hole on every platform:
it always changes when the manifest or any declared capability file's bytes
do. Whenever the stat tuple compares unchanged, revalidation confirms with
the content digest before trusting it — that confirmation, not the stat
tuple, is what makes the fast path safe to serve from cache.

### MCPConnectionPool trust model

`MCPConnectionPool` caches MCP clients keyed by transport identity AND the
effective-security fingerprint (`security` if given, else the process-global
policy set via `set_security_config()`, else the fail-closed default). This
is the fix for cross-caller trust inheritance: a trusted call and a later
omitted-policy call to the identical server can never resolve to the same
cache entry, because their effective security differs and so does their key.
A cache hit can then only ever return a client whose key already encodes the
caller's own effective security — an omitted-policy call misses and goes
through `_create_client`'s fail-closed validation instead of silently reusing
another caller's connection.

`get_client(security=None)` means the caller made no trust decision, and it
never recovers a policy some other caller authorized for the same identity.
Recovering a remembered policy is only reachable through
`_get_reconnect_client`, which is capability-gated: it requires the exact
`_MCPRecoveryCapability` instance minted for a proxy at authorization time
(`MCPConnectionPool._mint_capability`) — not a config, not a server name, not
an equal-content capability. A direct call, a stored bound method, a subclass
alias, or a proxy rehydrated from persisted `mcp_config` all lack a live
reference to that instance and fail closed with no recovery. This is
deliberately not part of `get_client`'s public contract: `create_mcp_tool`'s
stored callable is the only caller, re-entering a transport its own closure
already holds the capability for.

### HookedEvent stream teardown contract

`HookedEvent._stream` runs the pre-hook, yields chunks from `_core_stream()`,
then runs the post-hook. The post-hook runs however the stream ends —
exhaustion, a source error, an early-stopping consumer, or cancellation — and
`stream_terminal_state` says which of those it was. Whatever ended the stream
still propagates unchanged; post-hook failures are logged, never raised.

Guaranteed: the caller receives the very same exception object the stream
ended with, not a replacement, no matter how the teardown fails. Deliberately
not guaranteed: a cancellation actually delivered to the consuming task while
teardown is running is not swallowed — it reaches the caller in place of
whatever the stream ended with, because a task that was cancelled from
outside must not come back believing it was not. On a stream that was itself
ended by cancellation, the source is re-raised instead, since the consumer
stays cancelled either way. Off asyncio the two kinds of cancellation cannot
be told apart at all and both propagate.

A consumer that stops early is responsible for closing the stream, with
`aclose()` or `contextlib.aclosing`. A bare `break` does not close the
generator it was iterating, so teardown is deferred to whenever the
interpreter finalizes the abandoned generator — it still runs, and still
reports the closed state, but not at a point the consumer picked, and during
interpreter or loop shutdown the grace bound (`POST_STREAM_TEARDOWN_GRACE`)
can cut it short.

`_invoke_post_stream_hook_isolated` runs the post-hook in a child task that
captures whatever ends it and returns it instead of raising, so that task can
never end cancelled because of something the hook did — a cancellation
surfacing at the await afterward then has exactly one possible origin
(delivery to this task) and is honored: the hook's task is cancelled, given
`POST_STREAM_HOOK_STOP_GRACE` seconds to stop, and the cancellation
propagates. A hook that will not stop within that grace is abandoned:
reported at WARNING and left running, held so it is not destroyed mid-await
while the program is still going.
