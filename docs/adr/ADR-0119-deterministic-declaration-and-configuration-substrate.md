# ADR-0119: Deterministic declaration and configuration substrate

- **Status**: Accepted
- **Kind**: Aspirational
- **Implementation-status**: partial — `Params` honors declared dataclass defaults and
  per-instance `default_factory` values; `Params` and `DataClass` share ordered instance-field
  discovery, exclude `ClassVar` declarations, serialize in declaration order, and preserve full
  declared public-field state across updates; `Operable` selection and legacy `OperableModel`
  materialization retain declaration order, and multiple unnamed specs are accepted; legacy
  sentinel collapse is isolated behind a closed compatibility inventory; lightweight JSON
  projection delegates nested values to LionAGI's internal serializer, and omitted `Spec` base
  types are `Undefined`; `Params`, `Meta`, and `Spec` use one typed structural equality/hash
  projection, production declarations use the explicit `eq=False` authority, and declaration,
  field-layout, sentinel, singleton, annotation, and Pydantic model caches use identity-safe keys;
  `DataClass` and every current `HashableModel` descendant are unhashable behind a closed consumer
  inventory; Pydantic materialization is owned by `PydanticSpecAdapter.materialize()` without a
  `lionagi.models` dependency, and production callers no longer construct `OperableModel` or use
  the `Operable.create_model()` compatibility edge; the dependency-light immutable registry
  composition primitive, source records, and version-bound override ledger are implemented;
  the lifecycle policy domain now composes its fixed declarations into a private immutable
  catalog behind `PolicyRegistry`; remaining domain catalogs and canonical durable serialization
  remain open
- **Area**: utilities
- **Date**: 2026-08-16
- **Relations**: extends ADR-0050 (foundational utility and typed adaptation strata); required
  by ADR-0118 (declared entity schema) and the subsequent dispatch, harness, modularity, and Run
  records

## Context

LionAGI already has the right foundational vocabulary. `Undefined` and `Unset` distinguish
absence from a present-but-unresolved parameter. `Params` and `DataClass` distinguish immutable
configuration from mutable runtime state. `Spec` describes a field without committing to a
validation framework, and `Operable` holds an ordered set of those descriptions. ADR-0050
records those intended strata accurately.

The rest of the repository nevertheless authors configuration and wire contracts as unrelated
Pydantic models, plain dataclasses, dictionaries, string enums, and hand-written serializers.
ADR-0118 would make that worse if it introduced a second frozen schema-spec hierarchy without
first making the existing one safe for schema hashes, migrations, policy snapshots, and generated
wire models.

The current foundation also contains defects that are harmless in some dynamic-model uses and
fatal when it becomes an authority.

**P1 — `Params` does not honor declared dataclass defaults.** Its custom constructor sets only
caller-provided keywords and then changes remaining `Undefined` values to `Unset`. A declared
literal default or `default_factory` is therefore bypassed. Existing agent mode parameters can
construct with `behaviors is Unset` even though the dataclass declares an empty-string default.
Configuration cannot be canonical if omitting a field produces a different value from the field
declaration.

**P2 — ordered declarations become unordered at selection boundaries.** `Operable` stores a
tuple, but `get_specs(include=...)` iterates the caller's set. `OperableModel.new_model` converts
an ordered field collection to a set again. The loss reproduces: on a four-field `Operable`
declared `alpha, beta, gamma, delta`, `get_specs(include={"delta", "alpha", "gamma"})` returns
`('delta', 'gamma', 'alpha')`, which is not a subsequence of declaration order, and different
`PYTHONHASHSEED` values reorder it again.

Today that reaches generated field order, and it does not yet reach the hash. `Params.__hash__`
routes through `hash_dict`, whose `_generate_hashable_representation` sorts mapping items, so a
set-ordered `to_dict()` produces the same digest as a declaration-ordered one. The masking is
accidental: it depends on one helper sorting on the way past, and it disappears the moment any
consumer hashes the emitted order rather than the mapping. That is an argument for D7's separate
canonical-bytes surface, not a reason to leave the loss in place, and it is the reason the defect
is currently invisible to tests. Where the order does escape unmasked, it invalidates a schema
hash, migration plan, signed policy snapshot, or generated API contract.

**P3 — equality and hashing do not share one projection.** `Meta` can compare two dictionaries
as equal while hashing their different string renderings. Equal `Meta` and `Spec` objects can
therefore occupy separate set entries. `Params.__eq__` and `DataClass.__eq__` compare only their
hashes, so a collision can make unequal values equal, and a subclass reaches that behavior only
when it declares `eq=False`; the default `@dataclass(frozen=True)` decoration shadows it with a
generated `__eq__` instead. Which of the two semantics a declaration gets is decided by a
decorator argument rather than by the base class, which D4 closes. `HashableModel` is mutable
while its hash depends on its content; mutation after insertion breaks set and dictionary
membership.

