# ADR-0002: UUID-keyed ordered collection model

- **Status**: Accepted, amended 2026-10-16 (see Amendment 2)
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: extends ADR-0001

## Context

LionAGI collections need identity lookup, deterministic traversal, reusable orderings, and named
views without copying the underlying models. The shipped Pile, Progression, and Flow types answer
six related problems.

**P1 — Identity lookup and traversal order are different concerns.** A dictionary gives constant
time UUID lookup but not an independently editable order. A sequence gives order but makes identity
lookup linear and cannot by itself enforce that a UUID has a resident object.

**P2 — Construction and mutation must keep both representations aligned.** A collection becomes
invalid if its dictionary contains an item absent from the order, if the order names an absent
item, or if the default order contains duplicate UUIDs that address only one dictionary entry.

**P3 — Item admission has both nominal and structural definitions in the repository.** Pile uses
the nominal `Observable` base and optional runtime classes. The public contracts module separately
exports a runtime-checkable structural protocol requiring only an `id` property. Those are not the
same contract today.

**P4 — A reusable ordering and a Pile-owned ordering do not share one multiplicity rule.** A
standalone Progression is a sequence and several mutators preserve repeated UUIDs. A Pile's default
Progression must be unique because it is the one-to-one traversal view of a UUID-keyed dictionary.

**P5 — Some consumers need several orderings over one stored set.** Session observation and
exchange-style mailboxes need the same item in several named sequences. Adding all named views to
every Pile would burden the common collection, while duplicating items would create divergent
copies.

**P6 — Sync and async callers need coordination, but the current surface evolved incrementally.**
Pile exposes a reentrant thread lock and a separate AnyIO lock. Decorated operations do not cover
every public read and mutation, and the two locks are not one unified snapshot mechanism.

The implementation anchors are `lionagi/protocols/generic/pile.py`,
`lionagi/protocols/generic/progression.py`, `lionagi/protocols/generic/flow.py`,
`lionagi/protocols/generic/element.py`, `lionagi/protocols/_concepts.py`, and
`lionagi/protocols/contracts.py`.

| Concern | Decision |
|---|---|
| Pile storage and order | D1: A Pile owns one UUID-to-item dictionary and one unique default Progression containing exactly the same UUIDs. |
| Addressing and mutation | D2: UUID/Element references address identity; integers and slices address positions; mutators update store and order together. |
| Item admission and wire shape | D3: Pile admission is nominal and optionally class-restricted; serialized collections are polymorphic item lists plus a serialized Progression. |
| Reusable ordering | D4: Progression stores an ordered UUID deque with set-backed membership, while duplicate behavior remains method-dependent. |
| Named views | D5: Flow owns one item Pile and a Pile of named Progressions, validating that every view refers only to resident items. |
| Synchronization | D6: Pile and Flow provide method-level cooperative locks, not a general cross-context linearizability or snapshot contract. |

This ADR deliberately does **not** decide:

- database indexes or persistence tables for collections; adapters consume the serialized model;
- durable queue or scheduler semantics; Progression is an ordering, not an acknowledgement queue;
- ~~the future choice between unique and repeated standalone sequences; the current mixed behavior
  is retrospective truth and remains a delta~~ — **decided 2026-10-16 (Amendment 2)**: multiplicity
  is a property of the ordering type;
- ~~a unified sync/async concurrency design; the shipped locks and their limits are recorded, while
  the target requires a separate design decision~~ — **decided 2026-10-16 (Amendment 2)**:
  collections carry no locks, and coordination belongs to the holder.

## Decision

### D1 — Pile is one UUID-keyed store plus one unique default order

**The contract** (`lionagi/protocols/generic/pile.py`):

```python
class Pile(Element, Collective[T], Generic[T], Adaptable, AsyncAdaptable):
    collections: dict[UUID, T] = Field(default_factory=dict)
    item_type: set | None = Field(default=None, exclude=True)
    progression: Progression = Field(default_factory=Progression)
    strict_type: bool = Field(default=False, frozen=True)

    def __init__(
        self,
        collections: ID.ItemSeq = None,
        item_type: set[type[T]] = None,
        order: ID.RefSeq = None,
        strict_type: bool = False,
        **kwargs,
    ) -> None: ...
```

The invariant is:

```text
set(pile.collections.keys()) == set(pile.progression.order)
len(pile.collections) == len(pile.progression.order)
```

**Exact construction semantics**:

- Missing or falsey `collections` produces an empty dictionary, except that a falsey nominal
  Observable such as an empty Pile or Progression is retained as one candidate item.
