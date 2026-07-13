# Runtime internals reference

Terse, per-module reference for invariants, protocol contracts, and non-obvious design
rationale that used to live as long-form docstrings/comments in the runtime source
(`lionagi/state/`, `lionagi/service/`, `lionagi/providers/`, `lionagi/tools/`,
`lionagi/agent/`, `lionagi/testing/`, `lionagi/dispatch/`, `lionagi/hooks/`). The source
now carries a 1-2 line pointer; this file carries the substance. Organized by module path.

## lionagi/state/

### `state/engine.py`

- `make_readonly_engine()` — read-only `AsyncEngine` over an **existing** SQLite file, opened
  through SQLite's own URI read-only mode (`mode=ro`), not through `make_engine()`. This
  matters because: (1) no schema/PRAGMA write ever reaches the file — the OS-level open is
  read-only and SQLite raises on any write attempt; (2) none of `make_engine()`'s mutating
  connect-time PRAGMAs (`journal_mode`, `synchronous`, `wal_autocheckpoint`) are applied here
  on purpose, since they persist into the database file itself; (3) only `busy_timeout`
  (session-scoped, never persisted) and `query_only` (belt-and-suspenders — SQLite itself
  rejects any write statement) are set. SQLite only — a genuinely read-only Postgres
  connection should use a read-only DB role instead; there is no equivalent "connect without
  side effects" mode to fake at this layer for Postgres.

### `state/health.py`

`classify_session_health()` — pure function; `process_alive` is tri-state: `True` = observed
alive, `False` = confirmed dead (positive evidence — a recorded pid that is no longer
running), `None` = unknown (no recorded pid and no process match, the normal case for
externally-driven sessions mirrored into the DB). Decision order matters:

1. Terminal sessions (`completed`/`failed`/`timed_out`/`aborted`/`cancelled`): done means done
   unless stale locks were left behind (→ `ZOMBIE`). `has_artifacts` alone is not zombie
   evidence — artifacts are a *good* outcome; stale locks are the operational signal.
2. Orphan check runs first among the non-terminal branches: a session advertised but never
   producing a single message AND no artifacts on disk crashed before doing anything;
   transitioning it to `failed` is harmless, deleting it is also safe.
3. Confirmed dead (`process_alive is False`) outranks the activity guard below it — the
   process is gone no matter how fresh the last message is.
4. Unknown liveness (no matchable pid) trusts recent messages more than process visibility,
   because externally-driven sessions (mirrored into the DB from another process) never expose a
   matchable pid — an unmatched process only means dead once activity has also gone quiet
   past the kind-aware threshold.

### `state/transitions.py`

Guarded compare-and-swap state transitions (ADR-0059) — a minimal
counterpart to the entity-agnostic `transition()` API proposed in ADR-0058. Carries the
same request/result shape and reason-code discipline so ADR-0058 can absorb it as a
refactor, not a migration. Scoped to `entity_type='dispatch'` (`dispatch_outbox`) and
`entity_type='schedule_run'` (`schedule_runs`) only — not a general TransitionStore. The
guarded read/CAS/vocabulary/write algorithm itself lives in `lionagi.state.lifecycle` (shared
with `StateDB.update_status()`); this module keeps its own narrower entity-type boundary, its
`guard`/`patch` column allowlist, and the legacy `TransitionResult` return shape.

- `_ENTITY_TABLES` — `"schedule_run"` is ADR-0071 D2's generalized task-application entity
  (`schedule_runs` table, `schedule_id` now nullable), registered here so ALL status movement
  on it routes through this guarded CAS store rather than a second, parallel implementation.
- `_GUARD_PATCH_COLUMNS` — guard/patch column names are interpolated directly into SQL text
  (values stay bound params) inside the lifecycle service. Production call sites pass
  literal dicts, but this module is a generic surface, so a per-entity allowlist closes
  the latent injection surface instead of trusting the caller's dict keys outright.
- `transition()` — `UPDATE ... WHERE id=:id AND status=:from`, writing the row status and an
  atomic `status_transitions` append inside one transaction. A mismatched current state
  reports a conflict rather than raising or silently overwriting (the CAS guard). An
  undeclared status move (per the shared policy registry's edge graph) raises `ValueError` —
  this surface has no override mechanism, unlike `StateDB.update_status()`. `guard` adds extra
  `column = :expected` equality constraints to the WHERE clause beyond `status` — required
  whenever a transition can be a same-state no-op (e.g. `delivering -> delivering` recovery
  claims), where the status guard alone would match trivially and let two concurrent callers
  both believe they won the claim; callers pass the value they read *before* the transition as
  the expected guard value, and only the caller whose guard value still matches at UPDATE time
  wins. `patch` adds extra `column = :value` assignments to the SET clause, applied atomically
  with the status change and the `status_transitions` append — for callers (e.g. an
  operator-forced retry resetting attempt counters) that would otherwise need a second,
  non-atomic write.
- `"rejected"` is unreachable through this surface: `run_legacy_transition()` passes
  `raise_on_undeclared_edge=True`, so an undeclared edge (terminal or not) raises `ValueError`
  above rather than resolving to `"rejected"`, and `TransitionRequest` carries no override
  field to trigger the override path either.

### `state/claude_mirror.py`

Mirrors Claude Code session transcripts (`~/.claude/projects/*.jsonl`) into StateDB. A Claude
Code session is just another writer to `state.db`: each JSONL event maps to one or more
lionagi messages, written under a session/branch/progression with deterministic ids so
re-reading the same transcript never duplicates rows. The studio SSE reader polls the same
tables, so mirrored sessions stream live in the dashboard and the VS Code extension with no
studio-side change.

- `mirror_session()` — re-calling with already-seen events is a no-op: message ids are
  deterministic (upsert), and progression appends dedupe. Creates the session/branch on first
  call with a rich row (project, model, agent_name) so it groups correctly in the runs
  explorer. Live/idle transitions are owned by `reconcile_session_status`, not this writer.
  Scaffold writes (progressions → session → branch) are `INSERT OR IGNORE` and re-run every
  call in dependency order, so if an earlier pass died mid-scaffold (e.g. the branch write
  raised after the session row committed) the next pass repairs the partial state instead of
  skipping scaffolding just because the session row now exists. The project-backfill branch
  (`elif project and not existing.get("project")`) exists because `INSERT OR IGNORE` never
  updates an existing row — without it, an already-seen "(no project)" session stays that way
  forever; the backfill write does not disturb the liveness clock.
- `reconcile_session_status()` — a mirror session's `completed` means dormant, not terminal:
  when the transcript resumes, the next reconcile brings it back to `running`. Liveness is
  judged by `last_message_at` (the timestamp of the newest mirrored message) so an idle
  session converges to `completed` before the reaper can mark it failed, and an active one
  shows `running` (a live spinner in studio and the VS Code extension). **It must NOT read
  `updated_at`**: the status write below bumps `updated_at`, so keying liveness off it would
  let a just-marked `completed` session read as fresh again on the next pass and oscillate
  back to `running`. ADR-0035's integrity floor treats every session terminal status (not just
  `completed`) as terminal on the sessions table for orchestrated runs, so reactivating a
  mirror session out of any of them goes through the sanctioned override path — a real,
  deliberate, well-understood write (not a repair), attributed to the recorded automated
  override identity, landing in `admin_events` like any other override. A mirror session
  that is idle and already sitting on a non-`completed` terminal status (e.g. independently
  marked `failed` or `cancelled`) is left alone rather than rewritten to `completed` — only a
  live transcript resuming can justify pulling it back to `running`.
- `link_session_lineage()` — a continued conversation (after compaction, `--resume`, or a
  fresh window picking up an earlier thread) starts a new transcript whose first message
  points, via `parentUuid`, at the last message of the session it continues. When the mirror
  resolves that pointer to a different session it stores a `lineage` link on the child's
  `node_metadata` so studio and the VS Code extension can show provenance and walk the chain
  back. Written without moving the liveness clock; idempotent (re-linking rewrites the same
  value).

### `state/completion_evidence.py`

Completion-trust gate: cheap, local, no-network evidence that a run produced something. A
session can exit its loop cleanly with nothing to show for it — no commits, no artifacts, no
diff — and stamping that `completed` makes the status meaningless as evidence of delivered
work. This module gives the teardown path a lightweight git-based
signal to fall back on when no artifact contract caught the emptiness: is HEAD ahead of the
base ref, or does the working tree carry uncommitted changes? A probe that actually runs and
fails (transient error, timeout, git hiccup) must never be read as "ran and found nothing" —
that would silently turn a git error into a false `completed_empty` on real work. Only a probe
that *succeeds* is allowed to report an absence of evidence; any decisive failure bails the
whole check out as unchecked (`checked=False`) so the caller keeps trusting `completed`.