**P4 — the unnamed-field contract is accidental.** `Operable` intends to enforce uniqueness
only among declared names. It filters `None` but not `Undefined`, so two unnamed `Spec` objects
are rejected as duplicate `Undefined` names. A low-level field description should be allowed to
remain unnamed until an adapter that requires a name materializes it.

**P5 — framework and storage concerns leak back into the neutral layer.** The current
`Spec -> FieldModel -> FieldInfo -> model builder` path was duplicated by mutable
`OperableModel`, and production ReAct code constructed the latter before the D5 cutover.
ADR-0118 needs SQLAlchemy,
codec, foreign-key, index, and migration information, but placing SQLAlchemy objects or a second
storage-only field class in `ln.types` would reverse the dependency direction ADR-0050 protects.

**P6 — registration is often an import side effect.** Self-registering entities and global hook
or provider catalogs make the effective contract depend on which optional modules happened to
import first. That conflicts with deterministic schema generation and with an SDK installation
that does not load StateDB, CLI, Studio, or providers.

**P7 — the Krons reference demonstrates the intended composition but is not a source-port.** Its
field/type metadata and adapter separation are useful. It also retains set-ordered allowed keys,
content-dependent mutable hashes, and at least one validation path where a custom constructor
prevents `__post_init__` from running. Porting the files would copy the exact failure class this
record removes. The rule is the same as ADR-0118: port the pattern, not the file.

**P8 — runtime hashability is not durable identity.** Python `hash()` is process-local, callable
identity has no cross-process meaning, and omission-oriented `to_dict()` cannot both hide unresolved
fields and prove a signed/persisted snapshot is resolved. Dependent ADRs need a named byte and
digest contract rather than assuming `__hash__` or ordinary wire JSON is one.

| Concern | Decision |
|---|---|
| Absence | D1: `Undefined`, `Unset`, and `None` remain three distinct states with one shared serialization rule. |
| State shape | D2: `Params` is immutable configuration; `DataClass` is mutable runtime context; declared defaults are honored. |
| Order | D3: declaration order is canonical and survives selection, composition, serialization, hashing, and adapter emission. |
| Equality and hashing | D4: equality compares structural values, hashing uses the same canonical projection, mutable values are unhashable, and the subclass decoration that decides which implementation is in force is fixed rather than left to each declaration. |
| Field/schema composition | D5: `Spec` describes one field and `Operable` one ordered shape; domain adapters add Pydantic, SQLAlchemy, wire, or policy semantics. |
| Registries | D6: registries are explicitly composed immutable snapshots; imports never mutate the active contract. |
| Durable identity | D7: resolved snapshots have one versioned canonical-byte envelope and digest; runtime `__hash__` is never persisted. |
| Enforcement | D8: cross-process determinism, house-rule imports, compatibility, and mutation tests are merge gates. |

This record deliberately does not decide:

- the persistence entity vocabulary, physical schema, or migration algorithm; ADR-0118 owns it;
- hook phases, action authorization, provider capability policy, or Run fields; their ADRs consume
  this substrate;
- whether Pydantic remains the public SDK modeling library; it remains an adapter/materialization
  target;
- a universal base class for every LionAGI object. In particular, persistence rows do not become
  `Element` merely to reuse serialization;
- a multi-distribution package split. This record makes such a split possible but does not choose
  one.

## Decision

### D1 — Three absence states have one meaning everywhere

The canonical states remain:

```python
Undefined  # the key or field is not part of this input/projection
Unset      # the parameter exists but is unresolved; inherit or resolve later
None       # the caller explicitly supplied null / no value
```

Identity, not truthiness, distinguishes sentinels. `False`, `0`, `""`, and empty containers are
ordinary values unless a domain-specific adapter explicitly declares otherwise. New core
configuration must not enable `none_as_sentinel` or `empty_as_sentinel`.

Those two flags survive for compatibility adapters whose old contracts already collapse those
values, and the set of adapters allowed to set them is a closed enumerated list, not a capability
any adapter may claim. The list lives beside the flags as a module-level constant of call-site
identifiers; a call site absent from it raises rather than collapsing, and the D8 gate fails a
diff that sets either flag from a site the list does not name. Adding a site is an edit to that
constant, reviewed as a contract change. Otherwise the carve-out grows by usage and the
three-state rule becomes advisory in exactly the places that already had the loosest contracts.

The supported public helpers implement identity semantics only and reject either legacy flag.
`Params` and `DataClass` resolve the first class that declares their `ModelConfig` and validate
each enabled axis against the inventory before applying it. Named direct adapters use the same
private gateway. A static repository gate binds each private call's literal identifier to its
lexical owner and requires the discovered inventory to equal the constant. The identifier is an
architectural compatibility control, not a security credential: importing a private function and
spoofing its string is unsupported, and frame inspection is deliberately avoided. The gate covers
supported direct imports, declarations, re-exports, and assignments; reflective dynamic import,
`eval` / `exec`, and deliberate runtime metadata forgery are outside this non-security contract.

