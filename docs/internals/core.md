# Internals Reference

Invariants, protocol contracts, and design rationale for lionagi's core packages that don't belong inline as long-form comments. Organized by module path.

## `operations/`

- **`flow.py`** — `run_dag()` returns `{completed_operations, operation_results, final_context, skipped_operations}` always; with `reactive=True` also `spawned_operations` (successful-spawn count), `escalated_operations` (emitter ids), `dropped_spawns` (rejected spawn/inject attempts as `{reason, assignee, emitter_id, ...}`; reasons: `builder_error`, `null_child`, `cycle`, `max_spawn_exceeded`, `duplicate`). `spawn_branch_setup` (reactive only) runs after each reactively-spawned node's branch is cloned. `on_op_complete` (reactive only) runs synchronously at the tail of every node's execution — the only race-free point for a caller's `inject()` against the task group's convergence.
- **`lndl_middle/lndl_middle.py`** — LNDL seam Middle (ADR-0024 §1-2): advances a branch one LNDL round per inner chat call, looping internally up to a round budget (default 3). Opt-in via `branch.operate(instruction=..., middle=lndl_middle)`. `_classify_round` returns `(outcome, pending_action_calls, assembled_dict)` — `pending` is every lact for `Continue`, only `OUT{}`-reachable lacts for `Success`; `assembled` set only on `Success`.
- **`operate/step.py`** — `Step.request_operative`/`respond_operative`: identically-constructed Operatives share one request/response model **type** (process-wide cache); instances/state stay per-call. Never mutate a returned model class. `LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0` disables sharing (same cache as `adapters/spec_adapters/pydantic_field.py` below).

## `session/`

- **`signal.py`** — Reactive-bus signal types (ADR-0033), `schema_version=1` (bumps only on breaking field removal/rename; adding nullable fields is non-breaking). Kinds: `RunStart`, `RunEnd`, `RunFailed`, `NodeSpawned`, `NodeQueued`, `NodeStarted`, `NodeCompleted`, `NodeFailed`, `NodeAwaitingApproval`, `NodeEscalated`, `NodePaused`, `GateDenied`, `MessageAdded`, `DispatchSignal` (ADR-0059). `RunEnd.total_cost_usd` is `None` (unknown) unless a provider reports a dollar cost — never `0.0` for one that doesn't; cost accumulation checks presence, not truthiness. `_collect_multi_branch_usage` excludes `duration_ms` (parallel wall-clock isn't summable). `NodeEscalated.route`: `"higher_tier"` (retry), `"give_up"` (terminal), `"notify"` (soft, informational only) — only `"blocked"` urgency (default) or a signal with no request attached is classified escalated.
- **`observer.py`** — `_PAYLOAD_BYTE_CAP` bounds the persisted `payload` column, not the SSE frame (envelope adds ~176 bytes). Truncation in `_sanitize_signal_payload` measures the *final* serialized form (re-serializing after wrapping can be ~2x larger), shrinking a data slice iteratively until it fits. `SessionObserver.authorize` routes through the shared `GateResult` adapter so fail-closed-on-exception behavior matches `PermissionPolicy` and built-in coding guards (ADR-0086).
- **`session.py`** — Every new graph-execution surface must delegate through `Session.flow` or the streaming flow kernel, with conformance coverage. `Session.memory` is read-only; `memory=` constructor param is the only way to give a `Session` its own store.
- **`exchange.py`** — `Exchange.run` does not reset `_stop` on entry: a `stop()` issued before the first turn makes `run()` return immediately. Construct a fresh `Exchange` for a new run instead of reusing a stopped one.

## `lndl/`

LNDL (Lion Notation Definition Language) — structured-output tag format mixing natural reasoning with structured data. No external deps beyond lionagi + pydantic.