### `state/lifecycle/`

Unified lifecycle transition service: one guarded read-check-write-history
algorithm shared by every managed entity type's status transitions,
replacing per-surface transition logic. Public surface: immutable
command/result records (`models`), the policy registry (`policy`), the
SQLAlchemy transaction implementation (`service`), and the
StateDB/legacy-transition compatibility mapping (`adapters`).

- `service.py` — `transition()` is the public entry point, enforcing the
  policy's declared-edge graph: an undeclared move is a "rejected" outcome
  with a rejection audit row, not a raise, so callers get the same outcome
  shape for terminal-exit and undeclared-edge refusals alike; a valid
  override is the audited escape hatch for either. `_transition()` accepts
  additional keyword-only parameters outside the public `TransitionCommand`
  shape, used only by `lionagi.state.lifecycle.adapters` to keep the two
  legacy compatibility wrappers behaviorally identical to their
  pre-existing selves: `extra_guard` (an arbitrary per-column WHERE-clause
  guard, e.g. dispatch's `delivering -> delivering` crash-recovery claim
  guarding on `attempt`, which the public typed command has no generic
  field for), `enforce_edges` (`StateDB.update_status()` never enforced a
  declared-edge graph, only terminal-exit-requires-override and vocabulary
  membership, so it calls with `enforce_edges=False`; the legacy
  `lionagi.state.transitions.transition()` did enforce one, for
  schedule_run, so it calls with `enforce_edges=True`), and
  `raise_on_unguarded_conflict` (an unguarded zero-row UPDATE is a storage
  anomaly `StateDB.update_status()` has always raised `RuntimeError` on,
  from inside the transaction so a same-transaction rollback still occurs).
  A self-edge's `required_guard_fields` must be satisfied by either
  `extra_guard` covering those exact columns or a generic
  `expected_version` guard (`updated_at`, which the write always bumps) —
  either is an equally strong optimistic-concurrency guard against two
  callers holding the same snapshot both winning a crash-recovery claim;
  missing both is a caller-contract violation, so it raises rather than
  returning a conflict/rejected outcome. Commit (via the `_tx()` context
  exit) happens strictly before the terminal-callback registry push, so a
  handler can never delay, observe-before-commit, or roll back the write.
  `_write()`'s `write_reason_columns` mirrors legacy per-surface behavior:
  `StateDB.update_status()` always denormalized the reason onto the
  entity row's own `status_reason_*` columns (the default); the legacy
  `lionagi.state.transitions.transition()` surface never did, and
  `dispatch_outbox` (only reachable through that surface) doesn't even
  have those columns.
- `callbacks.py` — a `RunTerminalEnvelope` is constructed by the lifecycle
  service only after a guarded transition commits and lands on a terminal
  status for an execution entity (session, invocation, schedule_run, play);
  the registry then pushes it to every matching handler concurrently under
  one shared deadline (best-effort — a handler failure, timeout, or
  cancellation is logged and swallowed, never affecting the already-committed
  transition or delaying the caller past budget). Within `schema_version ==
  1` the envelope's guaranteed fields never change name/type/semantics/
  requiredness; new optional fields may be added without a version bump.
  `register()`'s `override` marks a per-run override: for any envelope it
  matches, only override registrations fire, replacing any non-override
  match for that run's scope only — other envelopes the override doesn't
  match are unaffected. In `emit()`'s handler fan-out, a plain
  (non-async-def) handler is offloaded to a worker thread rather than run
  directly (which would block the event loop and starve the shared
  `move_on_after` deadline); `abandon_on_cancel=True` is required (not the
  default) so the deadline can still cut the await short without waiting for
  the thread to finish on its own — the thread itself is only abandoned, not
  killed, and may keep running in the background after return.
- `notify_settings.py` — resolves `notify.on_terminal` (a string
  compatibility form, or a mapping `{enabled, adapter: {kind: exec|python,
  ...}, filter: {kinds, ids}}`) into a handler installable on a
  `TerminalCallbackRegistry`. Precedence is per-run override > project
  settings > global settings > disabled; absent key and explicit `enabled:
  false` are both disabled. No configuration shape ever reaches a shell: a
  plain command string is POSIX-word-split (`shlex.split`) and launched via
  `asyncio.create_subprocess_exec`, never `create_subprocess_shell`; a
  string that fails to split or needs shell features (pipes, redirection,
  conjunction, variable expansion) warns with a migration diagnostic and
  resolves to disabled, as does any resolution producing an empty argv. A
  resolution error never fails or delays the run it would have described. In
  the exec adapter, on cancellation the registry's own shared deadline races
  the call's identical `wait_for` timeout (the outer one started first and
  typically wins); either way the child (launched with
  `start_new_session=True`, its own process group) must be reaped or it
  orphans a live subprocess, so cleanup runs inside a shielded `CancelScope`
  since the enclosing scope is already cancelled.
- `deliveries.py` — acknowledgment is durable state written only by a named
  reconciliation consumer, never by the in-process push path
  (`TerminalCallbackRegistry` stays fire-and-forget and records nothing
  here). The reconciliation query is a read-only anti-join: terminal
  transitions on execution entities with no delivery row yet for the
  requesting consumer, with no age filter on either side — a late-committing
  older row, or an event from a long-offline consumer, stays unacknowledged
  indefinitely (this module never expires an unacked event on its own).
- `schema_meta.py`'s `session_controls` table — apply/stamp ordering is
  verb-classed: `pause`/`resume` are idempotent (apply, then stamp — safe to
  re-apply on a poller crash); `message` is not (stamp `'applying'`, then
  apply, then finalize — a crash surfaces as an unapplied `'applying'` row
  rather than risking a double injection). `'stop'` is schema-reserved and
  rejected by the current poller as unsupported; no CLI verb emits it yet.

## lionagi/plugins/registry.py

Combines discovery + trust + settings into one snapshot, two-stage lazy like
`EndpointRegistry._ensure_loaded`. Stage 1 (`_ensure_loaded`): manifests are
scanned/parsed the first time any consumer asks, cached for the process.
Stage 2 (`activate_target`): a declared target/module is imported only when
that capability is actually invoked, never as a side effect of discovery or
an unrelated capability firing. Eligibility (compatible + enabled) and trust
are revalidated fresh on *every* call, re-reading `plugin.yaml`, settings,
and every declared file from disk — an already-activated target stops being
handed out the moment the plugin is disabled or a declared file/manifest
changes, not just refusing brand-new activations. The specific target file
is read exactly once: that read's hash (checked against the currently
recorded trust entry) and the bytes that get compiled/exec'd are the same
`read_bytes()` call (`_read_and_verify_target_bytes`), never a hash-then-
reopen sequence that would leave a TOCTOU window for the file to be swapped.
`_exec_bundle_module` compiles the pre-read bytes directly rather than going
through importlib's `spec_from_file_location`/`exec_module` path, which
writes/reads a `__pycache__` `.pyc` validated by second-granularity mtime —
two writes within the same wall-clock second are indistinguishable to it, so
a re-import right after a re-trusted edit could silently execute stale
bytecode. Import results (success or failure) are cached per `(plugin,
target, content hash)`, so re-trusting changed content is a guaranteed cache
miss rather than depending on an earlier call having evicted the old entry.
`_rescan()` re-reads and re-parses `plugin.yaml` itself rather than reusing
the cached `record.manifest` — a stale cached manifest object always
re-derives the same "trusted" hash regardless of what's actually on disk,
since the manifest hash is computed by re-serializing the parsed object, not
by re-reading the file. `_target_resolution_map` builds target ->
(module_path, attr) from the manifest's own typed capability lists using the
same `parse_tool_target` split the hashing path (`discovery
._collect_declared_paths`) uses — two independently written splitting
expressions could disagree on where a path ends and a callable begins; one
shared parser can't. Nothing in this module runs at `import lionagi` time.

## lionagi/service/connections/mcp_wrapper.py

Security-critical admission-control gate for MCP (Model Context Protocol) tool descriptors:
decides whether an externally-supplied MCP tool schema is safe to register, defending
specifically against a generic command/process/script executor (a "bash"-shaped tool)
masquerading as something narrower via an insufficiently-bounded JSON Schema. This is
orthogonal to two other security mechanisms: `MCPSecurityConfig` governs transport
authorization (is this command/URL allowed to connect at all), and `PermissionPolicy` governs
invocation-time authorization keyed by tool name. This admission rule sits before both: it
rejects a caller-shaped generic executor descriptor before the tool is ever admitted into the
registry, regardless of transport settings.

