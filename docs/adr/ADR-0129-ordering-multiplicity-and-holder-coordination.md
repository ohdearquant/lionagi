# ADR-0129: Ordering multiplicity and holder-side coordination for collections

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: core-data-model
- **Date**: 2026-10-16
- **Relations**: extends ADR-0002 (UUID-keyed ordered collection model), whose D4 and D6 leave the
  two questions decided here open and whose delta rows 3 and 4 carry the code work; revises ADR-0006
  D4, ADR-0004 D5, and ADR-0092 D3, which rely on the collection locks this record removes

## Context

ADR-0002 records what the collection model is today and lists two questions it deliberately does not
decide: what decides a `Progression`'s multiplicity, and where the coordination of concurrent access
lives. Both are targets rather than observations, and the shipped code contradicts both. This record
decides them and hands the code work back to ADR-0002's delta rows, so the retrospective record
keeps describing the code.

**P1 — one ordering class carries two rules and applies them per method.** `Progression`
(`lionagi/protocols/generic/progression.py:139`) is a single class whose duplicate behavior depends
on the method called. `include` (`:270-286`, set-like), `exclude` (`:288-300`, removes every
occurrence), and membership (`:200-205`, "at least once") behave as a set; construction
(`:186-188` returns `deque(validate_order(value))` unchanged, so a repeated id is kept, as the edge
case in `tests/protocols/generic/test_progression_edge_cases.py:58-62` shows), `append`
(`:302-314`), `extend` (`:364-369`), `insert` (`:392-398`), `+=` (`:379-381`), and positional
assignment (`:225-244`) store additional positions for an id already present. A reader cannot tell
from a signature whether a call may duplicate an id, and neither can a caller that holds the object
as `Ordering[T]`.

**P2 — uniqueness holds only where Pile's own code enforces it.** `Pile`'s constructor rejects a
duplicate id in a supplied order (`lionagi/protocols/generic/pile.py:129-138`), `include` adds only
ids absent from the order (`:303-307`), and `_insert` (`:826-835`) and `_setitem` (`:707-747`) raise
`ItemExistsError` for an id already resident. The rule therefore lives in the collection's methods
rather than in the ordering type, and the ordering type's own mutators are free to break the
collection's invariant whenever a caller holds the order directly.

**P3 — a caller-supplied order is aliased, not copied.** `_validate_progression` returns the
`Progression` instance it was given (`pile.py:123-125`, `:138`), so `Pile(order=prog)` installs the
caller's object as the collection's order, and `Element.model_config`
(`lionagi/protocols/generic/element.py:36-41`) neither revalidates nor copies an instance assigned
to a model-typed field. A caller that keeps `prog` can append a second position for a resident id
with `prog.append(uid)` and change the collection's order without touching the collection.

**P4 — every collection type carries locks its holder cannot see the cost of.** `Pile` owns an RLock
and an AnyIO lock (`pile.py:208-209`) and reaches them through three shapes: `@synchronized`
(`lionagi/ln/_utils.py:458-467`) takes the RLock alone, `@async_synchronized` (`:470-499`) takes the
AnyIO lock and then spins on the RLock with a non-blocking acquire and a 0.5 ms sleep, and the
`_both_locks` context manager (`pile.py:555-564`) does the same for an unscoped region. The two
locks therefore do exclude each other: an async method and a `@synchronized` method on the same
`Pile` never run together, in one thread or across threads, and ADR-0002 D6 records the mechanism.
`@synchronized` decorates 19 methods: `__setitem__` (`pile.py:274`), `pop` (`:282`), `include`
(`:303`), `exclude` (`:309`), `clear` (`:323`), `update` (`:328`), `insert` (`:341`), `append`
(`:345`), `get` (`:349`), `keys` (`:358`), `values` (`:362`), `items` (`:366`), `is_empty` (`:370`),
`size` (`:374`), `__getitem__` (`:394`), `__contains__` (`:398`), `__len__` (`:402`), `__bool__`
(`:407`), and `filter` (`:643`). `@async_synchronized` decorates 8: `asetitem` (`:566`), `apop`
(`:575`), `aremove` (`:584`), `ainclude` (`:592`), `aexclude` (`:602`), `aclear` (`:610`), `aupdate`
(`:614`), and `aget` (`:622`). The undecorated helpers `_setitem` (`:707`), `_get` (`:749`), `_pop`
(`:782`), and `_insert` (`:826`) are not lock sites of their own. `Flow` owns a third RLock for its
own methods (`lionagi/protocols/generic/flow.py:36-41`, rebuilt at `:132`). What a per-method lock
still cannot do is make a caller's read-then-act sequence atomic.