- **`assembler.py`** — builds a dict from parsed `Program` + target Pydantic type for `model_validate()`. Supports scalar, nested-model, `list[scalar]`, `list[Model]` (field-repeat detection), `dict[str, V]`. `_coerce_str_to_list` priority: JSON array → Python list literal → newline-split → bracketed comma list → wrap whole string as `[s]`. `_alias_value`: an alias not declared this round but present in `action_results` resolves to that historical result.
- **`diagnostics.py`** — opt-in telemetry (`LndlTrace`); `trace=None` (default) is zero overhead. Three layers: syntax (`classify_chunk`: `clean`/`malformed`/`no_out`), outcome (`LndlRoundRecord.outcome`, mirrors `RoundOutcome`), result (`classify_result`: `ok`/`str`/`dict`/`empty`). `extract_lndl_chunks(messages, since=len(branch.messages))` isolates chunks from one call.
- **`_parse_function_call.py`** — parses `<lact>` bodies into `{operation, service?, arguments}`; a `svc.tool(...)` prefix yields `qualified_name = "svc.tool"`.
- **`normalize.py`** — auto-fixes model-invented LNDL syntax drift before parsing: curly-brace → angle-bracket tags, XML attributes stripped, missing `>` inserted before a parenthesized-call body, `Note.` → `note.` (matched case-sensitively downstream).
- **`parser.py`** — `_parse_out_list` returns `list[str]` (flat) or `list[list[str]]` (nested groups). `_resolve_alias_to_spec` priority: declared field name → declared model name → two-token hint → `None`.
- **`round_outcome.py`** — `RoundOutcome` ADT drives the multi-round loop. `Continue`: no `OUT{}` this round, ran lacts already persisted as tool messages. `Retry`: `OUT{}` produced but parse/resolve/validate failed — error feeds back next round; prior scratchpad/history intact.
- **`ast.py`** — `RLvar.extra_id`/`Lact.extra_id` record the leading token of a two-token raw form so the OUT-shortcut path can resolve alias → hint. `None` for single-token form.
- **`types.py`** — `_coerce_result`: a legitimate `None` for an `Optional` scalar passes through untouched. Boolean coercion uses `validate_boolean`, not `bool()` (`bool('false') == True` in Python).

## `libs/`

**`path_safety.py`** — `is_protected_name` matches **case-insensitively** (default macOS/Windows volumes are case-insensitive; `.ENV` can resolve to `.env`). Shared by `resolve_workspace_path` and the deny-only hook floor. `resolve_workspace_path` checks expanduser, pre-resolve symlink detection, containment, denied names; raises `PermissionError`. Check-time only (TOCTOU) — callers needing a stronger guarantee must do final I/O through a root-anchored, no-follow file descriptor.

## `casts/`

- **`emission.py`** — `EscalationRequest.urgency` (`"fyi"` or `"blocked"`) is the sole authoritative escalation-hardness field. `blocking` is a read-only back-compat alias for `urgency == "blocked"`.
- **`pattern.py`** — Roles/modes are a **closed** built-in set; not user-definable (users extend via packs, `casts/pack.py`). `Role.artifact_defaults` (ADR-0064: `{"expected": [{"id", "path", "required", ...}]}`) merges per-leg into the flow's `artifact_contract` at DAG-build time (`flow.py _build_dag`); `None` means no artifact claim.

## `adapters/`

**`spec_adapters/pydantic_field.py`** — `_model_type_cache`: model classes (unlike Operative instances) hold no request/response state and are shared across identical constructions. Callers must not mutate a returned model class. `LIONAGI_OPERATIVE_MODEL_CACHE_SIZE=0` disables sharing.

## `models/`

**`_build_model.py`** — `build_model_type` is deliberately **uncached** (`FieldInfo`/validator inputs can be mutable). The cache one layer up (`adapters/spec_adapters/pydantic_field.py`) keys by base-class **object identity** (not structural hash) plus frozen build options — a prior structural-hash implementation cross-wired distinct same-shaped classes.

## `ln/`

- **`concurrency/utils.py`** — SIGTERM/SIGINT around `run_async`. `_SIGTERM_RECEIVED` is a process-wide latch set the moment SIGTERM arrives; `SigtermInterrupt` raises only after the worker thread joins. `consume_sigterm_received` reads-and-clears so one external SIGTERM labels exactly one run. `SigtermInterrupt` subclasses `BaseException`, not `KeyboardInterrupt`, so a bare `except Exception:` can't swallow it. `run_async` installs temporary handlers that cancel the inner task via `call_soon_threadsafe` (SIGINT's default orphans the child thread; SIGTERM's default is silent immediate termination). In `_runner`, a signal latched before the future existed cancels immediately.
- **`_proc.py`** — `_safe_pgid`: `pid` must be `int > 1` (`0` = own process group, `1` = init/session leader — would `SIGKILL` the harness). `killpg` is POSIX-only; `None` return falls back to `proc.terminate()`/`kill()`.
- **`_ssrf.py`** — `_CANONICAL_LOCAL_HOSTS`: only exact strings `"localhost"`, `"127.0.0.1"`, `"::1"` accepted for `allow_local=True`. Alternate encodings (`2130706433`, `0x7f000001`, `127.1`, `::ffff:127.0.0.1`) are intentionally excluded (DNS-rebinding bypass).