The wire rule is explicit:

```python
def to_dict(
    self,
    exclude: Collection[str] | None = None,
    *,
    mode: Literal["python", "json"] = "python",
) -> dict[str, Any]: ...
```

- `Undefined` and `Unset` are omitted from this application/wire projection only; omission does not
  make the object eligible for a durable identity.
- `None` is emitted as `None`/JSON `null`.
- Enum conversion, paths, timestamps, UUIDs, and nested models use the selected internal
  serialization adapter; individual models do not hand-write a second recursion. Pydantic values
  use their JSON-mode serializers. Raw bytes remain rejected until D7 defines their versioned
  durable representation.
- JSON projection rejects non-finite numbers instead of allowing a serializer to turn them into a
  `null` indistinguishable from explicit `None`.
- Deserialization never guesses `Unset` from `null`. A missing key and a present null key remain
  distinguishable.
- Omitting `Spec.base_type` stores `Undefined`; explicit `Spec(None)` is invalid. Legacy
  `FieldModel(annotation=None)` is normalized to `Unset` only at that adapter boundary.
- Omission belongs to the declaring model's projection. Code must call `to_dict(mode="json")`
  before handing an unresolved lightweight declaration to the raw JSON encoder; a raw dataclass
  traversal has no field-owner contract and fails on a nested sentinel rather than inventing one.
- `Spec.metadata` entries are nested values, not independently declared `Spec` fields. A sentinel
  stored inside `Meta.value` therefore fails closed until the caller resolves or excludes that
  metadata entry; core projection does not silently drop a default, validator, or other contract.
- Core JSON projection does not invent a wire identity for a resolved Python `type`, validator, or
  default factory. Those values fail closed until a D5 target adapter materializes them or D7
  encodes a versioned `CallableRef`.
- Patch/update APIs may use `Unset` to mean “leave unchanged”; create APIs resolve every required
  `Unset` before crossing their owner boundary.
- A persisted or signed snapshot uses D7's distinct strict `to_snapshot(require_resolved=True)`
  surface, contains no sentinel, and raises a typed resolution error naming every unresolved
  `Unset` field path.

This gives provider inheritance, optional MCP lists, permission profiles, schema defaults, and
Run snapshots the same absence semantics. A surface that needs a fourth state defines a domain
enum rather than reinterpreting a sentinel.

### D2 — `Params` is immutable configuration and `DataClass` is mutable context

`Params` and `DataClass` share field discovery, default application, validation, and
serialization mechanics through private helpers. They differ deliberately in mutability and
hashability.

Target constructor behavior is equivalent to dataclass construction:

```python
@dataclass(frozen=True, slots=True, init=False, eq=False)
class Params:
    def __init__(self, **kwargs: Any) -> None: ...

@dataclass(slots=True)
class DataClass:
    def __post_init__(self) -> None: ...
```

For each declared field, in declaration order:

1. use the supplied keyword when present;
2. otherwise call its `default_factory` once when one exists;
3. otherwise use its declared literal default;
4. otherwise use `Unset` only when `prefill_unset=True`;
5. otherwise leave it `Undefined`;
6. when `strict=True`, reject a remaining sentinel with a field-qualified error.

Unknown keywords are rejected before any factory runs. A factory that raises propagates its
exception without leaving a partially returned object. Factories are never executed at class
definition time or canonical-serialization time.

The ownership rule is:

- `Params`: immutable configuration, registration, dispatch policy, permission policy,
  `EntitySpec`, `RunSpec`, and adapter options;
- `DataClass`: mutable invocation context, accumulating evidence, counters, and transient
  execution state;
- Pydantic model: validated public/wire DTO when Pydantic behavior is part of the boundary;
- `Element`: runtime object that actually needs LionAGI identity, creation time, and polymorphic
  envelope;
- plain dataclass: local implementation detail with none of the substrate behavior.

`DataClass` and its descendants are mutable and unhashable. Python does not allow a frozen
dataclass to inherit this non-frozen base; immutable configuration belongs in `Params`, while an
independently frozen local type may opt into its own explicitly reviewed structural authority.
Normal decorated subclasses retain dataclass-generated exact-class equality. The base also keeps
an exact-class, public-field structural equality fallback for a compatibility subclass that
explicitly requests `eq=False`; neither path grants a hash.

### D3 — Declaration order is canonical

Field order comes from `dataclasses.fields(cls)` and `Operable.__op_fields__`, never a set or
mapping created along the way.

The compatibility and ordered APIs are distinct:

