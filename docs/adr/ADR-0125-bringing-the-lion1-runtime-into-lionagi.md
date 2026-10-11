# ADR-0125: Bringing the lion1 runtime into lionagi: order, the internal actor, and consolidation

- **Status**: Accepted (2026-10-11)
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: governance
- **Date**: 2026-10-11
- **Relations**: extends ADR-0120 (interception, observation and delivery planes), ADR-0121
  (authoritative action execution), ADR-0122 (feature boundaries and optional runtime profiles),
  ADR-0092 (memory store) and ADR-0093 (external memory adapters); names the records each step
  amends; migrates the lion1 design series into this corpus (D8); during this migration, does not
  apply ADR-0122 D8's facade deletion or default-dependency removal (this record's D4)

## Context

This repository holds two agent runtimes. `lionagi/` is the published library: a `Branch` is one
conversation with a model, its tools and its message history; a `Session` groups branches and runs
operation graphs over them. `lion1/` is a second runtime written beside it with its own design
series (lion1 ADR-0001 to lion1 ADR-0029): an actor holds identity, privileges, command handlers and
an inbox; a run is a bounded, recorded loop in which the model issues commands in LNDL and every
step is an entry in an append-only record.

Neither is a replacement for the other. lionagi is the API users import and the base Studio, the
`li` command and the MCP server build on. lion1 is where several designs the lionagi records already
call for were built and exercised: one dispatch path for commands, an event bus kept apart from
command interception, privilege checks at dispatch, bounded runs with typed outcomes, context folding, notes that outlive a
run, a served actor and a long-running agent loop. This record decides how the second becomes part
of the first.

Measured at commit `14c7a358` (a static inventory of `lionagi/`, non-blank non-comment lines):

| Part | Lines | Share |
|------|-------|-------|
| `studio/` | 31,622 | 24.1% |
| `cli/` | 23,177 | 17.7% |
| `state/` | 11,463 | 8.7% |
| `providers/` | 7,882 | 6.0% |
| `protocols/` | 6,397 | 4.9% |
| `operations/` | 6,271 | 4.8% |
| `ln/` + `libs/` + `utils.py` | 7,977 | 6.1% |
| `service/` | 5,565 | 4.2% |
| `mcp/` | 5,555 | 4.2% |
| everything else | 25,253 | 19.3% |
| **total** (580 files) | **131,162** | |

lion1's core (`lion1/src/lion1/`) is 6,398 lines in ten modules, of which `actor.py` is 2,868 and
`lndl.py` 1,645; its hub (backends, tools, hooks, serve, the long-running agent) is 46 files.

**P1: two runtimes model the same things.** A model backend (lionagi `iModel`, lion1 `Backend`), a
callable the model may invoke (lionagi `Tool` and `FunctionCalling`, lion1 `Handler`), an
interception point (lionagi `Tool.preprocessor`, `HookBus.TOOL_PRE`, `HookRegistry`; lion1
`Actor.before` and `Actor.after`), an event stream (lionagi `SessionObserver`, `HookBus`,
`Broadcaster`; lion1 `Bus`), structured output (lionagi `operate` with an optional LNDL `Middle`;
lion1 LNDL as the run's language) and a persistent store (lionagi `state.db`; lion1 rows written
through a store client). Two implementations of each concept means two sets of bugs, two sets of
tests and a reader who cannot tell which one a feature belongs to.

**P2: the dispatch and action designs lionagi already accepted are not built.** ADR-0120 counts at
least fifteen callback and dispatch contracts and decides two shared kernels with the old families
as adapters; its implementation status is Phase 0. ADR-0121 decides one authoritative action
executor and typed admission decisions; it is Proposed and not started. ADR-0044 (executable
permissions), ADR-0007 (turn request compilation), ADR-0018 (turn-scoped execution state) and
ADR-0093 (external memory adapters) are Proposed. ADR-0122 (minimal default install) is Accepted
and not started. lion1 holds working code for much of what those records ask for.

**P3: the default install is the whole application.** `pip install lionagi` ships Studio, the CLI
and the run store, about 66,000 of the 131,000 lines, to a user who wanted a Branch. ADR-0122 D3
decides the minimal SDK profile as the default install; nothing enforces it yet. Removing
duplicated code inside the library does not change this much: a read of every area against lion1
estimates 1,400 to 2,400 removable lines (Appendix A), one to two percent. The application layer
also cannot simply be moved out, because core modules import it: `session/observer.py` imports
`agent.gate` and `state.db`, `session/signal.py` and `operations/flow.py` import `casts.emission`,
and `tools/coding.py` imports `agent.factory`, `agent.spec` and `agent.nudge`. Those imports are
the visible part; ADR-0122's first phase also needs neutral provider, process and store contracts
and no exceptions to its import rules. Extras can then cut the modules a default import loads and,
in a release that decides it, the dependencies a default install pulls in; this record makes only
the first change (D4). Neither takes optional source out of the wheel: ADR-0122 D3 and D7 keep one
distribution, and removing that source is a separate distribution decision.

**P4: lion1's public API costs a reader too many names.** Each of its eight examples imports six to
fifteen names from two to four modules, and every one imports the store client. A first program
should need one import.

**P5: lion1 is loosely typed at its edges.** Handler functions take and return `Any`, hooks return
unions of `str | BaseModel | dict | None`, and several records are plain dataclasses. lionagi's
public surface is typed and its users rely on that.

**P6: lion1's runtime records need a database, not a service.** The core writes run, session, call
and note rows (16 call sites of the `lion.*` verbs and two blob calls in `lion1/src/`); the
messaging, task and schedule verbs (about 60 call sites) are all in `lion1/hub/` and `lion1/apps/`,
where they connect long-running agents to one deployment's mailbox. lionagi already owns an
SQLite store with sessions and messages tables.

**P7: the two design series share numbers.** lion1 ADR-0007 (the record) and lionagi ADR-0007
(turn-request compilation) are different records; a bare "ADR-0007" in a merged tree resolves
against whichever series the reader assumes.

**P8: external contributions already target the seams this record moves.** An open contribution
adds a vector-store backend to the memory seam (ADR-0092) and a model-callable retrieval tool built
on `Tool`. Where those seams go must be decided before either merges.