### Identifier/key classification

- `_is_identifier_like_key()` — identifies dynamic-but-benign resource identifiers
  (`service_id`, `resource_path`, `request_id`, matching `*_id`/`*_path`/`*_uri`/`*_url`/
  `*_uuid`/`*_slug`). Excluded from the strong-executor-name "must be affirmatively bounded"
  fallback: a tool with a strong executor name but whose only free-form field is an
  identifier-shaped key is NOT treated as executor-shaped — a fixed-operation tool addressing
  a resource by free-form ID differs legitimately from one taking a free-form command/script,
  even though the identifier field itself is an unbounded string.
- `_EXEC_TAINTED_KEY_TOKENS` — tokens that make an otherwise identifier-shaped key still
  executor-shaped. A key like `executable_path`/`script_path`/`command_path` lexically matches
  the identifier-suffix exemption but its root token names an executor channel — a
  caller-controlled executor target, not a benign locator — so it must NOT be exempted from
  the strong-name fallback.
- `_is_exec_tainted_key()` — companion to the above: true when a key's own `_`-split tokens
  name an executor channel (command, cmd, shell, script, program, binary, executable, argv,
  args), overriding the identifier-suffix exemption.
- `_UNBOUNDED_NON_OBJECT_TYPES` — non-object types (string/number/integer/array) whose
  instances aren't intrinsically finite. A root `type` union including one, without a
  top-level `enum`/`const` pinning the instance, has a branch admitting an arbitrary value,
  bypassing every object-shaped constraint entirely because the instance never has to be an
  object.
- `_ANNOTATION_ONLY_REF_SIBLING_KEYWORDS` / `_has_structural_ref_siblings()` — keywords that,
  as siblings of `$ref`, are annotation-only and never constrain the instance (description,
  title, `$comment`, examples, default, `$defs`, definitions). Per Draft 2020-12, `$ref`
  siblings are NOT discarded — they constrain the same instance and must be evaluated in their
  own right, so a non-annotation sibling forces the sufficiency proof to also evaluate it as
  its own schema node.

### Keyword registry for the sufficiency proof

Answers exactly one question: is this schema document provably closed against an undeclared
value? A per-property "is this value a key channel" discriminator — deciding which positions
deserve scrutiny — is the same defect class as enumerating "dangerous keywords," re-entered
through the traversal axis: omit the conditional applicators (`if`/`then`/`else`/`not`) or
array applicators (`items`/`prefixItems`) and a property carrying one is never visited, so
the allowlist check that would have denied it never runs.
The fix: classify EVERY Draft 2020-12 keyword into EXACTLY ONE of four classes (inert/
bounding/modeled/denied), then walk the ENTIRE document — every property value, composition
branch, `$ref` target, `$defs` entry — UNCONDITIONALLY, checking each node's own keywords
against the registry. There is no longer a discriminator deciding "should this node be
visited"; a keyword not in the registry is UNKNOWN and fails closed unless its value provably
cannot carry a subschema.

- `_INERT_ANNOTATION_KEYWORDS` (`contentSchema` caveat) — `contentSchema` is inert with an
  individually-argued exception, not by default: its value is a mapping describing the DECODED
  content of a string instance, like `contentEncoding`/`contentMediaType` — none assert
  anything about the instance itself, but ONLY while the content-assertion vocabulary stays
  disabled (the default dialect this module validates against). A future dialect enabling that
  vocabulary would require `contentSchema` to leave this class — a maintenance tripwire.
- `_DENIED_APPLICATOR_KEYWORDS` — applicators recognized BY NAME but deliberately NOT modeled
  (patternProperties, propertyNames, unevaluatedProperties, unevaluatedItems,
  dependentSchemas, if, then, else, not, contains, items, prefixItems, `$dynamicRef`,
  `$dynamicAnchor`, `$recursiveRef`, `$recursiveAnchor`): presence anywhere denies the node
  outright, and the proof never recurses beneath one since an ancestor denial already covers
  everything nested. Promoting one to modeled (e.g. bounded-array support via
  items/prefixItems) is a separate, individually-argued change with its own soundness argument
  — never a silent reclassification.
- `_classify_keyword()` — classifies a keyword into inert/bounding/modeled/denied;
  unrecognized names are UNKNOWN. The registry is a CLOSED enumeration, not a spelling
  heuristic — an unseen keyword fails closed rather than being guessed at.

### Object-boundedness proof

- `_property_value_may_be_object_shaped()` — true when a declared property's VALUE could
  itself resolve to an OBJECT instance, requiring the boundedness proof to recurse.
  Deliberately narrow: only answers "does closedness apply here," never "is a keyword modeled"
  — that's answered totally and unconditionally by `_structural_coverage_insufficient`
  regardless of whether this gate recurses, so an omission here (e.g. a value reachable only
  through a DENIED applicator) is harmless. A scalar/array/annotation/free-form-string
  property value returns False on purpose — it remains the walker's territory by key name
  (`service_id: {"type": "string"}` stays admitted).
- `_schema_is_insufficient()` — top-level gate: insufficient if EITHER the object-boundedness
  proof OR the structural-coverage proof fails. The two proofs are deliberately independent
  (type/closedness vs. keyword coverage), combined by OR.
- `_object_boundedness_insufficient()` — recursive, union-aware TYPE-GATE + CLOSEDNESS check,
  orthogonal to `_structural_coverage_insufficient` (never denies on keyword identity, only on
  whether the instance is provably constrained to a closed, finite object shape). Binding
  order of checks (applicator delegation MUST run before omitted-type denial, or a
  legitimately-type-omitting applicator-root would false-deny): (1) budget/depth cap fails
  closed; (2) `type` excludes `"object"` → insufficient; (3) `type` union has a free-form
  alternative with no `const`/`enum` pin → insufficient; (4) APPLICATOR DELEGATION — `$ref`
  (local only, intersect structural siblings), then `oneOf`/`anyOf` (UNION: every branch must
  prove sufficient), then `allOf` (INTERSECTION: one bounded branch suffices), each recursing;
  (5) top-level `const`/`enum` pins the instance → sufficient regardless of type; (6)
  LEAF-OBJECT branch: omitted type (no const/enum) → insufficient (bare non-object never
  reaches object keywords); non-empty `properties` only bounded if CLOSED
  (`additionalProperties: False` or itself enum/const-restricted); empty/absent properties
  still needs `additionalProperties: False`; once the outer object is closed, each declared
  property's own object-shaped value is re-checked by this same predicate recursively, or
  insufficient — non-object-shaped values are left to the walker's key-name policy since any
  denied applicator they carry is caught independently by `_structural_coverage_insufficient`.
  Returns True (fail closed) for external/cyclic/unresolvable/exhausted references or
  compositions.
  - Type-array handling: a Draft 2020-12 type array (e.g. `["object","null"]`) is still an
    object schema if `"object"` is among its types, and properties must still be inspected;
    only excluding `"object"` entirely is insufficient.
  - Type-union weak-link: a type union including `"object"` plus a free-form alternative
    (string/number/integer/array, absent enum/const) is only as bounded as its LEAST-bounded
    branch — an instance satisfying the non-object branch never reaches the object-specific
    keywords checked below.
  - `$ref` sibling re-check: Draft 2020-12 evaluates `$ref` siblings; a closed reference target
    doesn't make the node sufficient if a sibling keyword (evaluated against the same
    instance) reopens it. Pure annotation siblings contribute nothing and must not force an
    otherwise-open target's insufficiency onto an unrelated property.
  - const/enum pin: a top-level const/enum pins the instance to literal value(s), satisfying
    the type-gate regardless of type (or its absence).
  - Closedness default trap: `additionalProperties` defaults to permissive (implicit true); a
    fixed `operation` enum/const does NOT stop an undeclared `command` property from riding
    alongside it unless `additionalProperties` is explicitly `false` (or itself enum/const-
    restricted). This is the concrete attack shape: a schema that looks locked down because
    one property is enum-restricted while every other key name is still wide open.

### Structural-coverage proof

- `_structural_coverage_insufficient()` — total, registry-driven traversal: does any
  schema-bearing position carry a keyword the sufficiency proof does not model? Independent of
  `_object_boundedness_insufficient`'s type-gate/closedness reasoning — applies neither. A
  scalar leaf `{"type": "string"}` is sufficient on its own, preserving a free-form identifier-
  key property (`service_id`) alongside a fixed operation. Every schema-bearing position is
  visited UNCONDITIONALLY — every properties value, additionalProperties schema
  (Mapping-valued), composition branch, resolved local `$ref` target, and `$defs`/definitions
  entry (even unreferenced ones, a deliberate fail-closed choice). Totality argument: every
  position is reached by exactly one of three routes — (i) under a chain of MODELED
  applicators (this function's recursion visits it); (ii) under a DENIED applicator (ancestor
  already returned True before looking inside); or (iii) under an UNKNOWN keyword (checked for
  subschema-shaped value and denied there). No fourth route exists. The unconditional `$defs`
  visit specifically guards against a future reference, or tooling resolving `$defs` by
  convention rather than explicit `$ref`, smuggling an unmodeled keyword through an entry
  never inspected.