## `engines/`

- **`engine.py`** — `EngineRun.cancel_active` waits up to `engine.cancel_timeout_s`; unsettled tasks are abandoned with a logged warning. `wait_quiescence` blocks until all spawned tasks settle, re-raises non-cancellation/non-budget failures (`EngineBudgetError` is swallowed like `CancelledError`). `EngineResult` is a `str` subclass; `.run` is a live `EngineRun` handle — don't retain it past reading the result (keeps the whole `Session` alive). `Engine._degrade_export` cancels in-flight tasks then runs `_partial_export` shielded + timeout-bounded; returns `_UNSET` on failure/timeout (logged, not raised). A non-budget leaf anywhere in an `ExceptionGroup` (including nested) re-raises instead of being laundered into a partial.
- **`coding.py`** — `CodingChainEvent` `eid` prefixes (`W`/`P`/`T`/`V`/`K`) are namespaced against the hypothesis engine's (`F`/`Q`/`E`/`H`/`X`/`R`/`C`/`A`). `CodingEngine._fix_loop` re-prompts and re-tests, bounded by `max_fix_rounds`; mechanical rounds (auto-repair only) skip the judge gate. `fast_test_cmd` gates intermediate rounds; `test_cmd` is always the final ground-truth leg. `_capture_diff` candidate set: initial workspace delta ∪ every file any `ChangeProposed` claimed, evaluated at verify time; paths normalized to workspace-relative POSIX before intersecting; paths escaping the workspace are dropped.

## `protocols/`

- **`context_providers.py`** — `ContextProviderRegistry`: providers register in render order; lowest-priority dropped first when combined output exceeds `budget`. A raising provider is warned + skipped, never blocks the turn. `gather_writeback` gives providers with an optional `writeback(branch, action_responses)` a chance to persist post-turn, under the same raise-warns-skips containment.
- **`messages/message.py`** — `Message._render_cached`: keyed by content identity + revision, served only when stored content **is** the current object (`id()` alone could cross-wire two objects with non-overlapping lifetimes reusing the same address).
- **`generic/processor.py`** — `Processor.process` dequeues/processes up to available capacity. Denied events are terminal (`SKIPPED`) or deferred (re-enqueued); the cycle stops when all queued events have deferred, to avoid busy-spin.
- **`messages/instruction.py`** — `_DATA_IMAGE_RE`: bitmap MIME allowlist only for inline image data URIs, non-empty base64 required; active-content types (HTML/JS/SVG) and other `data:` schemes rejected. `to_dict` includes `response_format` only when it's a plain JSON-serializable dict (excluded for type/`BaseModel` references, which can't round-trip).
- **`action/manager.py`** — `_validate_prebuilt_mcp_tool_admission`: an auto-generated `**kwargs`-wrapper schema carries no remote-server info, so it's treated as absent metadata (strong identities fail closed). `register_mcp_server` validates the complete list before registering **any** tool — a denial leaves the registry unchanged, never partially populated. `load_mcp_config` defaults to servers in the just-loaded config file, not the full process-global pool. `invoke()`: every tool routed through this method shares the same tool-pre/tool-post hook layer (constructing `FunctionCalling` directly bypasses it — documented, tested limit). Pre hooks run before the tool's own `preprocessor` chain and may rewrite arguments; rewritten arguments are revalidated against the tool's declared request model inside `FunctionCalling._invoke()`. Post hooks are advisory only. `_resolve_plugin_tool` (ADR-0088 D3): on a registry miss, asks the plugin registry for a trusted/enabled/compatible plugin declaring the tool; resolution/trust re-checked fresh every call, never cached. Raises `PluginToolCollisionError` unmodified when two enabled plugins declare the same tool name (ADR-0088 D6).
- **`action/tool_hooks.py`** — Hook contract at the `ActionManager.invoke` chokepoint: outermost mutation-capable layer around every tool call, separate from `lionagi.hooks.bus.HookBus` (audit plane) and the per-`Tool` `preprocessor`/`postprocessor` chain (innermost, spec-level). A pre hook returns `None` (allow unchanged), a `dict` (allow, replace arguments), or a `ToolPreDecision` (`"allow"` optionally with `updated_input`, `"deny"`, `"ask"` — fails closed, no interactive approval surface exists). A post hook receives tool name, final arguments, result (`None` on failure), error (`None` on success); advisory only.