- A dictionary item is reconstructed with `Element.from_dict` before admission. Other input values
  pass through the `to_list_type` normalization helper.
- Items are keyed by `item.id`. Repeated input items with the same UUID overwrite the earlier
  dictionary value before the order is validated; the resulting store has one entry for that UUID.
- With no order, Pile creates a Progression in dictionary insertion order.
- A supplied order may be a serialized Progression dictionary, a Progression, or an accepted
  sequence of references. It must contain no duplicate UUIDs, have the same length as the store,
  and resolve every UUID to a dictionary key. Violations raise `ValueError` before construction.
- An empty supplied order is treated like no order and therefore becomes dictionary insertion
  order. An explicitly empty order cannot describe a non-empty unordered Pile.
- `strict_type` is frozen after construction. `collections` and `progression` remain mutable, so
  callers can bypass the invariant by mutating fields directly; supported mutation goes through the
  Pile methods.

The dictionary and Progression are both necessary: identity lookup does not have to follow display
order, and reordering does not rebuild or duplicate the objects.

### D2 — References address identity and integers/slices address order

Pile's public surface deliberately has two indexing domains.

**The contract** (`lionagi/protocols/generic/pile.py` and
`lionagi/protocols/generic/element.py`):

```python
ID.Ref = UUID | Element | str

def __getitem__(self, key: ID.Ref | ID.RefSeq | int | slice) -> Any | list | T: ...
def __setitem__(self, key, item) -> None: ...
def get(self, key, default=UNDEFINED, /) -> T | Pile | D: ...
def pop(self, key, default=UNDEFINED, /) -> T | Pile | D: ...
def include(self, item, /) -> None: ...
def exclude(self, item, /) -> None: ...
def update(self, other, /) -> None: ...
def insert(self, index: int, item, /) -> None: ...
def append(self, item, /) -> None: ...
```

**Exact read semantics**:

- A UUID selects `collections[uuid]`. An Element or reference sequence is normalized through
  `ID.get_id`; one match returns the item and multiple matches return a list.
- An integer first selects a UUID from the default Progression, then returns that item. A slice
  returns a scalar when exactly one item was selected and a list for multiple items; an empty or
  invalid slice raises `ItemNotFoundError`.
- Passing a Python type returns a new Pile filtered by that type. Passing another callable returns a
  new Pile of items for which the predicate is true.
- `get` and `pop` translate lookup/normalization failures to `ItemNotFoundError` unless a default was
  supplied, in which case the default is returned.
- `keys`, `values`, and `items` are materialized lists in Progression order. Despite the `keys`
  return annotation saying `Sequence[str]`, the actual members are UUID objects.
- Iteration snapshots the current Progression into a list, then performs live dictionary lookups.
  It stabilizes the order list only; removal after the snapshot can still make lookup fail.
- Pile truthiness is `not is_empty()`, overriding Element's always-true behavior.

**Exact mutation semantics**:

- `include` validates items, adds only missing UUIDs to the Progression, and updates dictionary
  values. Including an existing UUID therefore replaces the stored object without moving it.
- `update` replaces existing UUID values in place and delegates new UUIDs to `include`; `append` is
  an alias for that upsert behavior, not an always-add duplicate operation.
- `insert` rejects any item UUID already present, inserts new UUIDs at the requested Progression
  position, then updates the dictionary.
- Integer/slice assignment accepts only new item UUIDs, replaces the selected Progression segment,
  removes the displaced dictionary entries, and installs the new entries. Other assignment requires
  each supplied key to match one new item's UUID; it then appends those keys.
- `pop` by integer returns one item; a multi-item slice returns a new Pile. `pop` by several UUID
  references returns a list rather than a Pile. All popped UUIDs are removed from both structures.
- `exclude` silently ignores missing candidates and pops only present ones. `remove` raises
  `ItemNotFoundError` when the item is absent and rejects integer/slice arguments.
- Set-style `|`, `^`, and `&` operations accept only another Pile and derive membership from UUID
  identity while retaining the left Pile's type policy.
- A valid UUID string is declared as `ID.Ref`, and `validate_order` handles it for several reads and
  pops. The Pile-specific `to_list_type` helper instead returns a bare UUID for that string, not a
  one-item list; construction/order and non-positional assignment paths that use this helper are
  therefore inconsistent and are retained as delta 1.

### D3 — Pile admission is nominal and its serialized constraint is not durable