- `_property_is_bounded()` — deliberately has no array carve-out: array boundedness needs BOTH
  `prefixItems` members AND the `items` (rest-of-array) schema checked together (see
  `_array_reaches_free_form`); a bounded `items` alone says nothing about an unbounded
  `prefixItems` member alongside it.
- `_item_schema_reaches_free_form_string()` — true when an array's items/prefixItems member
  schema may admit an arbitrary string, i.e. a free-form argv-shaped channel. Item schemas are
  ALWAYS treated conservatively (unlike keyed object properties, which get the
  identifier-suffix exemption): an opaque/unconstrained/malformed item shape is presumed
  free-form. Local `$ref` and allOf/anyOf/oneOf/if/then/else/not composition are resolved/
  recursed so indirection is still caught. Critically, a nested `type: "array"` item is NOT
  automatically bounded — it's only bounded if its own item schema is bounded, otherwise a
  caller can smuggle a free-form channel one array level deeper
  (`args: [["sh", "-c", ...]]`).
- `_array_reaches_free_form()` — true when an array-shaped node admits a free-form element.
  Relies on Draft 2020-12 semantics: `prefixItems` validates only prefix positions; `items`
  validates everything at-or-after. A MISSING `items` keyword defaults to `true`
  (unconstrained rest), so an array whose prefixItems are all enum/const-bounded is STILL
  free-form unless `items` is explicitly present and bounded (or `false`). Every prefixItems
  member is checked too — bounded `items` says nothing about an unbounded prefix member.

### Walker (`_walk_schema`)

The walker is a WHITELIST, not blacklist. Closing specific evasions one at a time (nested
properties, anyOf, `$ref`, additionalProperties, patternProperties; if/then/else, not, items,
prefixItems) leaves every future keyword free to reopen the same bypass class — enumerating
"keywords we deny" loses that arms race by construction. Instead the
walker enumerates keywords it affirmatively understands; any other subschema-shaped key is
unresolvable — future/unsupported keywords (unevaluatedProperties, dependentSchemas, contains,
propertyNames) deny by default for executor-signaling tools instead of admitting by default.

- `_SCALAR_ONLY_SCHEMA_KEYWORDS` — keywords whose value never carries a subschema (or, for
  `$defs`/definitions, only reachable via `$ref` resolution). `contentSchema` included on the
  same footing as its `_INERT_ANNOTATION_KEYWORDS` inclusion — the two caveats must be kept in
  sync if the content-assertion vocabulary assumption ever changes.
- `_UNRESOLVABLE_REFERENCE_KEYWORDS` — `$dynamicRef`/`$recursiveRef` are schema-bearing like
  `$ref` but their VALUE is a plain string, not a Mapping — so `_could_carry_subschema`'s
  Mapping-shape test never flags them, and a command channel reachable only behind one would
  otherwise be SILENTLY ADMITTED. Recognized by keyword identity instead, always treated as
  unresolvable — deliberately conservative since dynamic-scope `$dynamicAnchor` resolution
  isn't something this walker can safely reproduce.
- `_NUMERIC_BOUND_KEYWORDS` — explicit enumeration, deliberately NOT a `min*`/`max*` spelling
  heuristic: a prefix test would exempt an arbitrary unknown key like `minCustomThing` from the
  could-carry-subschema check purely by name, reopening the whitelist bypass.
- `_could_carry_subschema()` — true when an unrecognized keyword's value is shaped like it
  could hold a schema (mapping, or list at any depth containing one). Recurses through nested
  lists under the walker's depth cap so a schema-bearing value can't be laundered past the
  whitelist via extra list nesting. Charges every element against the shared node budget and
  fails closed on exhaustion, preventing a pathologically wide/deep value from being used as a
  denial-of-service against the admission check itself.
- `_is_inert_annotation_value()` — true when a value cannot itself carry a subschema
  (recursively scalar, or list/mapping of such with no schema-vocabulary key anywhere inside).
  A vendor annotation with genuinely descriptive metadata is inert; one embedding real schema
  vocabulary is not, regardless of key spelling — shape decides inertness, not the key. Fails
  closed (not-inert) on budget exhaustion, mirroring `_could_carry_subschema`.
- `_is_vendor_annotation_keyword()` — true for annotation-only keywords whose value carries no
  schema-bearing content. Narrow on two axes: (1) only the `x-` vendor-extension convention and
  `$comment` are even considered; (2) even for those, the value must be demonstrably inert — a
  vendor extension whose value is itself schema-shaped is exactly the hidden channel this
  walker exists to catch, so it is NOT exempted just for starting with `x-`. NEVER exempts
  `$`-prefixed keywords generally — that's exactly where real reference/applicator keywords
  live, so a blanket `$*` exemption would reopen the reference-bypass class this walker
  closes.
- `_mark_unknown_schema_keywords()` — whitelist enforcement: any unrecognized keyword with a
  subschema-shaped value is unresolvable, applied to every schema node the classifier inspects
  (including leaf-treated property schemas). Two carve-outs: (1) `$dynamicRef`/`$recursiveRef`
  anywhere makes the node unresolvable outright regardless of value shape; (2) a
  vendor-extension keyword is exempt even when Mapping-valued, since it's never an applicator.
  Value inspection shares the walker's node budget; exhaustion fails closed via the same
  helpers, preventing an enormous/deeply nested non-subschema value from burning unbounded CPU
  as a side channel around the traversal budget.
- `_OBJECT_CONTAINER_KEYWORDS` — presence of any of these (properties, patternProperties,
  additionalProperties, `$ref`, allOf, anyOf, oneOf, if, then, else, not) on a property's own
  schema means the property IS ITSELF a restated/composed schema — recurse rather than treat
  as a scalar leaf.
- `_ARRAY_ITEM_KEYWORDS` — an array property can be BOTH a free-form leaf channel in its own
  right AND hide a command channel inside an object-shaped item (items/prefixItems); both must
  be checked, so — unlike object-applicator keywords — these do NOT short-circuit leaf
  classification.
- `_SchemaWalkResult.unresolvable` — true when a channel could not be proven bounded
  (unresolvable/cyclic `$ref`, budget/depth trip, malformed shape, unrecognized keyword). Fed
  into fail-closed handling specifically for tools whose name/description signals an
  executor; otherwise it's just insufficient evidence, not an automatic denial.
- `_SchemaWalkResult.nodes_visited` — total-work budget companion to the depth cap, bounding
  runtime against a harmless but extremely wide fan-out (e.g. tens of thousands of anyOf
  branches) that the depth cap alone wouldn't stop.
- `_consume_node_budget()` — counts one unit of walker work; returns False once the
  total-node budget is exceeded so callers can stop iterating early — this is the mechanism
  that actually enforces `_MAX_SCHEMA_WALK_NODES` per-iteration, not just once at entry.
- `_composition_branch_reaches_free_form()` — true when any composition/conditional/`$ref`
  branch of a keyed property resolves to a free-form leaf. `{"anyOf": [{"type": "string"}]}`
  or `{"if": ..., "then": {"type": "string"}}` constrains the same instance the key names, so
  the key is unbounded even though the leaf type is indirect — without this, wrapping a plain
  string schema in one applicator layer would strip the key-to-leaf-type association the
  classifier relies on, evading detection with one layer of indirection.
- `_consider_property()` — leaf-shaped property schema never reaches `_walk_schema`, so the
  unknown-keyword whitelist is enforced directly here too (redundant for container properties,
  already walked, but harmless — closes the coverage gap for leaf properties that would
  otherwise skip the whitelist entirely). A container property is not itself a command value;
  the walker recurses into its reachable properties instead of classifying the container's
  key — but BEFORE recursing, it first attributes to the key any free-form leaf reachable
  purely through composition/conditional branches, because those constrain the key's own value
  even though expressed indirectly (ordering matters so a composed free-form leaf isn't missed
  just because the property also looks like a container). Array handling walks
  items/prefixItems for a hidden object-shaped command channel nested in array elements, but
  deliberately falls through to the leaf free-form check rather than returning early — the
  array property itself may ALSO be free-form (e.g. `argv: array-of-strings`), so both checks
  must run.