## `orchestration/`

- **`patterns.py`** — `role_node_builder` routes `SpawnRequest`s to role branches. `decorate_instruction`, when given, returns the full instruction text the child runs with. `start` seeds the spawn-id sequence past ordinals already issued in a prior generation (resume from checkpoint), preventing a fresh sequence from colliding with a restored node. `_next_spawn_seq` is closure-scoped and the **only** correct source of a spawned node's stable id — allocated at construction time, not completion time (minting at completion let an unrelated sibling "steal" spawn-1). The operation allowlist check inside `build` is defense-in-depth even though `SpawnRequest.operation` is a typed `Literal`; spawn-id allocation happens only after assignee validation succeeds. `spawn_id` survives branch-clone (it's metadata, not branch state) and is the stable correlation key every downstream surface must use; `reference_id` mirrors it for the executor's display path.
- **`prompts.py`** — Planning section (`DECOMPOSE_INSTRUCTION`): the orchestrator decomposes the task into `TaskAssignment`s; `assignee` names a roster role, `task` is the concrete objective. A list of `TaskAssignment`s (with `depends_on`) *is* the plan (and the DAG) — no bespoke plan model.

## protocols/ (additional entries)

<a id="pile-concurrency-contract"></a>

### Pile concurrency contract

`Pile` has a two-lock concurrency contract. The sync API (`@synchronized` methods, subscripting, iteration snapshots) is thread-safe under `_lock`. The async API (`a`-prefixed `@async_synchronized` methods) is task-safe under `_async_lock` AND excludes sync callers in other threads: the async wrapper holds both locks (async lock first, then a non-blocking spin on the threading lock) for the call's duration. Iteration (`__iter__`/`__aiter__`) captures a point-in-time snapshot of *order* under the lock; item lookup stays live, so removing a not-yet-visited item raises `KeyError` at that step (fail-loud) rather than yielding a stale object. `keys`/`values`/`items` return fully materialized snapshots. A `Pile` is iterable but NOT itself an iterator (matching `list`/`dict`) — traversal position lives in the object `iter(pile)` returns, so each reader gets its own cursor.

The exclusion boundary is CROSS-THREAD, not cross-task. On the event loop's own thread, a sync call by a different task while an async op is mid-await re-enters the RLock and proceeds (same-thread callers are cooperative by design; task-level exclusion for sync calls on the loop thread would deadlock it). Async-side critical regions (`async with pile`, `adump`, `adapt_to_async`, `__aiter__`) all use the ordered both-lock protocol.

<a id="message-render-cache-safety"></a>

### Message render-cache safety

`Message._render_cached` caches a rendering keyed by content identity plus a tracked revision counter, bypassing the cache when content holds a value whose in-place mutation the revision tracker can't observe. `_content_is_render_safe` memoizes the JSON-safety verdict per (content identity, tracked revision) — but only the *safe* verdict is cached. An untracked-mutable object can mutate without bumping the revision, so a cached *unsafe* verdict has no revision to invalidate on; it's recomputed every call.

`_has_untracked_mutable` walks a value for anything besides JSON-safe primitives and list/dict/tuple/frozenset nesting (`type` objects are exempt — content only reads their class-level schema). Iterative (explicit stack, not recursion), so deep-but-safe input can't raise `RecursionError`; fails safe (`True`, no raise) for a cyclic container or once traversal exceeds a bounded depth.

<a id="functioncalling-schema-revalidation"></a>

### FunctionCalling schema revalidation

`FunctionCalling._invoke` re-validates arguments after any pre-stage rewrite (hook layer or preprocessor) so a rewrite can never bypass the tool's declared schema. Keys outside the schema (e.g. an audit marker) are carried through untouched rather than dropped by pydantic's default `extra="ignore"`.

"Outside the schema" is judged against the model's declared input names (fields + aliases), not the *serialized* validated dump: a declared, aliased field left unset (e.g. `Field(default=0, validation_alias="a_alias")`) is absent from `model_dump(exclude_unset=True)` even though it's a real, schema-covered field. Classifying it as "extra" would let a preprocessor set it by name and forward the raw, unvalidated value straight to the callable.

<a id="graph-adjacency-cache"></a>

### Graph adjacency cache