| Concern | Decision |
|---------|----------|
| How lion1 enters | D1: by concept, into the lionagi construct the records already name; no `beta` package, no second import surface. |
| Order | D2: seven steps, each with its own record or amendment, in a fixed order. |
| The actor | D3: `Agent` is the public name; the actor is an internal abstraction built on `Branch`. |
| Compatibility | D4: existing public imports, call shapes, return types and behavior are preserved; removing any of them needs its own versioned decision. |
| Storage | D5: lionagi's own SQLite store is the default; external stores are adapters; lionagi ships no store binary. |
| Python floor | D6: ported code runs on Python 3.10, lionagi's floor. |
| Types | D7: ported code is typed on every public surface and checked. |
| Design records | D8: one corpus; lion1 records migrate into the step records and are deleted with their code. |
| Landing discipline | D9: each step lands as characterization, then the new mechanism with adapters, then internal callers moved, with stored rows and size measured. |

**Out of scope.** The detailed mapping of lion1's actor onto `Branch` and `Session` (step 5's record
decides it; D3 fixes only the boundary). Pricing, hosting and any service offering. The contents of
each step's record beyond the target and source this record assigns it. lion1's deployment-specific
hub modules (its mailbox desk, chores and CI instruments), which stay outside the library.

## Decision

### D1: lion1 enters by concept, not by package

Each lion1 concept is brought in by changing the lionagi construct that already owns that concept,
or that an accepted or proposed lionagi record designates as its owner. There is no
`lionagi.beta`, no `lionagi.lion1` and no re-export of lion1 names. The `lion1/` directory stays in
the repository as the reference implementation and test bed until the last step that draws on it
has landed; then it is removed in one change. lion1 has not been released: no lion1 tag exists, and
the package index holds only a 0.0.1 placeholder under its name that states it is not yet released,
so removing the directory withdraws no released code.

Exact semantics:

- A concept with a lionagi owner and a lionagi record (for example handler dispatch, owned by
  `protocols/action` under ADR-0121) is changed in place; the record is amended in the same series
  of changes.
- A concept with no lionagi owner (bounded runs with typed outcomes, the served agent, the
  long-running wake loop) gets a new module under the package the step's record names, and a new
  record.
- A lion1 module that duplicates a lionagi one without adding a concept (its own JSON helpers, its
  own model backends where a lionagi provider exists) is not ported; the lionagi one is used.
- Nothing in `lionagi/` imports from `lion1/` at any point. A test that compares the two may.

Why this way: a beta package would ship two runtimes under one name, with two answers for every
concept in P1, and every later step would have to migrate users off the beta names as well as off
the old ones. Changing the owning construct keeps one answer per concept at every commit.

### D2: seven steps, in order

| Step | What changes | lionagi target records | lion1 source |
|------|--------------|------------------------|--------------|
| 1 | One handler mechanism: commands dispatched on one path with privileges, interception before and after, a typed result; events on a fan-out that cannot authorize or veto a command; existing tool, hook and observer APIs become adapters | ADR-0120 D2/D4/D7, ADR-0121 D1-D5 and D10, ADR-0044 D2-D4, ADR-0047 | lion1 ADR-0005, `actor.py` `Handler`, `bus.py` |
| 2 | Directives and context management: what each model call contains, an internal append-only conversation record, folding, notes, memory reached through handlers with keyed access as a capability | ADR-0007, ADR-0018, ADR-0008, ADR-0092 | lion1 ADR-0006, ADR-0007, `record.py`, `notes.py`, the view in `actor.py` |
| 3 | LNDL as an optional structured-output format, one implementation | ADR-0024, ADR-0051 | lion1 ADR-0004, `lndl.py`, `spec.py` |
| 4 | Dedup and condense, and preparation of optional profiles under D4 | ADR-0122, ADR-0050 | Appendix A |
| 5 | The public `Agent` over an internal actor: identity, privileges, bounded runs, a typed model-call result | ADR-0041, ADR-0027 | lion1 ADR-0001, ADR-0002, ADR-0003, ADR-0010 |
| 6 | Serving an agent over HTTP (FastAPI, today a dependency of the `studio` extra; loopback by default), and the Studio frontend surface that starts, follows and stops served agents | ADR-0076, ADR-0078, ADR-0079 | lion1 ADR-0021 |
| 7 | Long-running agents that wake on an inbox carried by lionagi's own state store and its existing message and dispatch tables, shown in Studio | ADR-0070, ADR-0059, ADR-0072 | lion1 ADR-0013 |

Each step opens with its record (new or an amendment named in the table) and lands as a series of
changes under D9. A step starts when the previous one's record is accepted and its first change has
merged; work inside a step may overlap the next step's record.

Why this order: steps 2 to 7 all register code that runs when something happens, so step 1 comes
first; building them on `Tool` and the current hook families would build on what step 1 retires.
Context management (2) decides what a run's model call contains, which the structured-output path
(3) and bounded runs (5) both read. Dedup (4) comes after the three mechanism steps because those
steps delete most of the duplicate mechanisms themselves; the remaining duplication is then smaller
and easier to see. The agent (5) needs handlers, context and output; serving (6) and waking (7)
need an agent.

The fixed order is for concepts. Dependency cleanup under ADR-0122 does not wait for step 4: the
dependency inventory, the extraction of shared contracts and the removal of the core-to-application
imports in P3 start alongside step 1, and a contract step 1 changes is written in its final layer
the first time. These changes keep public behavior unchanged. Steps 1 to 3 do not need optional
packaging to have shipped, and optional packaging needs only that their shared contracts sit in the
right layer. Step 4 owns the profile machinery and the consolidation that remains; a change to what
the default release installs stays outside this roadmap under D4.

Memory is reached through handlers once step 1 lands, and the memory store stays its own backend
contract. Existing `MemoryStore` implementations keep their three methods (`store`, `retrieve`,
`search`), their return types and their query semantics, and `Tool`-based retrieval keeps invoking
the same backend through the handler adapter. lion1's notes are a different contract: keyed, scoped,
versioned, with exact get, list and delete. They enter as a keyed-access capability beside
`MemoryStore`, not as new required methods on it. One durable record family holds both: canonical
records with an exact index from scoped keys onto them, and search as a projection over those
records. Exact reads never go through ranked search. Keyed access is a small optional
protocol a backend implements, not a capability-negotiation layer, and an existing external store
is never mirrored or replaced to make the family whole. Step 2 fixes key scope and normalization,
identity on overwrite, revision and conflict behavior, deletion including the last key, complete
listing and stale search results; this is a new decision, not something lion1's notes already
provide. Keyed values may have a narrower serialization contract than `MemoryItem.content` without
narrowing existing items. A store without keyed support is still a valid
`MemoryStore`; asking it for keyed access raises an unsupported-capability error rather than
approximating keys with search. Nothing in this migration adds automatic recall, automatic
writeback or a new mandatory method. The change is recorded as an amendment to ADR-0092, read with
ADR-0093's fidelity contract, inside step 2.