- `_walk_schema()` — bounded, cycle-safe, budgeted traversal collecting classifier evidence.
  Recognizes properties (including nested objects), allOf/anyOf/oneOf, if/then/else/not,
  items/prefixItems, local `$ref` resolution, patternProperties, and additionalProperties
  (both as a scalar free-form map channel and, when object-valued, as a nested subschema). Any
  other subschema-shaped keyword is unresolvable — the walker's instance of the whitelist
  rationale above. Draft 2020-12 evaluates `$ref` siblings; after resolving/recursing into the
  target, the function falls through (no early return) so every other keyword on this node is
  still walked for a free-form channel reachable alongside the reference, mirroring the same
  rule in `_object_boundedness_insufficient`. An object-valued additionalProperties map-entry
  schema is a reachable subschema in its own right; walked so a command channel hidden behind
  a dynamic (unnamed) map key isn't missed — but no fixed key name exists for a free-form
  additionalProperties map channel, so it only counts as executor-shaped evidence when
  corroborated by the tool's own name or description, mirroring the same corroboration
  requirement `unbounded-script-payload` demands of payload keys (an uncorroborated free-form
  map alone is too weak a signal to deny registration).

### Top-level classification and API

- `_classify_generic_executor()` — an unbounded command-shaped field
  (`has_free_form_command`) is dangerous on its own; unrelated extra properties do NOT make it
  safe, and — unlike the strong-name/description signals — no corroboration is required to
  deny it. Highest-priority, least-corroborated denial reason (`unbounded-command-input`),
  checked first. A strong executor identity (e.g. `bash`, `exec`, `run_command`) must be
  AFFIRMATIVELY demonstrated safe: empty/no schema, every property bounded via enum/const, or
  only auxiliary/identifier-like free-form fields. An unresolvable channel or a remaining
  executor-shaped free-form property leaves the identity uncorroborated — i.e. the default
  posture for a strong executor name is DENY unless the schema actively proves itself safe,
  the inverse burden-of-proof from ordinary tools (`executor-identity-with-insufficient-
  schema`).
- `_SYNTHETIC_MCP_WRAPPER_PARAMETERS` / `is_synthetic_mcp_wrapper_schema()` —
  `create_mcp_tool()` wraps every MCP tool in `async def mcp_callable(**kwargs)`. When a Tool
  is built without an explicit `tool_schema` (e.g. constructed directly rather than via server
  discovery), `function_to_schema()` reflects that wrapper into this exact deterministic
  shape. It carries NO information from the remote server — it's a fixed artifact of the
  wrapper's own signature/docstring — and must not be treated as remote descriptor metadata by
  the admission rule; `is_synthetic_mcp_wrapper_schema` detects and exempts this case.
  `mcp_tool_name` is the key under which the tool was registered in `Tool.mcp_config` — the
  identity `create_mcp_tool()` used to name/document the wrapper, distinguishing it from
  `advertised_name` (what the caller claims the tool is called).
- `validate_mcp_tool_admission()` — raises `PermissionError` when an MCP descriptor exposes a
  generic executor. Explicitly PURE and SYNCHRONOUS: no reads of MCPSecurityConfig,
  environment, files, pool state, or client acquisition — zero side effects, trivially
  unit-testable, impossible to bypass via mocking transport state. Registration-time admission
  control ONLY; does not change transport authorization (MCPSecurityConfig) or
  invocation-time permissions (PermissionPolicy) — those remain separate, independently
  enforced gates.
- `MCPConnectionPool.load_config()` — returns the server names declared in THIS file only. The
  pool accumulates configs ACROSS loads (`_configs` is process-global class state), so callers
  meaning "the servers from the file I just loaded" must use this method's return value rather
  than enumerating `_configs` afterward, since it may contain servers from earlier, unrelated
  calls in the same process.

## lionagi/agent/, lionagi/dispatch/, lionagi/hooks/

### `agent/factory.py`

- `_chain_pre_hooks()` — security-hook composition contract (ADR-0086 delta row 1): every
  security control (PermissionPolicy pre-hook, guard_destructive/guard_paths) is adapted into
  a `GateResult` evaluator run through the shared gate pass runner — each control evaluates
  exactly once per pass, and an evaluator that raises unexpectedly is treated as a fail-closed
  deny. When user pre-hooks are present, the security pass runs *twice* (before user hooks,
  then again after against the final possibly-mutated args), so a user hook can never rewrite
  arguments past a control that already approved them.
- `_resolve_mcp_path()` — shared by `_load_mcp` and `_forward_mcp_to_cli_request` so both agree
  on the authoritative `.mcp.json` and trust gate: explicit `spec.mcp_config_path` always wins;
  project-scoped `.lionagi/.mcp.json`/`.mcp.json` only considered when
  `trust_project_settings=True`; user-home `~/.lionagi/.mcp.json` trusted unconditionally. An
  explicit `mcp_config_path` that doesn't resolve raises `ConfigurationError` (declared intent
  = configuration error, not a soft no-op); only auto-discovered candidates fall through
  silently.
- `_forward_mcp_to_cli_request()` — two-"island" MCP design: `_load_mcp` only reaches
  lionagi-native `branch.acts` tools (inert for CLI providers, which spawn their own subprocess
  and never call back into `branch.acts`); this function reaches the second island — the CLI
  subprocess's own per-turn request kwargs (`ClaudeCodeRequest.mcp_servers`, forwarded via
  `as_cmd_args()` as `--mcp-config`). It deliberately sets `mcp_servers` (a plain dict field)
  rather than `mcp_config` (path field): `mcp_config`'s validator unconditionally rejects
  absolute paths, and both resolved candidates here are always absolute, so using
  `mcp_config` would raise `ValidationError` on the very next turn. An explicit
  `spec.mcp_servers=[]` with no resolvable config file still forwards `{}` (forcing zero MCP
  servers) rather than leaving the CLI to fall back to its own discovery.
- Mutating `chat_model.endpoint.config.kwargs` in place would corrupt any other Branch sharing
  the same iModel instance (Branch keeps a caller-supplied chat_model by reference, not copy).
  The fix copies chat_model before mutating, sharing `share_session`/`share_executor` with the
  original so only the endpoint config (MCP filter) becomes branch-local.

### `agent/gate.py`

`GateResult` is the one immutable verdict shape every tool-invocation security control
produces (ADR-0086 delta row 1); adapters convert PermissionPolicy, guard_destructive/
guard_paths, and the session-level gate into that shape. Legacy hooks signal denial by raising
`PermissionError` and an argument rewrite by returning a `dict`; any other exception is
treated as an evaluator failure and turned into a fail-closed deny rather than propagating
uncaught.

### `agent/hooks.py`

`_resolve_against_any_root()` / `guard_paths()` — multi-root path-containment contract: a
relative path formed against the first allowed root is accepted as long as the resolved
location falls under *any* configured root, not only the first. A symlink or protected-
basename denial fails identically against every root and always surfaces over a generic
denial. `guard_paths` validation is check-time only — a TOCTOU race (swapping a validated file
for a symlink after the check passes) is explicitly out of scope.

### `agent/nudge.py`

`NudgeEngine.evaluate()`/`_merge()` — firing bookkeeping (once/cooldown state) is committed
only for rules whose message actually survives the token-cap merge; a message dropped by the
cap must never be treated as delivered. A rule whose condition or render raises is skipped and
logged without breaking other rules.

### `agent/spec.py`

`_wire_secure_guards()` — guards are registered into the `security_pre` bucket, not the
ordinary user `pre` bucket, so they participate in the same security→user→security recheck as
an explicit PermissionPolicy (ties to the same ADR-0086 double-pass contract as
`_chain_pre_hooks` above).

### `dispatch/outbox.py`

Durability and delivery are separate guarantees: an outbox row persists in `state.db`
independent of consumer liveness; a surviving producer (Studio daemon scheduler tick)
re-attempts the notify template until success/backoff-exhaustion/`max_attempts`. Transport is
a shell command template (ADR-0059 D3), argv-safe: `payload`/`deliver_to` are substituted as
whole argv elements (never string-interpolated), and the template always runs via `exec` (no
shell), so shell metacharacters are inert.

- `enqueue_dispatch()` — idempotent on `dedup_key` (re-enqueue with the same key returns the
  existing row id). `max_attempts` bounds delivery regardless of `ack_required`: an
  ack-required row that keeps sending successfully but never gets acked still exhausts at
  `max_attempts` sends (`dead_letter`/`DEAD_LETTER_ACK_TIMEOUT`) rather than re-delivering
  forever. `expires_at` is an *additional* optional bound on top of `max_attempts`.