> **Amendment (2026-07-25) — supersedes the 2026-07-20 note below**: Pile admission is
> **structural**. `Observable` is a single runtime-checkable `Protocol` in
> `lionagi/protocols/_concepts.py`: any object exposing an `id` satisfies
> `isinstance(item, Observable)` and is admissible. `Element` conforms through its `id` field
> without inheriting it (a Protocol cannot be a pydantic base). The two-symbol split stays
> gone — there is one `Observable`, and it is structural, so `isinstance` and Pile admission
> agree. Serializing a Pile remains the one Element-shaped boundary (dumping calls
> `to_dict()`).
>
> **How this decision got here.** Admission has now been decided three times and the arc
> matters, because the same ground has been re-argued twice without the reasons surviving in
> the record:
>
> 1. **Structural, first attempt.** `Observable` was a structural protocol and Pile admitted
>    anything with an `id`.
> 2. **The defect that ended it.** Admission and *id resolution* were structural at different
>    levels. Pile decided membership one way while `ID.get_id` and `validate_order` resolved
>    identity another, and `item_type` judged conformance at the class level, where a
>    data-member protocol cannot answer — `issubclass` is not supported for them by design. The
>    two halves disagreed, and the visible failure was an object that Pile *accepted* and then
>    could not address: it went in, and `pile[obj.id]` could not get it back out. A container
>    that admits what it cannot retrieve is worse than one that refuses the item outright.
> 3. **Nominal (2026-07-20).** The response was to make admission require inheritance. That
>    closed the disagreement by removing one side of it — nominal admission is checkable at the
>    class level, so the two halves could not diverge. It closed the defect at the cost of the
>    contract.
> 4. **Structural again, this amendment.** The disagreement is closed on its own terms instead.
>
> **Why the reason for step 3 no longer applies.** The revert happened because the class-level
> check and the instance-level check could disagree. They can no longer be asked to. `item_type`
> normalizes classes without judging conformance at all, because conformance is a property of
> instances and not of classes: a class that assigns `self.id` in `__init__` declares nothing at
> class level while every one of its instances conforms. The judgment therefore happens once, at
> admission, where the instance exists. Id resolution (`ID.get_id`, `validate_order`) resolves
> through the same structural check, so admission and retrieval are two uses of one rule rather
> than two rules that must be kept in agreement. This is the specific difference from the first
> attempt: not a better class-level check, but the removal of the class-level judgment that
> could disagree.
>
> **The checkable demonstration.** Naming a suite is a pointer, not a demonstration, so the
> guarantee is pinned to a named test:
> `tests/protocols/test_observable_protocol.py::test_duck_typed_item_with_uuid_id_is_admitted`
> constructs an id-bearing object that inherits nothing, admits it to a Pile, and asserts it is
> a member, that `pile[duck.id]` returns *that same object*, that the Pile has length one, and
> that `exclude` removes it. Admitted, addressable, removable — the exact failure of step 2,
> reproduced and shown closed. Siblings cover admission at construction time and admission into
> a Progression.
>
> **Widening is not the safety argument.** Nominal-to-structural admits strictly more, so no
> existing inheritor is turned away. That is true and it is not why this is safe. Step 2 *was* a
> widening, and it broke: widening admission without widening resolution is what produced an
> accepted-but-unaddressable object. The safety comes from admission and resolution being
> widened together, which is what the named test checks. A future change that widens one without
> the other reproduces step 2, and the monotonicity argument would still appear to apply.
>
> The historical blocks below are retained as the record of states previously decided against.
>
> **Superseded — current-state note (2026-07-20)**: The code block and delta 2 below describe the state
> before issue #2017. `lionagi/protocols/contracts.py` (`ObservableProto`, the structural
> `Observable`/`LegacyObservable` aliases) is deleted; there is no exported structural
> convenience protocol anymore. The nominal ABC is the sole public Pile-item contract and
> keeps the name `Observable`: with the structural protocol deleted, the `Observable` name's
> `isinstance` meaning is unambiguously the nominal admission contract. Delta 2 is resolved; the block
> below is kept as the historical record of the state it decided against.

Pile imports `Observable` from `lionagi/protocols/_concepts.py`, an empty nominal ABC. This is
deliberately distinguished from the exported structural convenience protocol.

**The contracts**:

```python
# lionagi/protocols/_concepts.py
class Observable(ABC):
    """Observable entities must define 'id'."""

# lionagi/protocols/contracts.py
@runtime_checkable
class ObservableProto(Protocol):
    @property
    def id(self) -> object: ...

Observable = ObservableProto
LegacyObservable = protocols._concepts.Observable
```

```python
def _validate_item_type(value) -> set[type] | None: ...
def _validate_collections(
    value: Any,
    item_type: set | None,
    strict_type: bool,
) -> dict[UUID, T]: ...
```