`Graph.get_predecessors_cached`/`get_successors_cached` memoize plain-tuple adjacency lookups per node id until a mutator invalidates them (`add_edge`/`remove_edge`/`remove_node`/`replace_node`/`splice_after`). The existence check only runs on a cache miss — `remove_node()` always clears its own cache entry in the same call that removes the node, so a stale hit past removal cannot occur.

The result is a tuple, not a list: the memoized entry is the exact object handed back on every cache hit, so a mutable list would let one caller's in-place edit corrupt what every other reader sees. Zero-copy on a cache hit; misses take the graph lock and publish a copied snapshot, and mutators evict entries by the same copy-and-replace strategy under that lock.

## `service/`

<a id="endpointregistry-match"></a>

### EndpointRegistry match

`EndpointRegistry.match` finds and instantiates the best matching endpoint. A *registered* provider is never rejected: if `provider` names a canonical provider/alias some entry already claimed, a request for an endpoint that provider doesn't expose falls through to generic construction. `ProviderNotFoundError` is reserved for a `provider` string matching no registered provider/alias at all — the generic OpenAI-compatible fallback then only builds when `openai_compatible=True` is passed explicitly, or (deprecated, warns) a `base_url` kwarg is given.

<a id="endpointregistry-plugin-revalidation"></a>

### EndpointRegistry plugin revalidation

`_revalidate_plugin_entry` keeps plugin entries available only while their declared target remains trusted. `PluginRegistry.activate_target()` rescans and rehashes every installed plugin on each call — too expensive to pay on every `match()` hit. It only re-runs when the `PluginRegistry` snapshot generation has advanced (a `reset()` happened), when this plugin's manifest or declared paths changed (`_plugin_entry_stat`), or when the stat signature matches but the content digest (`_plugin_entry_digest`) doesn't.

The stat signature alone isn't a portable content-change guarantee: `os.utime()` can restore a spoofed mtime, and on platforms where `st_ctime_ns` isn't a metadata-change token (Windows: file *creation* time), a same-length in-place edit can leave the stat tuple unchanged. Size and inode catch same-second/same-mtime edits and delete+recreate respectively, but not a same-length in-place edit. The content digest is computed only on that stat-stable path (files plugins declare are small) and closes the hole on every platform.

<a id="mcpconnectionpool-trust-model"></a>

### MCPConnectionPool trust model

`MCPConnectionPool` caches MCP clients keyed by transport identity AND the effective-security fingerprint (`security` if given, else the process-global policy via `set_security_config()`, else fail-closed default). A trusted call and a later omitted-policy call to the identical server can never resolve to the same cache entry — a cache hit can only return a client whose key already encodes the caller's own effective security.

`get_client(security=None)` means the caller made no trust decision and never recovers a policy some other caller authorized. Recovering a remembered policy is reachable only through `_get_reconnect_client`, capability-gated on the exact `_MCPRecoveryCapability` instance minted for a proxy at authorization time (`MCPConnectionPool._mint_capability`) — not a config, server name, or equal-content capability. Not part of `get_client`'s public contract: `create_mcp_tool`'s stored callable is the only caller.

<a id="hookedevent-stream-teardown-contract"></a>

### HookedEvent stream teardown contract

`HookedEvent._stream` runs the pre-hook, yields chunks from `_core_stream()`, then runs the post-hook — however the stream ends (exhaustion, source error, early-stopping consumer, cancellation); `stream_terminal_state` says which. Whatever ended the stream still propagates unchanged; post-hook failures are logged, never raised.

Guaranteed: the caller receives the same exception object the stream ended with. Deliberately not guaranteed: a cancellation delivered to the consuming task while teardown is running is not swallowed — it reaches the caller in place of whatever the stream ended with. On a stream ended by cancellation, the source is re-raised instead. Off asyncio the two kinds of cancellation can't be told apart and both propagate.

A consumer that stops early must close the stream (`aclose()` or `contextlib.aclosing`) — a bare `break` defers teardown to interpreter finalization, which still runs but not at a chosen point, and can be cut short by `POST_STREAM_TEARDOWN_GRACE` during shutdown.

`_invoke_post_stream_hook_isolated` runs the post-hook in a child task that captures whatever ends it rather than raising, so a cancellation surfacing at the await afterward has exactly one origin and is honored: the hook's task is cancelled, given `POST_STREAM_HOOK_STOP_GRACE` seconds to stop, and the cancellation propagates. A hook that won't stop in time is abandoned: reported at WARNING and left running.