```python
@classmethod
def field_names(cls) -> tuple[str, ...]: ...

@classmethod
def allowed(cls) -> frozenset[str]: ...  # membership compatibility only

class Operable:
    def get_specs(
        self,
        *,
        include: Collection[str] | None = None,
        exclude: Collection[str] | None = None,
    ) -> tuple[Spec, ...]: ...
```

`to_dict()` iterates `field_names()`. `get_specs(include=...)` treats include as membership and
filters the stored tuple, so output order is declaration order regardless of whether the caller
passed a set, list, or tuple. `exclude` does the same. Supplying both remains an error. Unknown
names are reported in deterministic sorted order.

Composition is left-biased and explicit:

```python
def compose(*operables: Operable, name: str | None = None) -> Operable: ...
```

- specs appear in operand order and within each operand's declaration order;
- duplicate declared names are rejected with both source positions;
- multiple unnamed specs are permitted;
- an adapter requiring names rejects unnamed specs at materialization time;
- no implicit alphabetical sort occurs;
- a domain that needs a different order creates an explicit reordered `Operable`, making the
  semantic change visible in its hash and review diff.

Canonical JSON sorts mapping keys only inside values whose mapping order is not semantic. It does
not sort declared field arrays, index key arrays, hook stages, policy chains, or graph edges whose
order carries meaning.

### D4 — Equality and hash use one structural projection

`Params`, `Spec`, `Meta`, and immutable registry values expose one private recursive projection.
It separates three questions that raw Python tuples conflate: whether two values are structurally
equal, whether the live graph is safe to hash, and whether retaining a key can safely share a
materialized cache entry:

```python
@dataclass(frozen=True, slots=True)
class _StructuralKey:
    value: Hashable
    hash_safe: bool
    cache_stable: bool

def _structural_key(value: Any) -> _StructuralKey: ...
def _try_stable_cache_key(value: Any) -> _StructuralKey | None: ...

def __eq__(self, other: object) -> bool:
    return type(self) is type(other) and self._key() == other._key()

def __hash__(self) -> int:
    return hash(self._key().require_hashable())
```

The projection rules are:

- every scalar carries an exact type tag, so `True`, `1`, and `1.0` are distinct; floats use their
  IEEE-754 bits, including signed zero and NaN payload; `Ellipsis` remains an immutable singleton;
- mappings become key/value projections sorted by framed structural order tokens; lists and tuples
  preserve order and retain their different collection kinds; sets and frozensets are sorted by
  those same tokens and retain their different kinds;
- a live `dict`, `list`, or `set` remains structurally comparable but makes its owner unhashable
  and cache-ineligible. A tuple or frozenset is safe only when every descendant is safe. This
  prevents a shallow-frozen `Params` from changing hash after a nested mutation. User collection
  subclasses remain opaque because they can attach or expose additional mutable state;
- enums include their concrete enum type and canonicalized value;
- typing forms recurse through their arguments; the interpreter-owned parameter list exposed by
  `Callable[[...], result]` is normalized as immutable syntax rather than mistaken for user state,
  and the available public `Any`/`NoReturn`/`Never`/`Self`/`LiteralString` singletons have explicit
  tags across their different Python 3.10–3.14 runtime representations;
- the closed stdlib `pathlib` concrete types and exact `UUID` are recognized immutable value
  atoms: their concrete type is part of the key, POSIX path parts retain case, and Windows path
  parts use the case-lowering of path equality. User subclasses remain opaque/cache-ineligible
  because they may add mutable state;
- dataclass owners include exact concrete type identity and their declared fields; mutable
  dataclasses and Pydantic models are structurally comparable but not hash/cache-safe;
- callables compare and hash by strong identity. Plain functions and types may be cache-stable;
  bound methods, partials, and callable instances are not, because sharing would retain mutable
  receiver state. Their unordered runtime ordering token uses that same identity, never mutable
  presentation attributes such as `__module__` or `__qualname__`;
- unsupported unhashable opaque values compare by identity and raise
  `UnhashableStructuralValueError` with their path when a hash is requested; cycles fail with the
  same typed path error. Cache admission converts that error into an uncached materialization,
  never a user-visible construction failure.
- the two Lion sentinels are recognized by exact singleton identity, never by spoofable module or
  class names;
- unsupported hashable opaque values also compare by identity and are cache-ineligible: arbitrary
  user hash/equality implementations are not treated as proof of recursive immutability.

The substrate no longer calls the public compatibility hashes in `lionagi/ln/_hash.py`, whose
unrecognized-value path renders through `str(item)` and then `repr(item)`. Those public helpers
remain available for audited compatibility consumers until their own migration; D4 deletes the
substrate dependency, not the public API.

Hash equality is never used as object equality. Equal objects must hash equal; unequal objects
may still collide without becoming equal. Structurally equal values containing mutable containers
are both unhashable; the Python equal-hash invariant therefore does not license a mutable content
hash.