**Exact semantics** (as decided at the time; the admission rule below was later
reverted — see the current-state note above for what the code does today):

- ~~With no `item_type`, every item must be an instance of the nominal ABC. Merely exposing an `id`
  property through the structural protocol is not sufficient.~~ Superseded: admission is
  structural again, and `item_type` no longer judges class-level conformance.
- `item_type` accepts classes and unions. A fully qualified class string is resolved when it reaches
  validation as a member of a list, tuple, or set. A scalar non-UUID string is currently discarded
  by Pile's `to_list_type` normalizer before the resolver sees it. ~~Every resolved member must be a
  nominal Observable subclass~~ — superseded: a resolved member need only be a class, since
  conformance is a property of instances and is established at admission. Duplicated declarations
  raise LionAGI `ValidationError`.
- With `strict_type=False`, an item's concrete type may be any subclass of an allowed class. With
  `strict_type=True`, `type(item)` must be exactly one allowed class.
- Invalid item types and invalid items fail before the Pile dictionary/order is committed.
- `collections` serializes as a list of each item's polymorphic `to_dict()` output. `progression`
  serializes as its full Element dictionary. `strict_type` is serialized.
- `item_type` is declared with `exclude=True`; despite a field serializer existing for it, the
  normal Pile dictionary does not carry the allowed-class set. `Pile.from_dict` calls the
  constructor with the fields present, so a standalone round trip loses the runtime admission
  constraint while retaining `strict_type`.

The nominal check is the implemented behavior because Pile needs model identity and serialization,
not merely a property named `id`. The mismatch with the public structural alias is still a source
of extension ambiguity and remains delta 2.

### D4 — Progression is an ordered UUID sequence with method-dependent multiplicity

**The contract** (`lionagi/protocols/generic/progression.py`):

```python
class Progression(Element, Ordering[T], Generic[T]):
    order: deque[UUID] = Field(default_factory=deque)
    name: str | None = None
    _members: set[UUID] = PrivateAttr(default_factory=set)
```

Input is flattened by `validate_order`: Elements contribute their IDs; mappings contribute keys;
UUIDs and UUID strings become UUIDs; nested list, tuple, and set containers are flattened; `None`
is ignored; any other member raises `ValueError`.

**Exact semantics**:

- `_members` is rebuilt as `set(order)` after validation. It accelerates membership but does not
  encode occurrence count.
- `include` is set-like: it appends only UUIDs absent from `_members`, returns whether anything was
  added, returns `True` for empty input, and returns `False` for invalid input.
- `append`, `extend`, `insert`, `+=`, construction, and slice/integer assignment do not reject a UUID
  already in the deque. Repetitions remain visible to iteration, length, `count`, serialization, and
  positional access.
- Membership asks whether every normalized reference occurs at least once. An empty normalized input
  therefore returns `True` by Python's `all([])` behavior; invalid input returns `False`.
- `exclude`, `remove`, and subtraction remove every occurrence of each selected UUID. For empty
  normalized input, `exclude` returns true as a successful no-op; otherwise it returns whether
  length decreased. `remove` also returns normally for empty input and raises if any non-empty
  requested UUID is absent.
- `pop`/`popleft` remove one occurrence and remove the UUID from `_members` only after its last
  occurrence leaves the deque.
- Integer lookup returns a UUID. Slice lookup returns a new Progression and rejects an empty slice.
  Out-of-range lookup raises `ItemNotFoundError`.
- Integer assignment uses only the first normalized UUID, silently ignoring additional UUIDs;
  empty input raises `IndexError`. An integer beyond the deque inserts that first UUID according to
  `deque.insert`; slice assignment consumes every normalized UUID and rebuilds the deque. `move`,
  `swap`, and `reverse` preserve membership and multiplicity.
- Progression equality compares ordered UUID contents and `name`, not inherited Element UUID. This
  intentionally differs from Element equality.

Pile construction imposes uniqueness on its default Progression; standalone and Flow-owned
Progressions do not. No caller can safely infer a universal duplicate rule from the type alone.

> **Superseded (2026-10-16, Amendment 2).** This heading's "method-dependent multiplicity" and the
> sentence above describe the shipped state. The rule now decided is that multiplicity is a
> property of the ordering type: a unique order belongs to a collection and admits no second
> position for a resident id, while a repeated order is a standalone sequence. See Amendment 2 for
> the decision, the code that does not follow it yet, and the rejected alternatives.

### D5 — Flow owns multiple referentially valid views over one item Pile

**The contract** (`lionagi/protocols/generic/flow.py`):