**P5 — the holders are invisible in the type, and two sites await inside it.** `Session` holds
`branches`, `MessageManager` holds `messages`, `Exchange` holds `flows`, `DataLogger` holds `logs`,
`Executor` holds its event pile, `InMemoryStore` holds its item `Pile`
(`lionagi/protocols/memory.py:53-82`, shared across a session's branches at
`lionagi/session/session.py:100-103`), and `Flow` holds its named views. Because coordination lives
inside the type, none of those holders states what it protects or for how long. Two call sites hold a
collection lock across an await: `Session.asplit` awaits `Branch.aclone` inside `async with
self.branches` (`lionagi/session/session.py:279-284`), and that callee takes the message pile lock
(`lionagi/session/branch.py:519-521`); `Pile.adapt_to_async` awaits the adapter inside the both-lock
protocol (`pile.py:902-903`).

| Concern | Decision |
|---------|----------|
| Multiplicity | D1: multiplicity is a property of the ordering type; a unique order belongs to a collection and a repeated order is a standalone sequence. |
| Locking | D2: a collection type carries no lock; one holder at a time, an explicit snapshot when several reads must agree, and coordination where the holder lives. |

**Out of scope.** A new persistence format for either ordering type: the serialized Pile keeps its
shape, a polymorphic item list plus a serialized order. A general cross-context linearizability or
transaction contract: D2 removes the interior guarantee rather than strengthening it. Durable queue
or scheduler semantics: a repeated order is an ordering, not an acknowledgement queue. Which holder
serializes first when two holders share one collection: this record names the holder rule, and each
holder's own record decides its schedule. The order in which the code changes land: ADR-0002's delta
rows state the deliverables and their acceptance.

## Decision

### D1 — multiplicity is a property of the ordering type

A `Progression`'s multiplicity is fixed by its type, and two types state the two rules.

**The contract.** Two ordering types, two rules:

| Type | Rule |
|------|------|
| Unique order (the order a collection installs) | Holds exactly the store's ids, one position each. Construction and every mutator reject a second position for an id that is already there. |
| Repeated order (a standalone sequence) | Holds every stored position. `append`, `extend`, `insert`, `+=`, and positional assignment store another position for an id already present. |

```text
unique order
  construct([uids]) -> rejects a supplied uid repeated in the sequence
  append(uid)     -> rejects a resident uid
  extend([uids])  -> rejects a resident uid
  insert(i, uid)  -> rejects a resident uid
  order[i] = uid  -> rejects a resident uid
  include(uid)    -> set-like, no second position
  exclude(uid)    -> removes every occurrence
repeated order
  construct([uids]) -> keeps every supplied uid, repeated ones included
  append / extend / insert / += / order[i] = uid -> stores another position
  include(uid)    -> set-like, no second position
  exclude(uid)    -> removes every occurrence
```

The class names are the implementer's to choose; what this decision fixes is that the two rules are
two types, that a collection's default order is the unique type, and that a caller holding one type
is never handed the other by accident.

**Exact semantics**:

- The unique order holds exactly the collection's ids, one position each, as ADR-0002 D1 requires,
  and equality between the store's keys and the order is the type's invariant rather than the
  constructor's check.
- Both construction and every mutator of the unique order reject a second position for a resident id
  with the error the collection raises today (`pile.py:129-138` for construction, `:826-835` and
  `:707-747` for the mutators); construction is named because it is the one path that stores a
  supplied sequence after checking only the collection's own keys.