#### What each step's record must settle

A read of each lionagi area against lion1 at `14c7a358` found, for every step, places where the step
is harder than its one-line description. Each step's record answers the items below; none is left to
implementation.

**Step 1.**

- "One handler mechanism" means one registration vocabulary over the two kernels ADR-0120 decides:
  an ordered interception chain and a fan-out. It does not mean one callback signature for every
  family. Commands, interceptors, observers and durable delivery keep distinct contracts: an
  interceptor can stop work, an observer records a fact, and durable delivery writes committed
  state. Users register through one small typed facade with three kinds (command, interception,
  observation), each returning a handle that removes it. The facade extends `ActionManager`, which
  ADR-0121 D2 already makes the registry and resolver for actions, and delegates every other kind
  to its existing scoped owner; there is no second authoritative registry. Its public spelling and
  handle shape are step 1's decision. A user registration cannot select or reorder the security
  stages.
- An observer cannot grant authority or veto a command. That is not the same as never blocking:
  today `HookBus.emit` awaits each handler in turn and `SessionObserver.emit` awaits the awaitables
  its subscribers return, so a slow subscriber delays the emitter. Each family's completion,
  failure, cancellation and ordering behavior stays as it is, including the hook bus's `StopHook`.
  Running an observer detached is an explicit, bounded policy with defined teardown and overflow,
  never what a subscription means by default.
- A tool is a handler: a name, a typed request, a function, the privileges it requires and its
  effect (read, write or pure). `Tool`, `FuncTool`, `FunctionCalling`, MCP tools and plugin tools
  register through adapters. The declared effect informs whether one turn's action requests may
  run concurrently; today the batch strategy decides alone (`operations/act/act.py`).
- The interception families that compile into the chain are the blocking hook points, the
  `ActionManager` pre and post hooks, the `AgentSpec` processors and the service `HookRegistry`.
  Each keeps its facade and its denial type.
- The families that compile into the fan-out are `SessionObserver` subscribers and terminal
  callbacks; the hook bus's observational points join them only under the ADR-0120 amendment
  described below. Every family keeps its own concurrency contract, pinned by characterization:
  - hook-bus handlers run in sequence;
  - observer callbacks run sync callbacks inline, then gather the awaitables;
  - terminal callbacks share one deadline;
  - service hooks own stream teardown.
- Live message persistence is not a handler. It keeps one state-owned ordered writer with its
  retry queue and teardown flush; the `MESSAGE_ADD` hook point becomes an adapter onto it. A
  best-effort fan-out would change which rows survive a partial database failure.
- A `Branch` owns the effective authority of the commands the library runs for it. The executor
  ADR-0121 D1 names captures that authority and the resolved policy for each attempt, checks the
  handler's requirements and any argument-dependent policy before user interceptors run, and checks
  again after any of them changes the arguments. `PermissionPolicy`, `Session.gate` and
  `AgentSpec.permissions` stay as policy inputs, carried without loss: `PermissionPolicy` judges
  arguments and can escalate asynchronously, which a set of privilege strings cannot express. A
  hard deny wins; the other kinds of denial keep ADR-0121's closed reducer rather than all becoming
  a deny; and a child never holds more authority than its parent.
- Today only the act path checks Branch authority: `operations/act/act.py` calls
  `branch.authorize` before the manager, and a direct call to `ActionManager.invoke` or to
  `FunctionCalling` runs without that check (manager pre-hooks, plugin trust and a Tool's
  preprocessing and argument validation can still refuse it).
  Both delegate to the same executor, including plugin tools resolved at call time (each call
  builds a fresh `Tool`, so a hook attached to tools at construction misses them) and MCP tools the
  library invokes. A standalone call keeps its historical behavior through an explicit compatibility
  context; a context that was restricted is never replaced by an unrestricted fallback, and a
  missing executor where one is required is a configuration error.
- Tools that run inside a provider's own process are outside this per-command check. Provider
  launch controls and capability reports describe what is enforced there; a stream event is not
  evidence that a call was authorized before it ran. A required provider control that cannot be
  enforced stops the launch. For an ordinary in-process callable the check is admission, not
  containment of its effects.
- Before step 1's first behavior-changing change, its record defines policy defaults, composition,
  capture and narrowing (an omitted policy, today's behavior, is distinct from an explicitly empty
  privilege set); the binding of every invocation facade to its executor context, never inferred
  from a global or from holding a bare callable; callback scopes and compatibility profiles; and the
  execution profiles the step supports, with their outcome, evidence and commit obligations
  (ADR-0121 D4, D10). A deferred profile is named and cannot silently fall back to a weaker one.
  Until that record is accepted, step 1's issues are characterization tests and the record itself.
- The step-1 record settles, in this order: the replaced entry points and their consumers, with
  each one's final layer under ADR-0122; the supported execution profiles; policy composition and
  binding; the invocation order (resolve, normalize, security, user rewrite, revalidation and
  security recheck, invoke), with cancellation, retry identity and ambiguous outcomes; the defaults
  for undeclared effects and requirements; a closed adapter matrix per callback family; where
  persistence and denial evidence are written, per profile; and the bounded change units with
  their characterization fixtures.
- The facade does not move ownership. ADR-0047 D1 assigns hooks per scope: the hook bus to a
  Session, the service hook registry to one `iModel`, Tool hooks to one Tool invocation. A shared
  Session or a shared model keeps one set of hooks; nothing is copied into each Branch, and a
  standalone Session or `iModel` keeps working without one.
  ADR-0120 gives the hook bus's observational points an interceptor profile (sequential, `StopHook`
  ends the chain); they keep that profile, or step 1's record amends ADR-0120 to move them to the
  fan-out with the same observable behavior.
- A handler's declared effect has a compatibility default. Today the caller's batch strategy decides
  whether one turn's action requests run concurrently, and lion1 defaults a handler's effect to
  write. A tool registered without a declared effect keeps today's batching; nothing serializes a
  legacy concurrent batch silently, and no privilege is inferred from a function's name.
- MCP transport admission, plugin eligibility and external-hook trust stay independent of Branch
  authority, each at its existing connection, resolution, activation or invocation boundary. They
  are not registration-time verdicts: plugin activation revalidates trust on every call, and an
  imported hook checks its trusted executable on every invocation, so a change of trust or content
  refuses the next call.