```python
class Flow(Element, Generic[E, P]):
    name: str | None = None
    items: Pile[E] = Field(default_factory=Pile)
    progressions: Pile[P] = Field(default_factory=Pile)
    _progression_names: dict[str, UUID] = PrivateAttr(default_factory=dict)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    def add_progression(self, progression: P) -> None: ...
    def remove_progression(self, key: UUID | str | P) -> None: ...
    def get_progression(self, key: UUID | str | P) -> P: ...
    def add_item(self, item: E, progressions=...) -> None: ...
    def remove_item(self, item_id: UUID | str | Element) -> None: ...
```

**Exact semantics**:

- Construction and `from_dict` compute `item_ids = set(items.keys())` and reject any Progression
  containing a UUID outside that set. An empty Flow is valid.
- `_progression_names` indexes only truthy names. During normal `add_progression`, a duplicate name
  raises `ItemExistsError`; UUID identity remains enforced by the Progressions Pile. Construction
  does not separately reject duplicate names, so rebuilding the private index leaves the last
  Progression with a repeated name addressable by that name.
- `add_progression` validates all references before modifying the Pile. Unnamed Progressions are
  valid and can be addressed only by UUID or instance.
- Name lookup wins for a string key. If no name matches, Flow tries to parse the string as a UUID.
  A missing non-UUID name is translated to `ItemNotFoundError`.
- `add_item` resolves UUID and name references against the Flow before inserting the item. A
  Progression instance is accepted directly without checking that it is resident in
  `flow.progressions`; the method can therefore upsert the item into the Flow and append its UUID to
  an external Progression that the Flow does not own. For every resolved or directly accepted view,
  repeated calls can append the same UUID repeatedly.
- `remove_item` removes every occurrence of the UUID from every Progression, then removes the item
  from the item Pile. `clear` clears both Piles and the private name index.
- Python-mode serialization explicitly calls nested `Pile.to_dict()` so concrete item classes are
  preserved. `from_dict` accepts a Pile, serialized dictionary, or item list for each Pile, manually
  validates referential integrity, uses `model_construct`, then rebuilds private fields.
- The same stored item may appear in several Progressions without copying the item object.

Flow, rather than Pile, owns the cost of multiple views because only the consumers that need named
orders should carry their lifecycle and referential-integrity rules.

### D6 — Synchronization is method-level and cooperative

Pile owns two independent private locks:

```python
_lock: threading.RLock
_async_lock: lionagi.ln.concurrency.Lock  # wraps anyio.Lock
```

The `synchronized` decorator acquires only `_lock`; `async_synchronized` acquires only
`_async_lock`.

**Exact semantics**:

- Sync `pop`, `include`, `exclude`, `clear`, `update`, `insert`, `append`, and `get` acquire the
  RLock. Direct `__setitem__`, `remove`'s membership test, `__getitem__`, iteration, key/value/item
  snapshots, filtering, and direct public-field mutation are not uniformly protected.
- Async mutation/read wrappers acquire the AnyIO lock. Several then call a sync decorated method,
  acquiring the RLock inside the async lock; `asetitem`, `apop`, `aclear`, and `aget` call private
  helpers directly and do not all share that nested locking shape.
- The sync and async locks do not exclude each other by themselves. A sync caller holding only the
  RLock and an async caller on a helper guarded only by the AnyIO lock can overlap.
- Sync iteration snapshots only Progression order. Async iteration snapshots order while holding the
  async lock, releases it, and then performs live dictionary lookups.
- Pickle restoration and deep copy create fresh lock instances; locks are not serialized or shared
  with the copy.
- `adump` snapshots dictionary UUIDs and a DataFrame under the async lock, writes outside the lock,
  and, when `clear=True`, later removes only the snapshotted UUIDs. Items added during the write are
  retained.
- Synchronous `dump(clear=True)` is format-dependent in the shipped control flow: Parquet reaches
  the final `clear()` call, while JSON-lines and CSV return immediately after their writer and do
  not clear. Async `adump(clear=True)` applies the snapshot removal after all three supported
  writers.
- Flow uses a separate RLock around its own named-view and item-management methods. External
  mutation of its public Piles or Progressions bypasses that lock and can violate referential or
  name-index invariants.

These facilities support disciplined cooperative use and individually serialized common mutations.
They do not promise a linearizable mixed thread/async collection, immutable snapshots, or atomic
multi-method transactions.

> **Superseded (2026-10-16, Amendment 2).** The locks above are the shipped state. The rule now
> decided is that a collection type owns no lock: one holder at a time, a snapshot where several
> reads must agree, and coordination at the holder rather than inside the type. See Amendment 2 for
> the decision, the callers that hold a collection across an await today, and the rejected
> alternatives.