- `deliver_due_dispatches()` / `_deliver_one_due_row()` — ack-required rows loop back to
  `pending` (not `delivered`) on transport success, so the same due-scan re-attempts with
  backoff until acked/expired/`max_attempts` exhausted; the default tier stops at `delivered`
  on first success. `delivering` rows are re-scanned for crash recovery, but a claim is
  exclusive only for `_CLAIM_LEASE_SECONDS` — the guarded attempt-counter CAS in `transition()`
  (guard on pre-claim `attempt`, not just status) prevents two overlapping scans from
  double-running the transport for the same attempt. Race hardening: the due-row snapshot and
  each row's `transition()` call are separate transactions, so a concurrent
  `purge_dispatch(es)` can delete a snapshotted row; `transition()` raises `LookupError` in
  that case, caught per-row so one purged row is skipped without aborting the rest of the
  batch.
- `purge_dispatch()` / `purge_dispatches()` — `purge_dispatch` accepts any status (naming an
  exact id is already deliberate non-bulk intent) and writes one `admin_events` audit row on
  success. `purge_dispatches` requires `status` and/or `before` (bare call raises
  `ValueError`, guards against accidental full-table delete); status semantics are
  deliberately asymmetric — an explicit status is honored exactly as given (including
  in-flight `pending`/`delivering`, treated as deliberate operator override), while a
  status-less call defaults to terminal-only (`delivered`/`acked`/`dead_letter`/`expired`) so
  it can never implicitly sweep in-flight rows a live scheduler tick may still claim. Distinct
  from the automatic terminal-only retention sweep in `db_maintenance.prune_old_data`. Always
  writes an `admin_events` row, including on `dry_run` calls, and preserves
  `status_transitions` rows for purged ids.

### `dispatch/revival.py`

This is a plain library call, not a new schedule `action_kind`: wiring a first-class
action_kind through the scheduler's fire/subprocess-spawn machinery would require rebuilding
the `schedules.action_kind` CHECK constraint (the same SQLite rename-rebuild migration
`_drop_legacy_action_kind_check` performs for the existing enum) — heavier machinery than a
library call needs. Any schedule action that can call a Python function can invoke it;
nothing in the module assumes a dedicated action_kind.

### `hooks/builtins.py`

`persist_session_end()` — in the normal CLI flow, `teardown_persist()` always stamps the
terminal status via `_teardown_common()`'s `update_status()` call before emitting
`SESSION_END`, so the session row is already terminal by the time this handler runs. In that
case only pure usage fields are written (input/output tokens, cost, turns, duration) — the
status/reason_code/ended_at transition is skipped (avoids a duplicate `status_transitions` row
and a double-fire clobbering an already-recorded status), and `node_metadata` is left
untouched since `_teardown_common()` already owns it for a terminal row and `update_session()`
does a plain column SET, not a merge (writing `{"error": ...}` here would clobber richer
existing data). Related: `persist_session_start`'s explicit `reason_code` on the "running"
transition avoids tripping a deprecation shim that would otherwise raise and get swallowed by
the bus, silently dropping all the provenance fields passed alongside it.

## lionagi/providers/

### `_cli_subprocess.py`

`ndjson_from_cli` PGID capture: the process-group id must be captured immediately after
spawn, not at teardown — if the child has already exited and been reaped by the time teardown
runs, `os.getpgid(proc.pid)` raises `ProcessLookupError`. Since `start_new_session=True`,
`pgid == proc.pid`, so capturing `proc.pid` right after spawn is equivalent and safe. The
actual pid-guard/platform check lives in `aterminate_process_group`.

### `anthropic/claude_code.py`

- **CLI flag metadata protocol.** Every CLI-mappable `ClaudeCodeRequest` field carries a
  `json_schema_extra` dict built by `_cli()`/`make_cli_flag()`, consumed by
  `build_declarative_cli_args()`. Kind semantics: `value` → `--flag <str(val)>`; `bool` →
  `--flag` when truthy else omitted; `bool_pair` → `--flag` when True, `--neg-flag` when False,
  omitted when None; `list_args` → one flag followed by many positional args; `json_value` →
  dict/list serialized to a JSON string; `repeat` → the flag repeated once per item. Anyone
  adding a new CLI-mappable field must pick the right kind or the arg won't round-trip.
- **`mcp_servers` None-vs-`{}` invariant.** Defaults to `None`, not `{}`, specifically so a
  request that never touched the field is distinguishable from a caller that explicitly
  forwarded an empty server selection. The `is not None` check (not truthiness) in
  `as_cmd_args()` means `mcp_servers={}` still emits `--mcp-config {"mcpServers":{}}`, forcing
  zero MCP servers, rather than silently omitting the flag and letting the CLI fall back to its
  own MCP discovery. Flattening this to a truthiness check would silently break "explicitly
  disable all MCP servers."
- **Repo-containment scope for `add_dir`.** The write-target path containment check
  (`contain_paths_in_repo`) covers `system_prompt_file`, `append_system_prompt_file`,
  `mcp_config`, `settings` — deliberately excluding `add_dir`. `add_dir` is a read-only grant
  validated separately by `_validate_add_dir`; absolute paths there are intentional grants, not
  escapes, and must not be rejected by the write-target containment logic.

### `google/gemini_code.py`

Google folded Gemini Code Assist CLI into Antigravity (`agy`). This provider drives `agy` in
headless print mode (`--output-format json`), which emits exactly one terminal JSON object
(one NDJSON record) consumed unchanged by the shared `ndjson_from_cli` plumbing.
`conversation_id` is stored as `session.session_id` so native resume works via `--conversation`.
The public names/aliases `gemini-code` / `gemini-cli` / `gemini_cli` are kept for backward
compat even though the underlying binary is `agy`.

- **`resolve_agy_model` resolution/precedence rules.** `agy` has no separate effort flag;
  effort is expressed only via a Low/Medium/High suffix baked into the model display name.
  lionagi's effort scale (`none|minimal|low|medium|high|xhigh|max`) is clamped onto Gemini 3.1
  Pro's Low/High-only range via `_clamp_gemini_effort`. An exact, already-`(...)`-qualified
  `model` (a concrete agy display name) wins over `effort` by default. `reapply_effort=True`
  exists specifically to let a new `effort` replace the suffix baked into a *persisted* prior
  resolution (e.g. `li agent -r ... --effort ...`), while a `model` the caller explicitly typed
  in the current turn still wins regardless.
- **No per-tool stdout events.** `stream_gemini_cli`'s `on_tool_use`/`on_tool_result`
  callbacks are accepted for interface parity with other CLI providers but never fire in this
  transport, because `agy`'s json output surfaces no per-tool events on stdout (they exist only
  in the per-session transcript file).
- **Relative-path/stdin quirks.** `agy` resolves relative `--add-dir` entries against the
  process cwd and has no `-C`-style flag to change that, so the resolved workspace must be
  passed as the subprocess `cwd`. Default stdin is `DEVNULL` since print mode reads nothing
  from stdin.
- **`streams_first_output_early` stays False.** `agy`'s json print mode only yields output
  after the entire result object arrives (no incremental streaming), so a healthy long-running
  call looks identical to a dead/hung worker to a first-chunk watchdog — the endpoint can't opt
  into the early-first-output fast path other CLI providers use.

### `openai/_chat_schemas.py`

`uses_developer_messages` — the `system`-vs-`developer` message-role gate is deliberately
conservative and prefix-based: only o1/o3/o4/gpt-5 families (including dated variants, matched
by prefix after stripping any provider prefix) are gated onto `developer`. Unknown or missing
models fail closed and keep `system` — the default behavior on an unrecognized model string is
the safe, backward-compatible one, not the newer `developer` role.

### `groq/audio_transcription.py`, `openai/audio.py`, `openai/images.py`

`_replayable_file_factory` retry-safety contract (identical helper duplicated in all three
files): returns a zero-arg callable that produces a *fresh* file object for each retry
attempt. Bytes/bytearray are snapshotted once and re-wrapped in a new `BytesIO` per attempt; a
seekable stream is seeked back to its starting position before each attempt; a non-seekable
stream cannot be replayed safely, so when a retry could occur (`require_replayable=True`) it
raises before any network I/O rather than silently resending an exhausted stream (single-shot
endpoints get the raw stream handed through once). The stream is snapshotted once and its
position restored immediately — handing the *live* stream object to each attempt isn't
sufficient, because an explicit `RetryConfig` retry re-invokes `_call`, which rebuilds this
factory around the now-consumed stream (already at EOF), and would silently upload an empty
file on retry without this snapshot.

### `openai/codex.py`