The same projection owns declaration-cache admission. `Spec` and `FieldModel` share one bounded
annotation cache keyed by the exact whole declaration; effective sentinel/nullable policy is
applied atomically only on a miss. Stable Pydantic model declarations use the same atomic
get-or-create contract, so concurrent callers receive one class identity. The materializer uses
the runtime's uncached `typing._AnnotatedAlias` constructor behind one private compatibility
function because public
`typing.Annotated[...]` has its own equality-based global cache and can return the wrong origin for
custom metaclasses. Python 3.10–3.14 contract tests pin that isolated private dependency. Pydantic
model keys include adapter-class identity, base-model identity, model name, the final ordered
declaration projection, and documentation; the already-projected spec order makes raw
`include`/`exclude` selectors redundant. A separate bounded stable-declaration projection cache
keeps those recursive checks off hot cache-hit paths. Field-layout, sentinel-policy, and
sentinel-singleton caches likewise wrap class objects in strong identity keys so permissive
metaclass equality cannot cross-wire their values.

**The decoration contract is part of this decision.** The base implementations above are reachable
only when a subclass does not shadow them, and today's subclasses decide that by accident. A
`@dataclass(frozen=True)` subclass of `Params` generates its own `__eq__` and `__hash__` and those
win over the base ones. Two equality semantics are therefore live in the package at once, selected
by a decorator argument:

| Subclass decoration | `__eq__` in force | Observable behavior |
|---|---|---|
| `@dataclass(frozen=True)` (default `eq=True`) | dataclass-generated | field-tuple equality requiring the same class; `hash()` raises `TypeError` when any field value is unhashable, including values the base `__hash__` handles |
| `@dataclass(frozen=True, eq=False)` before D4 | inherited `Params.__eq__` | hash equality; two *different* `Params` subclasses holding equal fields can compare equal |

Neither column is what D4 specifies. The decision is therefore:

- `Params`, `Spec`, and `Meta` subclasses declare `eq=False` so the structural implementations are
  the ones in force, and the structural `__eq__` compares concrete type first, which closes the
  cross-subclass equality shown in the second row;
- the house-rule gate in D8 fails a subclass of these bases that is decorated with `eq=True` or
  that defines its own `__eq__` or `__hash__` without declaring an override reason;
- the audit that precedes the `HashableModel` split covers `Params` subclasses too, because a
  subclass that relies on the first row's same-class requirement changes behavior when the
  structural implementation takes over.

The closed audit found no immutable production `HashableModel` consumer: the base and all nine
descendants permit assignment, and `OperableModel` additionally mutates its field declarations.
The entire current family is therefore unhashable. The historical public class name remains as a
serialization compatibility surface, not a hash guarantee. A future immutable Pydantic owner must
enter the inventory with a separately reviewed exact structural authority; it may not inherit or
recreate a content hash from this mutable base.

### D5 — `Spec` and `Operable` are the only neutral declaration vocabulary

`Spec` continues to describe one logical field:

```python
Spec(
    base_type,
    name="run_id",
    nullable=False,
    default=...,
    validator=...,
    metadata=(...),
)
```

`Operable` continues to describe one ordered collection of fields. Neither imports Pydantic,
SQLAlchemy, StateDB, Studio, or a provider SDK merely to store the declaration.

Domain information is namespaced metadata interpreted by an adapter. Common metadata remains
limited to framework-neutral meaning. For example, the persistence adapter may interpret:

```text
storage.column_name
storage.physical_type.sqlite
storage.physical_type.postgresql
storage.primary_key_position
storage.foreign_key
storage.bind_codec
storage.result_codec
```

Those keys do not make `Spec` itself a SQL column. A policy adapter may interpret a different
namespace, and an external-hook adapter another. Unknown namespaced metadata is preserved by the
neutral layer and either rejected or ignored according to the chosen adapter's closed contract.

The materialization seam is additive to the compatibility ABC:

```python
class SpecAdapter(ABC):
    @classmethod
    def materialize(cls, declaration: Operable, /, **options: Any) -> Any: ...
```

The base implementation delegates to an existing subclass's `create_model()`, so third-party
compatibility subclasses do not become abstract or unusable. `PydanticSpecAdapter` reverses that
authority locally: `materialize()` owns construction and deprecated `create_model()` delegates to
it.

Concrete adapters own validation needed by their target:

- Pydantic adapter rejects unnamed fields and emits `FieldInfo`/model types;
- persistence adapter combines an `Operable` with immutable `EntitySpec` table/index/codec
  parameters and emits SQLAlchemy objects;
- wire adapter emits a versioned JSON-compatible DTO/schema;
- policy adapter compiles a neutral policy declaration for a concrete harness/provider.