## Amendment 2 (2026-10-16) — multiplicity is a property of the ordering type, and collections carry no locks

The 2026-07-25 amendment recorded inside D3 is counted as Amendment 1 here. This amendment
settles the two questions D4 and D6 left open: what decides a Progression's multiplicity, and
where the coordination of concurrent access lives. They are one question, whether the rule
belongs to the type, so they are decided together, and both change what D4 and D6 say.

### Multiplicity is named by the ordering type, not by the mutator

**The decision.** A Progression's multiplicity is a property of its type, and two types state the
two rules:

- A **unique order** is the order inside a collection. It holds exactly the store's ids, one
  position each, as D1 requires, and no mutator on it can add a second position for an id that is
  already there.
- A **repeated order** is a standalone sequence, used where a second visit to the same id is
  meaning-bearing. `append`, `extend`, `insert`, `+=`, and positional assignment store another
  position on it, and construction, membership, and serialization agree with that rule.

A caller gets the rule the type names, not the rule the method happens to implement, so reading a
signature answers whether the caller may duplicate an id.

**What this changes.** D4's heading and its closing sentence — "duplicate behavior remains
method-dependent", "No caller can safely infer a universal duplicate rule from the type alone" —
are superseded; the note at the end of D4 states what stands in their place.

**What the code does today.** One `Progression` class
(`lionagi/protocols/generic/progression.py:139`) carries both rules, and which rule applies
depends on the mutator and on who owns the instance:

- Uniqueness holds where a Pile owns the order. Construction rejects a repeated id in the supplied
  order (`lionagi/protocols/generic/pile.py:129-133`), `include` adds only ids absent from the
  order (`pile.py:303-307` through `progression.py:270-286`), `insert` raises `ItemExistsError`
  for an id already in the order (`pile.py:826-835`), and positional replacement does the same
  (`pile.py:707-747`).
- Repeats survive where the caller owns the order. `append`, `extend`, `insert`, `+=`, and slice
  or integer assignment keep a repeated id (`progression.py:302-314`, `:364-369`, `:392-398`,
  `:379-381`, `:225-244`), while `include` on the same class is set-like
  (`progression.py:270-286`), `exclude` removes every occurrence (`progression.py:288-300`), and
  membership asks only whether the id occurs at least once (`progression.py:200-205`).

One object therefore answers multiplicity differently depending on the method called, and the
unique rule lives in Pile's constructor and its own methods rather than in the ordering type.
Pile's default order is a public field (`pile.py:188`), so `pile.progression.append(uid)` still
leaves a second position for a resident id; D1 records the same escape hatch for direct field
mutation.

**The alternatives this leaves rejected.**

- *A uniqueness flag on one type.* A `unique: bool` field, or a constructor switch, would carry
  the rule in a value, and every caller and reviewer would have to read that value to know whether
  a mutation may duplicate an id. That is the method-dependent rule moved from the method name to
  a field, and the field is what the type cannot check.
- *Keep the shipped mixed behavior.* Pile's constructor and its naming mutators would stay the
  only places the unique rule holds, and a standalone ordering would keep meaning different things
  per method. Delta 3 is the record of why that is not enough.

### Collections carry no locks, and coordination belongs to the holder

**The decision.** A collection type owns no lock. A Pile or a Flow has one holder at a time; a
holder that needs several reads to agree takes one snapshot and reads that; coordination between
holders is the holder's, exercised where the holder lives — `Session` over its branches,
`Executor` over its event pile (`lionagi/protocols/generic/processor.py:188`), `Flow` over its
named views. Where a shared-read shape is genuinely
needed, it enters as an explicit snapshot value, not as interior locking and not as a lock a
caller may hold across an await.

**What this changes.** D6's heading and the "method-level cooperative locks" sentence in the
table above are superseded; the note at the end of D6 states what stands in their place. The
unified sync/async concurrency design this ADR listed as undecided is decided here to the extent
that it is a place rather than a lock: coordination moves out of the collection.

**What the code does today.** Pile carries two private locks and a protocol over both
(`lionagi/protocols/generic/pile.py:208-209`, the both-lock entry at `:549-564`, the public
`lock`/`async_lock` properties at `:538-547`); async entry, async iteration, async adaptation, and
`adump` take them (`pile.py:857-876`, `:631-637`, `:902`, `:994-1012`). Flow carries its own
reentrant lock (`lionagi/protocols/generic/flow.py:41`, rebuilt at `:132`) and takes it in
`add_progression`, `remove_progression`, `get_progression`, `add_item`, `remove_item`, and
`clear` (`flow.py:140`, `:161`, `:175`, `:212`, `:233`, `:244`). Neither lock is serialized;
pickle and deep copy build fresh ones (`pile.py:504-536`).