- The repeated order stores a second position for `append`, `extend`, `insert`, `+=`, and positional
  assignment, and construction, membership, and serialization agree with that rule; this is the
  shipped `Progression` behavior (`progression.py:186-188`, `:302-314`, `:364-369`, `:392-398`,
  `:379-381`, `:225-244`).
- `include` is set-like and `exclude` removes every occurrence on both types, so the two types
  differ only in what a position-storing mutator may do.
- **Pile construction copies the caller's order.** When `order` is an order instance, the collection
  builds its own order from the supplied ids instead of adopting the instance. A caller that keeps
  the ordering it passed to `Pile(order=...)` therefore cannot append a position to the collection's
  order. This is the target that closes P3, and it supersedes `_validate_progression` returning its
  argument (`pile.py:123-125`, `:138`).
- Serialization follows the type: a unique order serializes as one position per id, a repeated order
  keeps repeated ids. Existing serialized standalone sequences already contain repeated ids and stay
  readable; the payload's class name is the ordering type's own change.

**State of the code today.** One `Progression` class carries both rules and applies them per method
(P1); uniqueness holds only where the collection's own methods enforce it (P2); a supplied order is
aliased rather than copied (P3). ADR-0002 delta row 3 carries the work, and ADR-0002 D4's
superseding note records that D4's heading and its summary-table row are superseded.

**Why this way.** The alternatives — a uniqueness flag on one type, keeping the shipped mixed
behavior, and a universal unique order — are compared below.

### D2 — a collection carries no lock, and coordination belongs to the holder

A collection type owns no lock. A `Pile` or a `Flow` has one holder at a time; a holder that needs
several reads to agree takes one snapshot and reads that; coordination between holders is the
holder's, exercised where the holder lives.

**The contract**:

- `Pile` exposes no lock. The private pair (`pile.py:208-209`), the `lock` and `async_lock`
  properties (`:538-547`), the both-lock context manager with its synchronous spin (`:549-564`), and
  the decorators that take them are removed, together with the async entry protocol that exists only
  to hold them (`:857-875`).
- The snapshot value is a **tuple of ids in order**: `Pile.snapshot() -> tuple[UUID, ...]`, read
  once without a lock. It replaces the two informal order snapshots the code takes today
  (`pile.py:378-386` for iteration, `:631-637` for async iteration) and it is the value a caller
  keeps instead of holding the collection open. A caller that also needs items reads them through
  `Pile.__getitem__` on the ids, which is what `keys`, `values`, and `items` do inside the lock
  today (`:358-368`).
- `adump`'s critical sections become plain reads of the ids and the records it is already ordered by
  (`:994-1013`).
- `Flow` drops its RLock (`flow.py:36-41`, `:132`). Its methods — `add_progression` (`:138-151`),
  `remove_progression` (`:159-171`), `get_progression` (`:173-202`), `add_item` (`:206-229`),
  `remove_item` (`:231-240`), `clear` (`:242-247`) — become ordinary methods, and `Flow` is a holder
  of its views like any other holder.
- **The holder rule.** The object that owns a collection serializes access to it; the collection
  type does not, and a caller does not hold the collection across an await. The named holders are:
  - `Session` over `branches` (`lionagi/session/session.py:74-76`, `:279-284`);
  - `MessageManager` over `messages` (`lionagi/protocols/messages/manager.py:73-75`, `:77-90`);
  - `Exchange` over `flows` (`lionagi/session/exchange.py:87-110`);
  - `DataLogger` over `logs` (`lionagi/protocols/generic/log.py:244-247`, `:291-325`);
  - `Executor` over its event pile and pending order
    (`lionagi/protocols/generic/processor.py:199-204`);
  - `InMemoryStore` over its item `Pile` (`memory.py:53-82`), which owns no lock today and relies
    on `Pile.ainclude`/`aget`; under D2 it takes its own lock for `store` and `retrieve`;
  - `Flow` over its named views (`flow.py:138-247`).