- Order is a security property. The compiled plan runs security interceptors before user
  interceptors and revalidates after any interceptor rewrites arguments (ADR-0121 D4); flattening
  the stages lets a user hook change arguments after the security decision.
- Admission outcomes keep ADR-0121's typed form: allow, deny, defer and escalate, with the
  attempt's identity and the changed-argument checks (ADR-0121 D3, D4, D10). A refused
  command returns to the model as a result with a reason, the shape lion1's `Result` has; that is
  what the model sees, not the whole contract. Today the shape of a denial depends on where it came
  from:
  - an observer denial becomes an action response;
  - a blocking hook point raises;
  - a manager pre-hook records a failed call;
  - a processor raises a governance error.

  Each adapter maps its old shape onto the typed outcome and keeps the old shape observable (D4).
- `SessionObserver.emit` awaits the gate, appends the event to its in-memory flow whatever the
  verdict, and on denial skips routes and subscribers, so bound persistence never sees a denied
  event. The record decides on purpose whether a denied command is durably audited, rather than
  inheriting that order.

**Step 2.**

- One request compiler. Two exist today. `_prepare_run_kwargs` serves `chat` and `run`.
  `prepare_messages_for_chat` is exported and tested, but no production path calls it, and its
  behavior differs: it can aggregate action rounds, and without that option it skips action
  requests. The record makes one compiler, the target of ADR-0007; the exported helper becomes a
  wrapper over it, with each of its options mapped onto it. Every path that calls a model goes through
  that compiler: `chat`, `run`, `operate`, the reformat retry in `parse`, `interpret` and `ReAct`.
- Directives need an addressable record, and it does not go into the message stream. lion1's hide,
  show and summarize pin their targets, record the sequence at which they take effect, and resolve
  directives at the same sequence in a fixed order; replay is by that ordinal, not by timestamp. A
  Branch's history is a UUID-keyed mutable collection with an optional progression that selects
  what the model sees, and the stored messages are referenced by progressions rather than owned by
  one Branch, so a sequence column on a shared message cannot place it in several histories.
  Studio reads the message rows that branch progressions select and tails that projection by
  creation time, message id and branch id. Directives stay outside that public projection, and its
  counts, previews and cursor behavior do not change.
- Step 2 therefore adds an internal append-only conversation record that a Branch uses before any
  `Agent` exists. Its entries have a stable conversation identity and sequence, immutable message
  payloads (or references to immutable payload versions) and typed directive entries that keep
  their effective sequence, resolved targets, summary text and precedence. Folding changes the
  model's view, never the historical rows. The smallest form that meets this is an immutable
  snapshot of a conversation revision plus ordered directive and membership deltas, with content
  versions only where an edit requires them; this is not a general event-sourcing layer, and table
  count, payload layout and baseline encoding are step 2's decision. In the state profile the
  record is stored by the state store beside, not inside, the public tables; sessions, messages and
  progressions keep their read semantics, message UUIDs, progression order, system-message placement
  and reader cursors. A message write and its record entry commit
  in one transaction through the state writer, and control entries never enter a progression.
  Existing messages seed a versioned baseline in their progression order, which does not claim to
  reconstruct edits made before it; later edits, removals and view changes are record events or a
  documented snapshot boundary. Cloned branches, shared messages and continuations each define how
  positions map. The current progression stays the default view and becomes an adapter. Step 5's
  internal actor owns this record through its Branches; there is never a second one.
- Hiding is not erasure. Retention and deletion of data are explicit, and immutable payload copies
  carry their own storage and erasure obligations.
- Three budgets stay distinct:
  - the context-provider injection budget (2,000 tokens today);
  - the view budget, at which older results fold;
  - a run's total context bound.

  lion1 estimates the view by characters and re-anchors on the prompt tokens the provider reports;
  lionagi estimates with a tokenizer before the call. The record says which estimate drives folding,
  and the run reports both.
- The compiler folds one captured revision of the record, taken before any context provider or
  await can change it, and each model call records which revision and view policy produced it. A
  snapshot alone does not prevent duplicate signal drains, model overrides leaking between calls or
  context providers re-entering, so ADR-0018 D2's same-Branch serialization and nested-scope rules
  hold as well; the request stays call-local (ADR-0007), and ADR-0008 is amended in this step.
- Context providers keep their gather count and order, their source attribution, their injection
  budget and today's behavior when no system message exists to render into
  (`operations/chat/_prepare.py`). Memory reached through handlers never turns opt-in retrieval
  into automatic prompt injection or into durable message content.
- Context commands the model can issue (hide, show, summarize) are handlers under step 1's
  privilege check; putting them in prompt text grants nothing.

**Step 3.**

- Today LNDL is chosen in two ways, and they return different things:
  - passing an `Operable` as `response_format` returns the extracted LNDL text, unparsed;
  - `middle=lndl_middle` parses, assembles, runs actions and validates.

  The record makes the format one explicit, named choice on `operate` and `communicate`, with JSON
  as the default. Both old spellings stay as adapters with their current return types.
- One parsing engine. The two readers accept different languages, and neither contains the other.
  lion1's has the better structure: its scanner respects strings, nested tags and heredocs, where
  lionagi extracts tag bodies with non-greedy regular expressions. But lion1's is not the stricter
  reader. It turns a malformed header or an unclosed tag into prose and a recorded problem, drops a
  later declaration of a duplicate alias, reads a non-call action as a value, and repairs newlines,
  closers and commas in calls. lionagi raises a parse error on a duplicate alias or an unclosed tag,
  and its normalizer repairs several forms.
- Step 3 uses lion1's quote-aware scanner and literal-call reader as the base of one engine,
  without lion1's statement evaluator. lionagi's parser, normalizer, AST and operation APIs stay as
  facades over that engine under an explicit legacy profile; the new format option has its own
  documented profile. A profile is a closed, internal set of syntax and error policies that each
  facade selects, not a user-configurable policy language and never a second parser tried on
  failure; no parser runs actions. Public profile names are step 3's decision.
- The legacy profile preserves what lionagi accepts, what it rejects, the AST shape and binding,
  the error types and which actions are reachable. That includes fence extraction and joining,
  the normalizer's repairs (opt-in exactly where it is opt-in today), two-token headers and alias
  defaults, bare and nested OUT forms, partial results at end of input, termination at the first
  OUT, and the existing rejections: duplicate aliases, unclosed declaration tags, non-finite numeric
  literals in OUT and the OUT-list nesting limit. The standalone value and call-literal helpers keep
  their own acceptance and error behavior, and the profile covers the public lexer and parser
  construction, not only which strings are accepted. Taking lion1's tolerances wholesale would break compatibility as surely as dropping
  lionagi's. Diagnostics tell repaired input, ignored prose, refused constructs and a failed round
  apart. Before the engine replaces either reader, a table-driven corpus runs against both and the
  differences are each named.