Callers hold a collection across an await today: `async with self.branches`
(`lionagi/session/session.py:75`), `async with self.messages`
(`lionagi/protocols/messages/manager.py:74`), `async with self.flows`
(`lionagi/session/exchange.py:87`), and `async with self.logs`
(`lionagi/protocols/generic/log.py:246`). There is no snapshot type for them to move onto:
iteration stabilizes the order and looks items up live (`pile.py:378-386`), and `keys`, `values`,
and `items` materialize a list inside the lock (`pile.py:358-368`). Delta 4 is rewritten below to
name the removal, the missing snapshot, and those four holders.

**The alternatives this leaves rejected.**

- *Keep the two locks and publish their contract.* D6 already records why a lock inside the
  collection cannot give a caller a linearizable mixed thread/async view or a stable snapshot.
  Documenting the limit does not remove it, and the cost of holding a collection across real work
  stays hidden inside the type.
- *One lock for both contexts.* A single lock was rejected above: a threading lock cannot be
  awaited as the only async primitive, and an AnyIO lock is not a drop-in synchronous lock.
  Removing the pair is the decision taken instead of widening it.

### Consequences of both decisions

- A caller states multiplicity in the ordering type it holds, so a reviewer reads the rule from
  the signature rather than reconstructing it from the call history, and Pile's default order is
  unique because of its type rather than because its constructor checked.
- Removing interior locks removes a deadlock class and the cross-thread spin from every async
  entry, and it removes the guarantee D6 records for a caller that holds a collection across an
  await. Each affected caller moves to the holder rule or to a snapshot, which is work outside
  `lionagi/protocols/generic/`.
- A snapshot copies the order and the items it visits, so a reader that takes several snapshots
  pays for each, while a holder that owns the collection pays nothing extra.
- Reversal is asymmetric. Splitting the ordering type touches serialized repeated sequences and
  the Pile constructor; restoring interior locking touches every caller listed above.
- Neither decision adds a persistence format. The serialized Pile keeps its shape, a polymorphic
  item list plus a serialized Progression; the default order's class name inside that payload is
  the ordering split's own change.
- Delta rows 3 and 4 are narrowed in place by this amendment rather than closed: each decision is
  now recorded, and neither is in the code yet.

## Consequences

Identity lookup remains independent from traversal order, and one stored object can participate in
several named sequences without duplication. Pile construction rejects the most damaging split-
brain states between dictionary and default order. Flow centralizes the additional cost and
integrity rules of multiple views.

The design also carries concrete maintenance costs:

- contributors must choose the correct identity or positional access domain and account for scalar
  versus list/Pile return shapes;
- Progression multiplicity depends on the mutator and owner;
- Pile's nominal admission rule differs from the exported structural protocol and is not fully
  preserved in its wire payload;
- valid UUID strings do not normalize consistently on every Pile path;
- Flow can mutate a Progression instance that is not one of its owned named views;
- synchronous dump-and-clear behavior differs by selected file format; and
- callers currently need to know which lock and mutation discipline protects a compound operation.

Reversing D1 or D2 affects nearly every collection consumer. Replacing D4's multiplicity behavior
requires a data and caller audit because serialized repeated sequences already exist. Replacing D6
is mechanically local to collection methods but semantically broad: it changes blocking,
reentrancy, async scheduling, and snapshot guarantees.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Fix Pile UUID-string reference normalization in `to_list_type`; acceptance requires valid UUID strings to work as one-item references on the paths that fail today — `__getitem__`, `__setitem__`, `exclude`, and `order=` at construction — with regression coverage (`get` and `pop` already route through `validate_order` and work). | S | #2012 |
| 2 | ~~Define one public Pile-item contract and align the exported Observable protocol with it~~ — **Resolved 2026-07-20**: the structural protocol is deleted, the nominal ABC is the sole contract, exported as `Observable`, enforced by item-type validation, with extension tests in `tests/protocols/test_observable_protocol.py`. | M | #2017 |
| 3 | ~~Decide and enforce Progression multiplicity semantics for standalone Progressions, Pile default order, and Flow named views; acceptance requires append, extend, insert, serialization, and membership behavior to match the documented rule~~ — **Narrowed 2026-10-16 (Amendment 2)**: the rule is decided (multiplicity is a property of the ordering type). What remains is the code: a unique order type and a repeated order type, with Pile's default order, standalone sequences, and serialization following the rule their type names. | M | (filled at issue-open time) |
| 4 | ~~Publish and enforce Pile synchronization and snapshot semantics; acceptance requires every public mutator and read API to be audited against a stated sync, async, and mixed-context contract, with stable snapshot APIs where live traversal is unsafe~~ — **Narrowed 2026-10-16 (Amendment 2)**: the design is decided (collections carry no interior locks; coordination and snapshots belong to the holder). What remains is the code: remove the locks, provide the snapshot value, and move every caller that holds a collection across an await onto the holder rule. | M | (filled at issue-open time) |
| 5 | Define and enforce Progression ownership in `Flow.add_item`; acceptance requires instance, UUID, and name references to follow one documented residency rule, reject invalid ownership before item insertion, and never mutate an external view accidentally. | S | (filled at issue-open time) |
| 6 | Make `Pile.dump(clear=True)` consistent across JSON-lines, CSV, and Parquet; acceptance requires every format either to clear the same documented pre-write snapshot after success or to retain it, with failure-path tests. | S | (filled at issue-open time) |