`FieldModel` and `OperableModel` remain deprecated compatibility materializations for the
published window. New architecture does not extend them as a parallel declaration stack. ReAct
and the other production callers use neutral declarations plus the explicit target adapter.

### D6 — Registries are explicit immutable snapshots

A declaration module exports values. It does not mutate the active registry when imported.

```python
@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryEntry(Params, Generic[ItemT]):
    key: str
    value: ItemT

@dataclass(frozen=True, slots=True, init=False, eq=False)
class RegistryFragment(Params, Generic[ItemT]):
    owner: str
    version: str
    items: tuple[RegistryEntry[ItemT], ...]
    feature: str | UnsetType = Unset

@dataclass(frozen=True, slots=True, init=False, eq=False)
class Registry(Params, Generic[ItemT]):
    @classmethod
    def compose(cls, *fragments: RegistryFragment[ItemT],
                name: str, version: str | UnsetType = Unset) -> Registry[ItemT]: ...
```

The composed `items` are source-bearing `RegistryRecord` values. The snapshot also retains the
exact input fragments and an ordered `RegistryOverride` history containing both the displaced and
replacement entries. Callers cannot supply override rules to `compose()`: a registry subtype owns
an immutable `ClassVar` allowlist whose rule names the key, incumbent and replacement owners and
fragment versions, registry contract version, and rule version. `Registry` construction and
`with_updates()` are closed; `compose()` is the only invariant-forming entry point.
Composition-authority subtypes inherit from one `Registry[T]` base, not an ABC or mixin; a reusable
typed subtype uses `class Catalog(Registry[ItemT])`. A subtype may add frozen, structurally stable
`ClassVar` metadata, which remains outside snapshot fields and projection, but cannot add instance
fields or methods. Subtypes are declaration-only authorities, not domain facades. Domain lookup
helpers and cross-entry validation live in a separate facade; when those invariants exist, the
facade is the exported composition root and encapsulates both its private marker subtype and raw
`Registry` snapshot. It returns domain projections or the facade, never the marker instance as the
public authority, so callers cannot recover inherited `compose()` through `type(snapshot)` and
bypass domain validation.

Composition occurs at a named application boundary:

```python
entity_registry = EntityRegistry.compose(core_state_entities, studio_entities)
hook_profiles = ExternalHookProfiles.compose(builtin_profiles, enabled_plugin_profiles)
tool_catalog = ToolCatalog.compose(builtin_tools, selected_mcp_tools)
```

Exact semantics:

- fragment and item order are explicit and stable;
- fragment owners are unique within a composition, including empty fragments;
- duplicate canonical keys fail with both owners named unless the registry type declares a
  versioned override rule;
- an override is recorded in the runtime registry value and structural hash; D7 later includes
  that history in the canonical content digest;
- optional modules contribute a fragment only when the composition root enables them;
- the generic registry never imports, probes, or infers an optional feature. A miss returns the
  caller's default or raises `KeyError`; the ADR-0122 loader or domain facade, which knows the
  omitted manifest/distribution, owns the typed missing-feature error;
- a registry value is immutable and safe as a runtime catalog. Durable attachment to a Run,
  schema plan, persisted policy, or signed record waits for D7's resolved snapshot envelope,
  `CallableRef`, canonical bytes, and content digest;
- dynamic plugin enablement produces a new snapshot value; it does not mutate a snapshot being
  used by an in-flight operation. `Registry.version` is the contract version used to authorize
  override rules, not a content digest; D7 supplies content identity;
- module reload does not duplicate registrations because imports do not register.

This rejects decorator-driven global self-registration for schema, hooks, providers, and tools.
Decorators may tag declarations; the composition root must still collect an explicit declared
set.

`lionagi.ln.types.registry` is the dependency-light reference implementation for this decision.
`lionagi/state/lifecycle/policy.py` is the first migrated domain. Its explicit builder preserves
the fixed seven-policy order, `PolicyRegistry.seal()` composes a private tuple-backed declaration
fragment, and the unchanged facade returns cached `LifecyclePolicy` projections with
`ImmutableEdgeMap` compatibility. The old entity/table dictionaries are deleted. Other domains
follow this pattern rather than exporting the raw marker subtype or snapshot.

### D7 — durable snapshots are distinct from wire serialization and runtime hash

`to_dict()` remains an omission-oriented application/wire projection. Durable schema, policy,
registry, plan, and Run identities use a separate strict surface:

```python
@dataclass(frozen=True, slots=True, init=False, eq=False)
class SnapshotEnvelope(Params):
    domain: str
    contract_version: str
    payload: JsonValue

def to_snapshot(value: Any, *, domain: str, contract_version: str,
                require_resolved: bool = True) -> SnapshotEnvelope: ...
def canonical_bytes(snapshot: SnapshotEnvelope) -> bytes: ...
def canonical_digest(snapshot: SnapshotEnvelope) -> str: ...  # sha256-v1
```