- ADR-0024 and ADR-0051 are amended together in this step. Continuation rounds keep executing their
  valid actions, a success candidate executes only the actions its output reaches (ADR-0024 D4),
  and both go through the authoritative action path from step 1.
- lion1's program language (statements and control flow) is not part of this step.
- The two `Operable` types are different concepts. lionagi's is an ordered set of field specs that
  builds a model; lion1's is a registry of named command and output schemas. lion1's registry maps
  onto the handler library (steps 1 and 5), and lionagi's `Operable` keeps its meaning.

**Step 4.**

- Removing duplication inside the library is worth an estimated 1,400 to 2,400 lines, and steps 1
  to 3 remove part of it themselves (Appendix A). Step 4 prepares optional profiles for Studio
  (31,622 lines), the CLI (23,177), the state store and the test helpers: each profile declares its
  dependencies, and the default import stops loading their modules. The default installation keeps
  its current dependencies and capabilities (D4), and feature source stays in the one wheel unless
  a separate distribution decision under ADR-0122 D7 splits it. The step reports four numbers
  separately: source lines, wheel bytes, installed dependency footprint and modules imported.
- The profile machinery lands only after the import, feature-loader and compatibility gates pass,
  with the core-to-application imports in P3 already removed (they start with step 1). ADR-0122's
  feature manifest, deterministic loader, settings separation and fresh wheel checks per profile are
  part of those gates; moving the four listed import families does not by itself remove the
  existing transitive cycles. `toml` and YAML have callers in CLI, provider and profile code, so
  those dependencies move only with their callers.
- For this migration D4 governs over ADR-0122 D8 and its facade-removal phase: neither a
  deprecation window nor the existence of an optional profile authorizes removing an existing public
  capability, and no supported default call gains a new `MissingFeatureError` under this roadmap. A
  default-profile release that changes those capabilities is a separate, versioned compatibility
  decision outside this roadmap; this record decides neither whether nor when it happens.
- Concepts that share a name but not a contract are not merged:
  - the async `Event` primitive and the durable `Event` record;
  - Python hashing, content digests and cache keys;
  - the `to_dict` conversions;
  - the Studio scheduler and the dispatch outbox;
  - HTTP and CLI retry, beyond one shared policy for retry decisions and usage accounting.

**Step 5.**

- One Branch builder. Three paths build a working Branch today: `agent.create_agent` from an
  `AgentSpec`, the Studio operator engine, and the CLI fan-out worker. `Agent` and the three paths
  call one internal builder; `Agent` and `create_agent` never call each other. `create_agent` stays
  async and keeps returning a `Branch` with its current options, because engines put that Branch in
  a Session, orchestration patterns hold `dict[str, Branch]`, and CLI workers attach persistence to
  it. A caller that builds from a raw prompt selects that mode explicitly and gets no Role grants,
  settings or MCP discovery it did not get before (ADR-0041 D1, D2, D4).
- `AgentSpec` stays the declarative configuration and is not turned into a live object. `Agent` is
  a small runtime facade over it: `Agent(...)` normalizes its arguments into an `AgentSpec` plus
  typed handler registrations, and `Agent.from_spec(spec)` takes the same path. Constructing an
  `Agent` makes no model call and opens no store; the first awaited run materializes it and checks
  the requested capabilities before any work.
- `Agent.run` is one bounded awaited operation; `Branch.run` keeps its streaming async-generator
  contract. Each run names its conversation or continuation. Independent concurrent runs have
  isolated turn state and captured policy; resuming one conversation twice at once is serialized or
  refused. The record fixes the typed run outcome (output, refusal, exhaustion, cancellation, usage)
  and whether `run` returns the output or that outcome, along with disposal and cancellation.
- Two functions are both named `create_agent`. `lionagi.studio.services.agents.create_agent` writes
  a profile file; `lionagi.agent.create_agent` returns a Branch. Studio keeps profile persistence,
  and one converter turns a stored profile into an `AgentSpec`.
- `iModel` gains a typed call result. lion1's backend returns text together with prompt tokens,
  cost and refusal, and for CLI providers it can plan a session restart when the view changes: how
  many hidden tokens a restart frees and when that pays back. `iModel` returns none of these as a
  typed result. The result is an adapter over today's event and chunk metadata. Session planning is
  a policy on CLI providers; a session id does not replace it.

**Step 6.**

- lion1's serve is a small offer the owner fixes:
  - the owner sets the profile, the model and a request template;
  - a client sends inputs, with bounds at or below the owner's;
  - the server accepts loopback peers only and checks Host and Origin.

  Studio's API is a broad operator and introspection surface. The two are complementary. Serve does
  not replace Studio routes, and Studio's API does not become the serve contract.
- Studio starts, follows and stops served agents through the daemon's versioned application
  contract (ADR-0078 D1 and D5, proposed). The daemon uses a serving port, and each served agent
  still enforces its own offer, bounds and transport policy. The daemon never forwards its operator
  authority or bearer credential to a destination a client supplies; the service owner registers and
  validates agent destinations. Compatible local clients can use the bounded serving protocol
  directly, and it exposes none of Studio's operator routes. Letting the Studio frontend talk to
  agents directly is outside this step: it would widen the boundary ADR-0078 sets, and the serve
  guard today accepts only its own address as Origin, so it needs its own amendment covering origin,
  authentication, credentials and lifecycle.
- Neither loopback binding nor an optional bearer secret proves who the user is. Local transport is
  not multi-user authentication.
- The step's record names the optional profile that carries serving and its dependencies. FastAPI
  is today a dependency of the `studio` extra, which `all` includes, and not of the default install;
  serving adds no dependency to the default install (D4).

**Step 7.**

- Two existing mechanisms are not this capability:
  - a schedule (ADR-0070) starts work from a trigger; today a fire launches its child directly with
    no lease, and ADR-0072 specifies the move to leased, queued admission;
  - the dispatch outbox (ADR-0059) delivers messages with retry and optional acknowledgement, and a
    successful notify is not proof that the consumer received or committed anything (ADR-0059 P5).

  Neither is a persistent identity that wakes on its inbox. The record adds that identity, and the
  state store carries its inbox. A schedule may start such an agent.