## Alternatives considered

### Dictionary insertion order as the only order

Python dictionaries preserve insertion order, so Pile could omit Progression. That would make
initial traversal deterministic, but arbitrary reordering, positional replacement, and multiple
views would require rebuilding dictionaries or copying items. An explicit UUID sequence keeps
storage identity independent from current processing order.

### A list of items with linear identity lookup

A list would unify storage and order and remove the split-representation invariant. UUID lookup,
membership, replacement, and graph endpoint resolution would become linear scans, and duplicate
identity behavior would remain ambiguous. The dictionary plus Progression pays one extra structure
for direct lookup and explicit order.

### Several Progressions inside every Pile

This would make named views directly available on the common collection. Most Piles need only one
order, and every Pile would then need naming, referential integrity, and view-removal rules. Flow
keeps that complexity in the consumers that need it.

### Copy each item into every named sequence

Independent lists would avoid reference validation, but a content change in one list would not
update the others and UUID identity would no longer select one authoritative object. Flow stores
objects once and sequences only UUIDs.

### Structural `id`-only admission

Accepting `ObservableProto` would make lightweight third-party objects easy to store. Pile also
serializes every item with `to_dict` and reconstructs dictionaries through Element, so an `id`
property alone is not the full operational contract. The current code retains nominal admission;
delta 2 requires choosing and documenting the complete extension surface rather than pretending the
structural alias is sufficient.

### Unique Progressions everywhere

A universal set-like order would simplify membership and align standalone Progression with Pile.
It would remove the ability to represent repeated sequence visits and would change `append`,
`extend`, `insert`, and already serialized data. The code has not made that choice; the fork remains
delta 3.

### Duplicate-preserving Pile default order

Allowing repeated UUIDs in Pile's default Progression would represent the same dictionary item at
several positions, but Pile length, pop, replacement, and dictionary/order equality would become
occurrence-sensitive. Construction deliberately rejects this even though standalone Progression can
represent it.

### One cross-context lock for all operations

A unified lock and snapshot API could provide a stronger contract to mixed sync/async callers. A
threading lock cannot be awaited safely as the only async primitive, while an AnyIO lock is not a
drop-in synchronous lock; changing all methods also risks deadlock and event-loop blocking. The
current separate locks remain retrospective truth pending the explicit concurrency design in delta
4.

### Require every Progression instance passed to Flow to be resident

`add_item` could resolve a Progression instance through its UUID exactly as it resolves UUID and
name inputs. That would make the Flow the sole owner of every mutated view and reject an external
Progression before inserting the item. The shipped method treats an instance as already resolved,
which avoids one lookup but bypasses the Flow's residency boundary. No stronger rationale for that
shortcut is recorded; delta 5 retains the ownership correction.

### One dump-and-clear path for every file format

All synchronous writers could fall through to one post-write `clear()` block, matching async
`adump`. That would make `clear=True` independent of the selected format. JSON-lines and CSV
currently return their adapter result from inside the format branch, so only Parquet reaches the
shared clear block. No format-specific retention rationale is recorded; delta 6 treats the split as
a control-flow defect rather than a deliberate storage policy.

## Notes

`Pile.dump` and `adump` support JSON-lines, CSV, and Parquet through DataFrame adapters. Those file
formats are transport conveniences, not a second collection identity model. Parquet requires
`pyarrow`; DataFrame conversion requires pandas and raises an installation-oriented `ImportError`
when unavailable.