`require_resolved=True` rejects every nested `Unset` with its field path. `Undefined` is omitted
only because the field is outside that projection; `None` remains explicit JSON null. The envelope
contains a namespaced domain and contract version so identical payloads in different authorities
do not share an accidental identity. `canonical_bytes` is UTF-8 canonical JSON through LionAGI's
internal serializer: mapping keys sorted by encoded key, semantic sequences preserved, no NaN or
non-finite numbers, and normalized versioned adapters for timestamps, paths, enums, bytes, and
identifiers. `canonical_digest` is lowercase SHA-256 over those exact bytes and records algorithm
`sha256-v1` beside every persisted reference.

Python `_structural_key()` and `__hash__` remain runtime equality/container mechanics. Their
integer result is never serialized, persisted, signed, compared across processes, or used as a
schema/policy/plan identity.

Raw callable objects are forbidden in durable declarations. Runtime-only `Spec` values may retain
callable identity semantics, but any validator/codec/compiler function that contributes to a
snapshot is an immutable `CallableRef(namespace, name, version, implementation_digest)` resolved
through an explicit registry. Missing, ambiguous, or digest-mismatched resolution fails before
materialization; importing a module path is not itself proof of the referenced implementation.

### D8 — Determinism and house rules are executable gates

The foundation suite adds the following required matrices.

**Defaults and absence**

- literal default, default factory, supplied value, `Unset`, `Undefined`, and explicit `None`;
- factory called once per instance;
- strict and compatibility serializer modes;
- nested path in unresolved-sentinel errors.

**Order and canonical serialization**

- include/exclude passed as list, tuple, set, and frozenset yields declaration order;
- composed Operables retain operand order;
- separate processes with at least four `PYTHONHASHSEED` values emit byte-identical canonical
  bytes and equal `sha256-v1` digests for schemas, policies, and registries;
- intentional reorder changes the canonical digest;
- unresolved nested `Unset`, a raw callable, and a mismatched `CallableRef` digest each fail strict
  snapshot construction before bytes are emitted.

**Equality/hash**

- equal dictionaries with different insertion order produce equal `Meta` values, while both
  remain explicitly unhashable because the live dictionaries are mutable;
- unequal values remain unequal even under an injected hash collision;
- immutable sequences and frozensets produce equal runtime hashes for equal projections within a
  process; mutable dict/list/set descendants make `Params`/`Meta`/`Spec` unhashable;
- `DataClass`, its production descendants, and the current mutable `HashableModel` family are
  unhashable; the inventory currently contains no immutable Pydantic exception;
- callable metadata retains identity semantics;
- unsupported opaque mutable metadata fails explicitly;
- `True`, `1`, `1.0`, signed zero, sentinel states, enum types, and custom-metaclass type identities
  remain distinct where their typed runtime meaning differs;
- annotation/model caches reuse repeated stable declarations, bypass mutable/bound-method values,
  and never cross-wire adapter, `Spec`, or base-type subclasses;
- two different subclasses of one base holding equal field values are unequal, which is the
  cross-subclass equality the pre-migration `eq=False` decoration admits;
- a subclass of `Params`, `Spec`, or `Meta` decorated `eq=True`, or defining `__eq__` or
  `__hash__` outside the three base authorities, fails a closed static check rather than silently
  selecting generated implementations;
- a new `DataClass` or `HashableModel` descendant fails the closed mutable-model inventory until
  it records its mutation surface and set/dict-key or transient-dedup consumers; current entries
  declare no production set/dict-key consumer, and listable operation DTOs deduplicate by
  identity-or-equality without restoring a model content hash;
- `none_as_sentinel` and `empty_as_sentinel` are set only from call sites named in D1's
  enumerated list, and a diff that sets either elsewhere fails.

**Adapter separation**

- importing `lionagi.ln.types` does not import Pydantic, SQLAlchemy, StateDB, Studio, provider
  SDKs, CLI, adapters, models, operations, or session modules;
- Python-mode declaration projection preserves order and live Python values for in-process
  adaptation; it is not a reconstructable transport encoding. JSON projection uses LionAGI's
  internal serializer and fails closed when a Python type, callable, or unresolved identity has no
  wire representation. Durable reconstructable JSON, canonical bytes/digests, and `CallableRef`
  resolution remain D7's separate snapshot contract;
- target-specific validation occurs only when the corresponding adapter is invoked;
- no new raw `asyncio`, `json`, schema-library, or serialization helper is introduced outside the
  approved `lionagi.ln` seams.

**Registry composition**

- same-fragment and cross-owner duplicate keys report the key, both owners, and declared
  positions deterministically; repeated owners fail even when their keys do not overlap;
- an override matches the exact key, current owner/version, replacement owner/version, and
  registry version; ambiguous matches and an unversioned registry fail closed;