- **Four of those sites are synchronous holders, and two sites genuinely wait.** `Session`'s
  `ainclude_branches` (`session.py:74-76`) takes the lock and calls the synchronous
  `include_branches`; `MessageManager.aclear_messages` (`manager.py:73-75`) takes it and calls the
  synchronous `clear_messages`; `Exchange.collect` (`exchange.py:87-110`) pops from the outbox
  inside the lock and runs its `await gather` at `:114` after the lock is released;
  `DataLogger.alog` (`lionagi/protocols/generic/log.py:244-247`) takes it and calls the synchronous
  `log`. These are holders of one collection for the duration of a synchronous call, not waiters.
  The sites that do await while holding a collection lock are `Session.asplit`
  (`session.py:279-284`), whose callee `Branch.aclone` additionally takes the message pile lock
  (`branch.py:519-521`), and `Pile.adapt_to_async` (`pile.py:902-903`). Both move onto the holder
  rule or onto the snapshot, as does `DependencyAwareExecutor._preallocate_all_branches`
  (`lionagi/operations/flow.py:344-362`), which holds `branches.async_lock` around synchronous work
  today.
- **Accepted records this changes.**
  - ADR-0006 D4 (`Accepted`) is the message-added observer contract. Its async path reads "lock pile
    → construct and insert or replace message → release pile lock → snapshot callbacks"
    (`docs/adr/ADR-0006-conversational-message-envelope-and-ordered-history.md:357-363`), which is
    `MessageManager.a_add_message` today (`manager.py:77-90`). The lock in that path is the pile's,
    and its holder `MessageManager` is listed with the holders above and keeps a lock of its own
    around the insertion instead of taking the pile's. ADR-0006 D4's re-entrancy statement —
    callbacks run after the lock is released, so an async callback may re-enter `a_add_message`
    without deadlocking (`:379-381`) — keeps its observable behavior, because callbacks still run
    after the insertion, and restates its reason as the holder's critical section ending before the
    callback snapshot.
  - ADR-0092 D3 (`Accepted`) keeps `InMemoryStore` on a `Pile` and records that `Pile.ainclude()`
    and `aget()` supply the synchronization
    (`docs/adr/ADR-0092-minimal-branch-and-session-memory-store.md:261-264`). With no Pile lock,
    `InMemoryStore` is the holder of that `Pile` and takes its own lock for `store` and `retrieve`,
    the same treatment as `MessageManager`.
  - ADR-0004 D5 (`Accepted`) presumes the Pile locks: its backing Piles "have their own locks, but
    direct Pile mutation is not coordinated with the Graph lock" and strengthening it "requires
    closing direct mutation paths and coordinating Graph and Pile locks"
    (`docs/adr/ADR-0004-directed-graph-structural-invariants.md:341-342`, `:369-370`). With no Pile
    locks, Graph's own RLock is the only lock, and traversal stability belongs to Graph's own
    record.
- **Every named view is one of the two types.** With multiplicity fixed by D1, the views the
  collection records name classify as follows.

| View | Where the code is | Class |
|------|-------------------|-------|
| A collection's default order (`Pile.progression`) | `pile.py:188-191`; uniqueness enforced today at `:129-138`, `:303-307`, `:826-835`, `:707-747` | unique |
| `Flow` views appended by `add_item` | `flow.py:206-229`; ADR-0002 D5 records that repeated calls append the same id repeatedly | repeated |
| `Executor.pending` | `processor.py:199`, `:236` (`pending.include(event)`) | repeated type, used set-like: this call site stores at most one position per id |
| `Executor.pile` | `processor.py:201-204` | unique |
| `MessageManager.messages` | `manager.py:47-52` | unique |
| `MessageManager` typed views (`assistant_responses`, `actions`, `action_requests`, `action_responses`, `instructions`) | `manager.py:453-490` | unique: each access builds a fresh filtered Pile (`pile.py:1015-1053`) whose own default order is a unique order |
| `Branch.progression`, both forms | `branch.py:247-254`: the `metadata["current_progression"]` subset, built at `lionagi/tools/context/context.py:142-149`, or `MessageManager.progression` when it is unset | unique: the subset is seeded from the full order and the context tool never appends a duplicate to it (`context.py:334-335` skips ids already active before `:343`), `branch.py:249` calls it a subset, and ADR-0007 D2 rejects a duplicate id in the selected history (`docs/adr/ADR-0007-canonical-turn-request-compilation-boundary.md:218-219`) |
| `Exchange` mailbox views `outbox` and `inbox_<sender>` | `exchange.py:53`, `:148-153` | repeated: standalone orders held by per-owner Flows; the `progressions` Pile that indexes them is itself a collection, and therefore unique over the view objects |