- The inbox has a durable recipient identity, sender admission, an exclusive wake claim, a consumed
  cursor, an idempotent identity per delivered input, backpressure, shutdown and recovery. Advancing
  the cursor, accepting the input and continuing the wake commit at one specified boundary.
  Delivery acknowledgement and a successful process launch do not prove that the agent consumed or
  committed a message, and nothing here promises exactly-once external effects.
- Each wake continues the agent's record, so without a fold point the record grows without bound.
  lion1 refuses to serve such an agent against a real store for exactly this reason. Persisted
  hiding is not enough: lion1's fold still walks every entry, and its store loads the whole
  continuation chain. Before long-running agents ship, a restart begins from a durable checkpoint
  plus a bounded tail, with a versioned folded state, stable alias and sequence mapping, a defined
  answer to which hidden payloads stay reopenable, and an explicit audit-retention policy. The
  record also decides how a checkpoint is installed atomically, how an interrupted installation
  recovers, and how archived history before a checkpoint stays readable. The restart bound covers
  all the metadata needed to rebuild the live state, not only the visible message tail. Keeping
  every hidden payload immediately reopenable and keeping unbounded audit history may need archive
  reads outside the bounded restart path; step 7's record decides that behavior, and a checkpoint
  does not make the tradeoff disappear. One versioned checkpoint value with an atomic active
  reference and a bounded tail is enough; no pluggable checkpoint subsystem is needed now.

### D3: `Agent` is public; the actor is internal

The public type a user builds is `Agent`. The actor is how an agent is implemented and is not part
of the public API: no public name contains "actor".

The boundary this record fixes, and step 5's record maps in detail:

- `Branch` stays the conversation: messages, the model, the handlers registered on it and the
  privileges it runs under (step 1).
- The internal actor holds what a conversation does not: a name, the privileges an agent may grant
  its runs, its handler library, an inbox, and a store. It owns branches; a run executes on one.
- What lion1 calls a profile (which handlers the model is told about, which privileges a run gets,
  the role's prose, default bounds) and what lionagi calls `AgentSpec` are one concept; step 5's
  record merges them into `AgentSpec`, which stays declarative. `Agent` is the runtime facade over
  it and has no second configuration schema.
- `Session` is where agents meet: a shared event fan-out, delivery between inboxes, and operation
  graphs that call agents.
- lion1's `Backend` (a function from messages to text) is replaced by `iModel`; lion1's record maps
  onto step 2's internal conversation record, and its store onto the SQLite store (D5).

The target shape of a first program, which step 5's record makes exact:

```python
from lionagi import Agent

agent = Agent("reviewer", model="openai/gpt-4.1-mini", tools=[read_file, run_tests],
              privileges={"fs.read"})
verdict = await agent.run("review this diff", output=Verdict, max_turns=20, cost_budget=1.0)
```

`read_file`, `run_tests` and `Verdict` stand for an application's own tools and output model; a
published example defines them or uses built-in ones. A function's name says nothing trustworthy
about the privileges or effects it needs: step 1 decides the defaults for a plain callable and how
an explicit registration declares them.

Code written against `Branch` and `Session` today keeps working unchanged (D4).

Why this way: "agent" is the word users already search for and the word lionagi's `AgentSpec` and
`create_agent` use. lion1's distinction between an actor (who acts, with what authority) and a
profile (how the model is configured for one run) is real and is kept; it is a distinction inside
the implementation, not one every user must learn first.

### D4: compatibility through adapters

This migration preserves every public import, every supported call shape, the return types and the
observable behavior of existing lionagi programs. New APIs are additive. An existing name whose
implementation moves becomes an adapter over the new mechanism and stays supported; it emits no
`DeprecationWarning` merely because it now delegates. Removing a lionagi
public name or behavior, or changing what a default install can do, is not authorized by this
record: each such change needs its own explicitly versioned compatibility decision.

Exact semantics:

- An adapter preserves the observable behavior its characterization tests pin (D9), including
  ordering, concurrency, failure behavior, error types and the rows written to the store.
- Where the new mechanism cannot reproduce an old behavior, the adapter keeps the old behavior; if
  the step's record concludes it cannot, the record names the behavior and the change waits for its
  own compatibility decision. An adapter never changes behavior silently.
- Contributors' code built on today's seams (a tool built on `Tool`, a store implementing
  `MemoryStore`) keeps working without being rewritten against handlers.

### D5: storage

The default store for run records, notes and durable memory is lionagi's own SQLite store (the
`state` package, through `aiosqlite` and SQLAlchemy, both already dependencies). External stores are
adapters behind lionagi's store and memory contracts and satisfy ADR-0093's fidelity contract. lionagi
builds and ships no store binary.

Exact semantics:

- Run records, sessions, calls and notes that lion1 writes through its store client are written to
  the state store's tables; step 2 (notes) and step 5 (runs) name the tables and any new columns,
  under the state store's own schema versioning.
- A long-running agent's inbox (step 7) is carried by the state store's existing message and
  dispatch machinery; no external messaging service is required to run one.
- An external store or messaging service is reached only through an adapter the user installs and
  configures; the default install never contacts one.
- A `Branch` with no requested persistence keeps today's in-memory behavior. An `Agent` defaults to
  a documented ephemeral mode for bounded one-shot use; a name is not a durable identity, and an
  ephemeral result says that no restart recovery exists.
- Persistence, resume, durable notes and inboxes are requested explicitly. A request activates the
  configured persistent adapter, and lionagi's SQLite store when none was supplied. Installing a
  package never changes an existing object's storage, and never attaches an ephemeral `Agent` to a
  database (ADR-0122 D3 and its rejected alternative of inferring features from installed packages;
  ADR-0092 D4, D5).
- Under ADR-0122 the state store belongs to an optional profile. If SQLite persistence is requested
  and that profile is not installed, construction or materialization raises `MissingFeatureError`
  naming the profile, the missing dependency and the extra, before any model call, tool effect or
  accepted input. There is no in-memory fallback. A configured external adapter can satisfy the
  request without SQLite. Inside this roadmap this error cannot occur on a default install, because
  the default install keeps its current capabilities, the state store among them (this record's D4
  and D5, and step 4); it applies only to an install that leaves the state profile out, and it is
  not a new error on any default call.
- An unavailable store, a schema newer than the code, and a resume handle that is not found are
  distinct errors, and none of them starts a fresh empty continuation. A selected SQLite store goes
  through the state store's own migrations and keeps its direct readers' contracts. Persistent mode
  has a stable store location, a durable agent and conversation identity, and an explicit resume
  handle. A long-running agent without durable identity and storage is a configuration error.