- **cwd/-C double-resolution gotcha.** `_ndjson_from_cli` deliberately does NOT pass `cwd=`
  to `ndjson_from_cli`, because the Codex CLI already receives the workspace via the
  `-C <repo>` argument emitted by `as_cmd_args()`. Setting `cwd=` as well would make the CLI
  resolve `-C repo` from inside `repo`, producing a bogus `repo/repo` path.
- **Error envelope shape varies by event type.** Codex CLI's error payload location differs by
  event type: `"error"`-type events carry a top-level `"message"` key with no nested `"error"`
  key, while `"turn.failed"` events nest the message under `error.message` — both must be
  checked. The raw `error` value is captured *before* null-normalization (`_raw_err`)
  specifically because the benign-EOS check further down must distinguish an explicit
  `"error": null` (a malformed envelope — a real error) from the bare `{}` sentinel (benign
  EOF).
- **Benign-EOS sentinel on resumed sessions.** Some Codex CLI versions emit
  `{"type": "error", "error": {}}` when a resumed session ends normally — this is a benign
  end-of-stream, not a real failure, and is tagged so `run()` treats it as clean EOS rather
  than raising `RunFailed`. All of the following must hold for the benign-EOS classification:
  `type == "error"` exactly (`"turn.failed"` is never considered benign); the *raw* payload is
  exactly `{}` (an explicit `null`, once normalised to `{}`, must NOT qualify, hence checking
  `_raw_err` pre-normalization); and no other failure-indicating keys (`code`/`message`/
  `status`) are present in the outer event. Getting any of these three conditions wrong either
  misclassifies a real failure as benign or vice versa.

### `pi/cli.py`

- **`_PI_MODEL_PROVIDER_MAP` design rationale.** Model-name prefixes are mapped to
  `pi --provider` values, but only for *unambiguous* prefixes where the model name uniquely
  identifies the provider. Ambiguous family names (`llama`, `gemma`, `mistral` — available on
  multiple providers) are deliberately omitted from the map, so callers must set the provider
  explicitly or let `pi` resolve it itself, rather than the code guessing wrong. `strip=True`
  entries remove the prefix from the model string, needed for `openrouter/`-style routing.
- **Pi CLI arg-parsing quirk.** Pi's CLI arg parser has no `--` terminator support, so the
  prompt is passed as a bare positional argument. Prompts starting with `-` or `@` may be
  misparsed by Pi's own CLI as flags/file-references — callers should avoid leading dashes (or
  `@`) in prompts passed through this provider.
- **Dual meaning of the `"done"` event type.** Pi CLI overloads the `"done"` event type: both
  the top-level `AgentEvent.done` (true end-of-stream) and a top-level
  `AssistantMessageEvent.done` (an individual assistant message finishing) use the same
  `"done"` string, and both may carry a final message with model/usage info that must be
  captured via `_remember_assistant_message`.
- **`streams_first_output_early` stays False.** Pi's transport emits an `"agent_start"` event
  right after spawn, but `stream()` discards raw dict events and only yields its first
  `StreamChunk` once a `PiChunk` actually carries text/thinking/tool content — so the first
  output a caller can observe may lag the process spawn by the model's full "thinking" time,
  making the early-first-output optimization unsafe here (same gotcha class as
  `gemini_code.py`'s `agy` note above).

## lionagi/service/ (remaining)

### `connections/registry.py`

`EndpointRegistry.match()` — on a registry miss, consults the plugin
registry (ADR-0088 D3) before falling back to the generic
OpenAI-compatible endpoint: `_consult_plugin_providers()` imports every
ACTIVE plugin's declared provider module (never at import time or
discovery, preserving import-time O(1)), exclusively through
`PluginRegistry.activate_target` — never a direct `importlib` call on
plugin code — so the trust/enabled/active chokepoints enforced there apply
here too. Each activation is cached by the plugin registry itself, so
repeated misses are cheap. A plugin supplying no matching provider (or none
at all) leaves the fallback identical to the no-plugin case.
`_revalidate_plugin_entry` keeps a plugin-sourced registry entry available
only while its declared target remains trusted, removing it on
`PluginActivationError`.

### Retry & sentinel-exclusion contract (`connections/endpoint.py`, `providers.py`, `resilience.py`)

- `endpoint.py` — 4xx (non-429) client errors are wrapped in `_NonRetryableClientError` so the
  original `aiohttp.ClientResponseError` stays inspectable via `__cause__` while retry logic
  sees an excluded type. The sentinel must stay excluded until whichever retry layer is active
  (the outer `retry_config`-driven wrapper in `call()`, or the native path in
  `_call_aiohttp()`) has given up — unwrapping earlier would let a broad
  `retry_exceptions=(aiohttp.ClientError,)` config replay a 400/401/403 the transport contract
  intends as single-shot. Request bodies are rebuilt inside the per-attempt function rather
  than before retry orchestration, because `FormData`/`BytesIO`/file-stream bodies can be
  consumed by the first POST and must not be silently replayed on a later attempt. In the
  native (no-`RetryConfig`) path, `config.max_retries` is a total-attempt cap (formerly
  backoff's `max_tries`), but `retry_with_backoff` runs `max_retries+1` attempts internally, so
  the call subtracts one to preserve the configured total attempt count. `_can_retry()`: true
  when an explicit `RetryConfig` wraps the call, or the native path's total-attempt cap allows
  a second attempt; single-shot endpoints (`max_retries<=1`, no `RetryConfig`) never replay a
  body, so callers may hand over non-replayable inputs like one-shot streams only in that case.
- `resilience.py` — in `retry_with_backoff`, `exclude_exceptions` membership is checked
  per-instance (`except exclude_exceptions`) rather than per-type, which correctly handles
  subclass hierarchies — e.g. `retry_on=(OSError,), exclude=(ConnectionError,)` must not retry
  a `ConnectionError` even though it IS-A `OSError`.

### `connections/agentic_endpoint.py`

`streams_first_output_early` — true when a CLI/agentic transport emits its first `StreamChunk`
shortly after the subprocess spawns (e.g. an ndjson "system"/"init" event), making a stalled
first chunk a reliable dead-worker signal. False for transports that buffer all output until
the run completes, where a slow-but-healthy call is indistinguishable from a dead one until
the whole result arrives. This flag gates `run.py`'s default liveness watchdog
(`LIONAGI_WORKER_LIVENESS_TIMEOUT`).

### `connections/endpoint_config.py`

`_FIELD_KEYS_BY_CLASS` is keyed on `id(cls)` rather than the class object itself, because a
dict lookup by class object would go through `__eq__`/`__hash__`, which a custom metaclass may
override; each cache entry retains a strong reference to the class (keeping the id stable) and
is only served when the stored class `is` the lookup class, guarding against id reuse after
garbage collection. The cached value is the set of accepted field keys (including declared
aliases) computed once per class from `model_json_schema()`, since subclasses may add
fields/aliases and rebuilding the JSON schema on every `EndpointConfig` construction would
cost more than the rest of validation combined.

### `imodel.py`

- `stream()` — the `finally` block pops the in-flight call from `executor.pile` without
  yielding inside the `finally`, because yielding inside a generator's `finally` would swallow
  a `CancelledError` arriving during generator cleanup, which would break `anyio.fail_after`
  timeout enforcement for callers wrapping the stream.
- `copy(share_session, share_executor)` — creates a new `iModel` with the same config but a
  fresh ID. `share_session=True` carries the CLI provider's `session_id` onto the copy so
  cross-turn continuation is preserved (only applies when both endpoints are
  `AgenticEndpoint`). `share_executor=True` reuses the exact same `RateLimitedAPIExecutor`
  instance instead of building a fresh one, so a caller-supplied executor's rate limits and
  queue capacity stay shared between original and copy. Default (`False`/`False`) gives the
  copy its own independent executor — this is what `Branch.clone()` relies on for CLI
  providers, where each cloned branch needs its own session and queue rather than contending
  with the parent's. `circuit_breaker` and `retry_config` objects are shared by reference (not
  deep-copied) between original and copy; only `config` is deep-copied.

### `manager.py`

`iModelManager.shutdown()` — without explicitly closing every registered `iModel`, each one's
background rate-limit replenisher task stays scheduled and prevents `anyio.run`/`asyncio.run`
from returning at process exit. Idempotent; per-model failures (including `CancelledError`)
are logged and swallowed so one broken endpoint's shutdown failure can't block the others.

### `providers.py`