- ADR-0003 D5's `pending` (the set-like `include` in "Executor owns live Events and pending order",
  `docs/adr/ADR-0003-in-process-event-execution-lifecycle.md:358-359`) is the `Executor.pending` row
  above.

**Why this way.** The alternatives — keeping the two locks with a published contract, and one lock
for both contexts — are compared below.

## Consequences

- A caller states multiplicity in the ordering type it holds, so the rule is read from the signature
  rather than reconstructed from the call history, and a collection's default order is unique
  because of its type rather than because its constructor checked.
- Copying the caller's order on construction closes the alias in P3 at the cost of one copy per
  construction from an existing order; construction from a plain sequence is unchanged in shape.
- Removing the interior locks removes a deadlock class and the cross-thread spin from every async
  entry, and it gives up the per-method mutual exclusion ADR-0002 D6 records; that exclusion never
  spanned a caller's read-then-act sequence, so nothing a caller could already rely on across an
  await is lost. Each named holder moves onto the holder rule or onto the snapshot, which is work in
  the callers rather than a change to the ordering types.
- A snapshot copies the ids and nothing else, so a reader that takes several snapshots pays for each
  and a holder that owns the collection pays nothing extra; a reader that needs the items as well
  does its own lookups on the ids, which can miss an item removed after the snapshot.
- Reversal is asymmetric. Splitting the ordering type touches serialized repeated sequences and the
  construction path; restoring interior locking touches every named holder.
- Neither decision adds a persistence format. The serialized Pile keeps its shape, a polymorphic
  item list plus a serialized order, and only the order's class name inside that payload changes
  when the one ordering type becomes two.

## Alternatives considered

### A uniqueness flag on one ordering type

A `unique: bool` field, or a constructor switch, would carry the rule in a value, and every caller
and reader would have to read that value to know whether a mutation may duplicate an id. That is the
method-dependent rule moved from the method name to a field, and the field is what the type cannot
check: nothing prevents flipping it on an order a collection already holds. It lost for that reason.

### Keep the shipped mixed behavior

The collection's constructor and its naming mutators would stay the only places the unique rule
holds, and a standalone order would keep meaning different things per method. It lost because the
invariant then depends on every call site enforcing it, and a caller holding the order directly can
break it, so the same ground has to be argued again for each new mutator. ADR-0002 delta row 3
carries the split.

### A unique order everywhere

A universal set-like order would simplify membership and align standalone sequences with
collections. It would remove the ability to represent repeated sequence visits and would change
`append`, `extend`, `insert`, and already serialized data. It lost because the repeated order is
meaning-bearing where it is used: `Flow` views take the same UUID again on every `add_item`
(`flow.py:226-229`, ADR-0002 D5), and mailbox orders carry one entry per delivery.

### Keep the two locks and publish their contract

A published lock contract would document what the type does today. It cannot give a caller a
linearizable mixed thread/async view or a stable snapshot, because each lock is held for the
duration of one method, so a caller's read-then-act sequence spans several acquisitions and the
two locks, which do exclude each other, protect no more than one method at a time. Documenting the
limit does not remove it, and the cost of holding a collection across real work stays hidden inside
the type.

### One lock for both contexts

One lock would have to serve a synchronous and an asynchronous caller at once. A threading lock
cannot be awaited as the only async primitive, and an AnyIO lock is not a drop-in synchronous lock,
so neither replaces the pair on its own; and a lock held for the duration of one method still cannot
make a caller's read-then-act sequence atomic. Removing the pair is the decision taken instead of
widening it.

## Notes

- `ADR-0002` D4 and D6 keep the shipped-state descriptions, with superseding notes that point here.
  ADR-0002's delta rows 3 and 4 carry the two code deliverables and their acceptance clauses, so
  this record decides and the retrospective record tracks.