- A memory store the application supplied stays the application's: the first claim wins when a
  Branch joins or moves between Sessions (ADR-0092 D4), and closing or retrying an external adapter
  stays with the application and the adapter (ADR-0093 D4).

Why this way: lion1's core uses its store service as a database (P6). Shipping a native binary per
platform to provide a database would add a Rust build, four platform wheels and a release
coordination with the binary's source for a capability SQLite already gives the library. Users who
want the service's other capabilities (messaging between agents, task queues, cross-session memory
with ranking and feedback, a hosted store) connect it as an adapter.

### D6: Python 3.10

Ported code runs on Python 3.10, lionagi's declared floor. lion1 compiles under the 3.10 parser
today (checked with a 3.10 interpreter over `src/`, `hub/` and `apps/`, with a 3.11-only control
file failing in the same run); what it needs from 3.11 is library, not syntax: `tomllib`,
`datetime.UTC`, `asyncio.timeout` and `asyncio.timeout_at`, `BaseException.add_note`, and
`datetime.fromisoformat` accepting a `Z` suffix (four call sites parse timestamps without
replacing the suffix). Ported code uses, respectively, the `toml` package lionagi already depends
on, `timezone.utc`, anyio's cancel scopes, an explicit message, and a normalizing parse, so the
default install gains no dependency (D4).

Why this way: raising the floor is a breaking change for every user on 3.10; Python 3.10 reaches
end of life this month, and the floor can rise in its own release later. Lowering the ported code is
mechanical and touches about twenty files.

### D7: types

Every public function, method and model in ported code is fully annotated; handler requests and
results are pydantic models or frozen dataclasses with annotated fields; `Any` does not appear on a
public signature except where the value is genuinely arbitrary user data, and then it is named in the
docstring. The type checker configured for the repository runs over every ported module and must
pass.

### D8: one set of design records

The repository ends with one design corpus, `docs/adr/`. lion1's records migrate into it as the
steps land: a step's record restates, in full, every lion1 decision it adopts (its claims, the
mechanism, the reason and the alternatives lion1 rejected), so nothing is lost when the lion1 record
goes. lion1's code and records stay in place until the last step that draws on them has landed, and
then leave together in the one change that removes `lion1/` (D1).

Exact semantics:

- Until a lion1 record is migrated, any reference to it is written "lion1 ADR-NNNN"; a bare
  "ADR-NNNN" always means this corpus.
- A lion1 decision a step rejects is recorded in that step's record under Alternatives considered,
  with the reason, before the lion1 record is deleted.
- A lion1 record no step adopts (its deployment-specific records: the desk, chores, the instrument,
  process isolation) is not migrated; it leaves with the code it describes.

Why this way: two corpora with overlapping numbers would leave a reader two answers for one
question, and a reference that resolves against the wrong series. Restating rather than linking
keeps each lionagi record readable on its own after `lion1/` is gone.

### D9: landing discipline

Each step lands as a series of changes in this order:

1. Characterization: tests that pin the behavior of every API the step replaces, written against
   the code as it is, passing before any change.
2. The new mechanism and the adapters, with the characterization tests unchanged and passing.
3. Callers inside lionagi moved to the new mechanism.

Removing an adapter is not part of any step; it needs its own compatibility decision (D4).

Every change in a step reports the lines it added and removed in `lionagi/`. A change that touches a
path writing to the store shows the rows written before and after for the same inputs and requires
them identical, or names each difference and the record that allows it.

## Consequences

- Steps 1 to 4 consolidate the mechanisms assigned to them, so the concepts in P1 that those steps
  cover have one owner in `lionagi/` by the end of step 4. `lion1/` stays until the last step that
  draws on it (step 7) has landed, and is then removed.
- The open records ADR-0120, ADR-0121, ADR-0044, ADR-0007 and ADR-0018 gain an implementation
  source; where lion1's shape differs from the record, the step's amendment decides, so those
  records will change.
- Steps 1 to 3 change internal call paths every lionagi operation uses. Their risk is in behavior
  the characterization tests do not pin; D9's first change is where that risk is paid.
- Contributors whose open changes build on `Tool`, `MemoryStore` or the hook families keep working
  unchanged; handlers are offered to them, not required.
- Step 4 reports dependency, import and wheel measurements separately. Its profiles cut the import
  cost of the default path; a reduction in what the default install carries or can do is not a
  completion condition of this roadmap and needs its own decision (D4). Removing duplication inside
  the library alone would never solve the size problem (P3): most of what a default user carries is
  application code and its dependencies. The wheel's source bytes shrink only if a later
  distribution decision splits it.
- A user who wants the external store's services installs and configures an adapter; nothing in the
  default install reaches a service.
- Reversal cost: D1 and D3 are cheap to reverse until step 5 ships a public `Agent`; D5 is cheap to
  reverse at any point because external stores are adapters; D4 is a promise to users, and any
  later removal goes through its own decision.

## Alternatives considered

- **Move lion1 into a `lionagi.beta` package in one change.** It would have made lion1 importable
  from the published package at once. It lost because it ships two runtimes under one name for every
  concept in P1, and moves every user twice (onto beta names, then off them).
- **lion1 replaces lionagi as its next major version.** It would have given a clean API with no
  adapters. It lost because lionagi's users, Studio, the CLI and the MCP server all build on
  `Branch` and `Session`; a replacement abandons them or forks maintenance.
- **Keep both runtimes and connect them with adapters both ways.** It lost on P1: two of everything
  indefinitely, with adapters as a third.
- **Ship the store service as a binary wheel per platform and keep lion1's store client.** It would
  have kept lion1's storage code unchanged. It lost on D5: a Rust build and four platform wheels to
  provide a database the library already has.
- **Bind the store service in process through a native extension.** It lost for the same reason,
  and because the service is designed as a single writer process; embedding it would put its
  runtime inside every Python process and make a crash in it a crash of the caller.
- **Adopt an external vector store as the default store.** It lost because a run's records are
  written once per event and need transactions; the candidate's durable format rewrites a whole
  file per mutation. Vector stores enter as memory adapters under ADR-0093.
- **Design step 1 fresh, ignoring ADR-0120 and ADR-0121.** It lost because those records already
  carry the analysis of today's fifteen dispatch contracts and three enforcement layers; step 1
  amends them where lion1's working shape is simpler.
- **Raise the Python floor to 3.11 with the first port.** It lost on D6: a breaking change bundled
  with an unrelated one; the floor rises on its own.