`normalize_effort()` must be called once at every boundary where a raw effort string enters
lionagi (CLI flag, profile frontmatter, orchestration spec) because the clamp tables
downstream are keyed on lowercase effort levels and silently misclamp (no raise) on an
un-normalized value like `"High"`. Codex reasoning-effort ceilings are model-dependent per the
codex CLI's live model list: `gpt-5.6-sol`/`gpt-5.6-terra` accept `max`/`ultra`,
`gpt-5.6-luna` accepts `max` only, and every earlier model tops out at `xhigh`; unrecognized
(future) models intentionally pass through unclamped so a genuinely supported new tier is
never silently degraded. agy (the Antigravity CLI) has no effort flag/kwarg at all — effort is
expressed only as a Low/Medium/High suffix baked into the resolved `--model` name, and Gemini
3.1 Pro has no Medium tier, so lionagi's 8-level `none|minimal|low|medium|high|xhigh|max|ultra`
vocabulary collapses onto this 3-tier scale via `_GEMINI_EFFORT_CLAMP`.

### `rate_limited_processor.py`

- `start_replenishing()` — its cancellation handler wraps `await self.start()` too, so a
  cancel arriving before the main loop is reached is still caught inside the task instead of
  surfacing as an uncaught error on `stop()`. The periodic re-drive of the queue
  (`if not self.queue.empty(): await self.process()`) exists because `process()` re-enqueues
  rate-limited events instead of dropping them, but `forward()` is one-shot — without
  re-driving on each refresh, deferred events would sit `PENDING` until the caller's
  `invoke()` safety timeout instead of actually retrying once the budget replenishes.
- `stop()` — Python 3.11+ re-raises `CancelledError` on `await task` after `task.cancel()`
  even though the task body already suppressed it internally; this is swallowed explicitly so
  callers closing multiple iModels in sequence don't abort on the first one's close.
- `handle_denied()` — rate-limit denial is a deferral, not a rejection — returning `False`
  makes the base `process()` re-enqueue the event (stays `PENDING`) for retry once the limit
  replenishes, rather than terminalizing it the way a permission rejection would.

## lionagi/testing/

### `testing/_endpoint.py`

`ScriptedEndpoint.copy_runtime_state_to()` — when an `iModel` is cloned, the script must be
**deep-copied**, not shared, so the clone gets an independent positional cursor. If the script
were shared, positional matching would cross-contaminate between clones — clone A consuming
response entry 0 would advance the shared cursor, so clone B's first call would incorrectly
receive entry 1 instead of entry 0. Recorded calls (`self.calls`) are shallow-copied instead,
since each clone only needs its own future calls tracked, not a defensively-copied history.

### `testing/_script.py`

- `_build_entry()` — response entries are dispatched to their concrete subclass
  (`TextResponse`, `ToolCallResponse`, `StructuredResponse`, `StreamResponse`, `ErrorResponse`)
  by manually branching on the `type` field rather than relying on Pydantic v2's discriminated-
  union support. Deliberate: Pydantic v2 won't reliably select a discriminated-union member
  when fields beyond the discriminator (`type`) differ between candidate models, so manual
  dispatch is used to get clearer validation errors when a script entry is malformed.
- `ScriptModel.next()` — response-entry matching has a two-phase precedence contract that test
  authors writing scripts rely on. **Phase 1**: unless `mode == "positional"`, every entry with
  a non-empty `when:` matcher is checked (skipping entries already served by a `when:` match)
  and the first one whose predicate matches (`call_index`, `after_calls`, `prompt_contains`,
  `prompt_regex`, `has_tool`) is returned; if `mode == "when_only"` and nothing matched, it
  raises immediately. **Phase 2**: falls back to positional order over entries that do NOT have
  a `when:` matcher, advancing an internal cursor; raises `ScriptExhaustedError` once positional
  entries are exhausted. This order (when-matchers-first, then positional) is the core scripted-
  fixture replay semantic and isn't obvious from the fixture's public surface alone.

## lionagi/tools/

### `sandbox_backend.py`

The sandbox backend seam (ADR-0090): backend divergence (local worktree vs. Daytona vs.
future backends) is absorbed entirely in `provision()` and `capabilities()`; `run_cell()`'s
signature never changes per backend. A `Cell` declares a `kind`: `prompt_cell` (the provider
call runs host-side, already authenticated — no secrets ever cross into the box) or
`exec_cell` (untrusted code runs inside the box, secrets injected explicitly). Callers must
read `capabilities()` to decide what a backend can do and must never branch on a backend's
name/type — this is the load-bearing security/extensibility contract of the whole seam.
`_SAFE_ENV_KEYS`: `run_cell`'s subprocess never blanket-inherits the host environment
(credential-leak vector); only `PATH`, `HOME`, `PYTHONPATH`, `VIRTUAL_ENV` are forwarded
automatically, plus whatever `cell.env` explicitly allow-lists.

### `khive_injection.py`

Reference `ContextProvider` (ADR-0008): recalls/optionally composes from a khive daemon over
the same MCP transport lionagi already uses for tool servers
(`service.connections.mcp_wrapper`) — no new transport, and no khive/MCP import at module
load, so the core import path stays clean without the `mcp` extra installed. Every recall
emits `brain.auto_feedback` in the same round-trip with the policy's explicit `profile_id`
(khive's auto_feedback does no binding resolution, so an implicit/default profile
mis-attributes the event). `writeback()` is a separate opt-in POST-turn hook — rule-based tool
error/resolution pairs written to `memory.remember` at capped, low-provenance salience,
invoked by the `operate()` Middle (not `provide()`), and it is NOT the nudge plane. Both
`provide()` and `writeback()` fully swallow transport failures (logged only) so a turn always
proceeds.

`KhiveInjectionPolicy.namespace`, when set, is threaded onto every khive verb the provider
emits (recall, compose, auto_feedback, remember) to isolate its writes to a named store.
`auto_feedback` WRITES to the live brain store, so an unpinned "read-only" caller still mutates
posteriors — pinning a namespace is required, not optional, wherever writes must stay isolated.
Currently only the write verb honors namespace (read verbs reject unknown params); this is
forward-wired for when reads grow namespace scoping too.

### `sandbox.py`

Git worktree lifecycle (`_cleanup_worktree_sync`, `_merge_sync`, `sandbox_merge`) is
retry-safe: a resource that's already absent counts as cleaned up, so a partial failure (e.g.
worktree removed but branch deletion blocked by another checkout) can be completed by a later
retry instead of failing forever on the step that already succeeded. `SandboxSession.is_active`
only flips to `False` once both resources are actually gone — a partial failure keeps the
session marked active so a caller can't mistake it for cleaned up. Merge additionally refuses
when `repo_root` is in a detached HEAD state, isn't checked out on the session's recorded base
branch (no auto-checkout), or targets a protected branch name (`main`/`master`/`release*`)
unless the caller explicitly opts in via `allow_protected`. (`git rev-parse --abbrev-ref HEAD`
returns the literal string `"HEAD"` when the repo is detached — not an actual branch name — a
quirk both `_merge_sync` and `create_sandbox` special-case, since unhandled it would let a
merge move a detached HEAD forward with no branch ref pointing at the result, or let
`create_sandbox` record a nonexistent branch as its merge target.)

`_list_untracked_files()` uses `git ls-files --others --exclude-standard -z` (NUL-delimited
raw paths) rather than `git status --porcelain`, which quotes/escapes paths with spaces or
special characters (breaking naive `line[3:]` slicing) and reports an untracked directory as a
single `?? dir/` entry instead of the files inside it.

### `communication/messenger.py`

`_fire()` — two related logging design decisions: (1) for the `help` event specifically, a
raising coordinator callback is caught and logged rather than propagated, because the whole
point of `help` is fire-and-continue — it must never surface as an unhandled exception on the
emitting worker's tool-call turn. (2) When no callback is registered at all for an event,
that's logged at `debug` (not `warning`) — a mis-wired coordinator should stay discoverable
during bring-up, but debug-level avoids spamming normal runs where some events are legitimately
unused.

### `coding.py`

`CodingToolkit.__init__`'s `sandbox_allow_protected` — whether the bound sandbox tool's
`merge` action may target a protected branch name (`main`/`master`/`release*`) is an
operator-level trust decision, not something the agent can request per call. It's deliberately
absent from `SandboxRequest` so an in-band agent can never self-approve merging into a
protected branch — set it only when composing the agent from code you control (e.g. a CI job
that always merges into main).

`_invalidate_stale_reads()` drops `file_state` entries whose backing reader-read result was
evicted/compacted by the context tool; otherwise the read-before-edit guard would stay
"satisfied" for a read the model can no longer actually see, letting it edit blind.

### `_subprocess.py`

`_subprocess_sync()` — `env=None` inherits the full parent environment; callers pass an
explicit mapping to scope less-trusted commands (e.g. the ADR-0090 sandbox-backend seam) to a
minimal environment.
