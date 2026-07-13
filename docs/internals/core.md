# Internals Reference

Invariants, protocol contracts, and design rationale for lionagi's core
packages that don't belong inline as long-form comments. Organized by module
path. Inline comments stay short; the full contract lives here.

## `operations/`

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