- **One callback signature for tools, hooks, observers and message persistence.** It would have
  given one thing to learn. It lost because those families differ in what a callback may do. One
  stops work, one records a fact, one writes committed state with ordered retries, and one runs a
  validated request. A shared signature either erases those guarantees or reintroduces them as
  flags (step 1; ADR-0120 rejects a universal semantic bus for the same reason).
- **Merge the two LNDL grammars by accepting the union of what both parsers accept.** It would have
  kept every input working. It lost because a union silently gives existing input new meanings and
  can make new commands reachable, and because it discards lionagi's rejections, which are part of
  its contract. Step 3 runs one engine with explicit profiles instead.
- **Replace lionagi's LNDL input handling with lion1's reader as it is.** It would have brought the
  better scanner in one move. It lost because lion1 tolerates input lionagi rejects and binds some
  headers differently, so existing programs would change meaning. Keeping lionagi's regular
  expression parser as the base lost too, because it keeps the ambiguous construct boundaries the
  scanner fixes.
- **Replace Studio's API with the serve protocol, or serve agents through Studio's API.** Either
  would have left one HTTP surface. It lost because the two have different principals: the serve
  protocol is a bounded offer an owner publishes to clients, and Studio is the owner's own control
  surface. Merging them either exposes owner controls to clients or limits the owner to client
  bounds (step 6).
- **Build long-running agents on the scheduler or the dispatch outbox.** It would have reused
  working machinery. It lost because a schedule starts work and the outbox delivers a message;
  neither holds a persistent identity, a cursor or a fold point. Step 7 uses both as parts and adds
  the identity.
- **Keep every existing registration API and only share the loops beneath them.** It would have
  changed the least. It lost because it leaves a new user with all of today's registration
  vocabularies to learn; step 1 adds one typed registration facade over the existing owners and
  compiles the old APIs into it. A new universal registry holding copies of Session and model hooks
  lost too: it duplicates ownership and makes shared models and reparenting hazardous.
- **Expose directives as conversation messages.** It would have reused the existing message
  machinery. It lost because a directive in a legacy progression becomes a visible turn and changes
  what existing readers count and tail, and a sequence on a message shared between branches cannot
  place it in several histories. A side table of directives that points at mutable current messages
  lost too: replaying it would change old views.
- **Make `AgentSpec` the `Agent`, or have `create_agent` return an `Agent`.** Either would have
  avoided a new class. It lost because declarative configuration cannot own a run's lifecycle
  without losing its no-I/O guarantee, and because callers of `create_agent` hold, include and
  iterate a `Branch`. `Agent` composes the spec and the shared builder instead.
- **Extend `MemoryStore` with keyed methods.** It would have put notes and memory behind one
  protocol. It lost because every existing adapter, including vector-store backends, would stop
  conforming. Keyed access is a companion capability, and keys are never approximated by search.
- **Fall back to memory when a requested store is unavailable, or turn persistence on when the state
  profile is installed.** Either keeps calls succeeding. It lost because a call that succeeds
  without the durability it asked for suggests records that do not exist, and because an unrelated
  install would change behavior and identities. Requested durability fails explicitly instead.

## Appendix A: consolidation candidates

Each line is a candidate, not a promised deletion. The estimates are hand counts of the source
spans that would go after an adapter keeps the public name working; new shared code offsets part of
each. The step column names the step whose record decides the candidate.

| Candidate | Step | Estimated lines removed | Public names kept by adapter |
|-----------|------|-------------------------|------------------------------|
| One request compiler for every model call | 2 | 300-450 | `prepare_messages_for_chat`, `MessageManager` methods, Branch operations |
| Interception mechanics shared by hook points, tool hooks, agent processors and the service hook registry | 1 | 100-180 | `HookBus`, `add_tool_pre_hook`, `add_tool_post_hook`, `HooksMixin`, `HookRegistry`, `HookedEvent` |
| Fan-out loops shared by observers, hook points and terminal callbacks | 1 | 100-160 | `Session.observe`, `Session.route`, `HookBus`, `TerminalCallbackRegistry` |
| Permission sources compiled into the dispatch check | 1 | 30-80 | `PermissionPolicy`, `Session.gate`, `AgentSpec.permissions` |
| Live message persistence moved from generic hooks to the state writer | 1 | 40-100 | `route_message_persistence`, `unroute_message_persistence` |
| LNDL extraction route turned into a named format choice | 3 | 10-30 | `response_format=Operable`, `lndl_middle`, `build_lndl_middle` |
| `Structure` and `Operative` over one request and response schema builder | 3 | 100-170 | `Structure`, `JsonStructure`, `Operative`, `Step` |
| `FieldModel` as a wrapper over `Spec` | 4 | 250-320 | `FieldModel`, its module path, `to_spec` |
| One retry decision and usage-accounting policy for HTTP and CLI calls | 4 | 100-180 | `RetryConfig`, `EndpointConfig.max_retries`, `Endpoint.call` |
| CLI process glue moved onto the shared subprocess layer | 4 | 50-100 | the four CLI endpoint classes |
| One UUID conversion | 4 | about 25 | `ln.to_uuid` (already deprecated) |
| The deprecated `CLIEndpoint` alias and its warning stay unchanged; removing it is outside this roadmap (D4) | 4 | 0 | `CLIEndpoint` |
| A typed model-call result on `iModel` | 5 | 80-140 | `iModel`, `APICalling`, `StreamChunk` |
| One Branch builder for agents, the Studio operator and CLI workers | 5 | 150-300 | `create_agent`, `AgentSpec`, operator and worker options |
| One converter from a stored profile to `AgentSpec` | 5 | 50-120 | both `create_agent` functions |
| **Total** | | **about 1,400-2,400** | |

Prepared as optional profiles rather than removed (step 4, ADR-0122): Studio (31,622 lines), the
CLI (23,177) and the test helpers. The state store (11,463) is optional too, with the default noted
under D5. A profile declares their dependencies and keeps their modules off the default import
path; under D4 the default installation keeps its current dependencies, and their source stays in
the wheel unless a later distribution decision splits it (ADR-0122 D7).

Not candidates: the pairs listed under step 4 that share a name but not a contract, and
`OperableModel`. It supports mutation, validators and attribute access that `Operable` does not;
whether those stay supported is its own question, decided before any merge.

## Notes

- The numbers in the Context table come from a static inventory over `lionagi/` at `14c7a358`
  counting non-blank, non-comment lines per top-level package.
- The step obligations and Appendix A come from a read of six areas at the same commit, each
  compared with lion1:
  - tools, hooks and observers;
  - messages and context;
  - LNDL and specs;
  - utilities and the data model;
  - the model service and providers;
  - the application layer.