- chained overrides preserve the first key slot and retain ordered displaced/replacement history;
- composing base and optional profiles in one process, concurrently, or after module reload does
  not mutate the fragments or any already-composed snapshot;
- imports of `lionagi.ln.types.registry` load no adapter, model, operation, provider, session,
  state, Studio, Pydantic, or SQLAlchemy layer and expose no mutable registration API or hidden
  collector.

**Compatibility**

- existing public imports remain during their declared window;
- current Pydantic output is characterized before adapter replacement;
- persisted/wire payload fixtures are dual-read before old forms are removed;
- a migration that changes canonical bytes increments the owning contract version.

## Implementation sequence

1. Add failing characterization tests for current defaults, unnamed specs, selection order,
   equality/hash, mutable hashing, and import boundaries.
2. Introduce shared field/default discovery and structural-key helpers without changing public
   signatures.
3. Fix `Params` defaults and ordered serialization; add `field_names()` while retaining
   `allowed()` as membership compatibility.
4. Fix `Operable` selection and unnamed-spec handling; remove set conversion from model
   materialization.
5. Correct `Meta`/`Spec` equality and hash; close the mutable-model consumer inventory and make
   the current `DataClass`/`HashableModel` families unhashable.
6. Move production callers from `OperableModel` and the lazy `Operable.create_model()` edge to
   neutral declarations and explicit adapters. (Delivered for current production callers.)
7. Introduce the immutable registry primitive, then migrate each domain at its named composition
   root behind its compatibility facade. (Primitive and lifecycle-policy domain delivered;
   remaining domain migrations remain open.)
8. Land strict snapshot envelopes, canonical bytes/digest, and versioned CallableRef resolution.
9. Only then allow ADR-0118 schema hashes, harness policy snapshots, dispatch policies, and Run
   contracts to depend on these primitives.

No schema, permission, or Run migration may quietly bundle steps 1-5. The foundation changes
have a high fan-in and receive their own compatibility review.

## Consequences

- New architecture reuses LionAGI's own concepts rather than adding frozen dataclass and Pydantic
  forests beside them.
- Schema hashes, policy snapshots, and generated API contracts become reproducible across
  processes.
- Absence and inheritance stop changing meaning between AgentSpec, MCP scope, provider config,
  StateDB patches, and frontend payloads.
- Explicit registry composition makes optional loading and test isolation possible.
- Some existing accidental behavior changes: defaults begin working, unnamed specs stop
  colliding, and include selection becomes declaration-ordered. Those changes require targeted
  compatibility tests and release notes even though they are bug fixes.
- Mutable models lose convenient hashing. Code relying on that unsound behavior must use stable
  identity or an immutable snapshot key.
- Adapters become slightly more explicit at call sites. That cost buys a dependency boundary and
  prevents storage/provider details from entering `ln.types`.
- Reversal would be expensive after schema and policy hashes are persisted, so canonicalization
  is versioned from its first durable use.

## Alternatives considered

### Leave the primitives as convenience helpers and create new architecture-specific models

This minimizes immediate changes to high-fan-in code. It also creates parallel absence,
serialization, ordering, equality, and adapter rules in every ADR. That is the current failure
mode at a new layer and was rejected.

### Port the Krons type and schema files

Krons demonstrates useful metadata and adapter composition. Executed probes found the same
set-order and mutable-hash hazards, plus validation bypass caused by custom construction. A bulk
port would import defects and PostgreSQL assumptions while obscuring which LionAGI contract was
chosen. Patterns are referenced; files are not copied.

### Use Pydantic models for every declaration and runtime context

Pydantic gives strong public validation and JSON Schema emission. It also makes the lowest layer
framework-dependent, encourages mutable models to masquerade as immutable specs, and does not by
itself define deterministic registry composition or storage semantics. Pydantic remains a
materialization target.

### Use ordinary frozen dataclasses and `dataclasses.asdict`

This would honor defaults and order, but it collapses sentinel and codec behavior, recursively
copies values with no versioned wire policy, and provides no common adapter/registry contract.
The existing types are retained and corrected instead.

### Self-register declarations at import time

Self-registration is terse and works in a monolithic eager import. It makes schema and policy
contents depend on optional imports, plugin order, test history, and module reload. That is
incompatible with deterministic hashes and conditional loading, so explicit composition wins.

### Sort every collection before hashing

Sorting masks nondeterminism but destroys semantic order for fields, interceptor stages, index
keys, and graph edges. Only semantically unordered mappings and sets are canonical-sorted; ordered
declarations retain their order.

## Notes

The word “BaseModel” is intentionally absent from the foundational ownership table. LionAGI may
continue to export Pydantic's `BaseModel`, but it is a framework type, not the architectural base
for configuration, mutable execution state, identity, schema declarations, and persistence rows
all at once.
