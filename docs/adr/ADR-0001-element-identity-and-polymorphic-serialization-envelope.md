# ADR-0001: Element Identity and Polymorphic Serialization Envelope

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: none

## Context

LionAGI needs persisted and externally addressable model objects to retain identity across
collections, graph relationships, messages, logs, and serialization boundaries. Five concrete
problems shape the shipped model.

**P1 — Addressable objects need stable identity independent of their current values.** A node can
change content or metadata while references held by a Pile, Graph, log, or operation still need to
identify the same object. Value equality would make identity depend on mutable fields; object
identity would be lost at a serialization boundary.

**P2 — A serialized base type must recover the concrete model.** Callers frequently hold
`Element`, `Node`, or heterogeneous Pile references while the payload contains an Instruction,
Graph, Event, or extension subclass. Reconstructing only the statically requested base class would
either reject subtype fields or discard subtype behavior.

**P3 — Several persisted shapes and naming generations must remain readable.** Normal model dumps
use `metadata`; database adapters use `node_metadata`. Current writers store a fully qualified
class name, while older built-in payloads may contain a short name such as `Instruction`. The
reader therefore cannot be a single direct registry lookup.

**P4 — The envelope has a trust and extension boundary.** `Node` subclasses register
automatically, other `Element` subclasses do not, and a fully qualified discriminator may cause a
Python module import. That behavior makes third-party recovery possible, but it also means
deserialization is not a passive data-only operation.

**P5 — Graph nodes need a stable content shape without making every value object a node.** `Node`
adds arbitrary content and a nullable embedding to the Element envelope. Configuration and query
objects do not become addressable merely because they are Pydantic models, and can remain plain
models.

The current implementation is defined by `lionagi/protocols/generic/element.py`,
`lionagi/_class_registry.py`, and `lionagi/protocols/graph/node.py`.

| Concern | Decision |
|---|---|
| Object identity and base fields | D1: Addressable framework models inherit the frozen UUID and creation timestamp plus mutable metadata from `Element`. |
| Serialized envelope | D2: Writers place a fully qualified `lion_class` discriminator in metadata and support Python, JSON, and database dictionary shapes. |
| Concrete-class recovery | D3: Readers resolve registry entries, importable dotted paths, and a fixed legacy built-in short-name set in that order. |
| Node payload | D4: `Node` adds serializable arbitrary content and a nullable float-list embedding while retaining the Element envelope. |

This ADR deliberately does **not** decide:

- node lifecycle policy such as versioning, soft deletion, or content hashing; those features are
  activated by per-class node configuration and are not required by the identity envelope;
- adapter-specific database schemas; adapters consume database-mode dictionaries but own their
  table and transport contracts;
- vector indexing or retrieval behavior; `Node.embedding` is a stored compatibility field, not a
  declaration that every Node is searchable by vector; or
- a future trusted plugin-registration policy; the current asymmetric registry/import behavior is
  recorded here, while its replacement remains an explicit delta.

## Decision

### D1 — Element supplies identity, creation time, and metadata

`Element` is the common identity envelope for addressable or persisted LionAGI models. It is a
Pydantic model and the nominal `Observable` used by generic collections.

**The contract** (`lionagi/protocols/generic/element.py`):

```python
class Element(BaseModel, Observable):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    id: UUID = Field(default_factory=uuid4, frozen=True)
    created_at: float = Field(
        default_factory=lambda: now_utc().timestamp(),
        frozen=True,
    )
    metadata: dict = Field(default_factory=dict)

    @property
    def created_datetime(self) -> datetime: ...

    @classmethod
    def class_name(cls, full: bool = False) -> str: ...
```

**Exact semantics**:

- With no input, `id` is a UUID4, `created_at` is the current UTC timestamp expressed as a float,
  and `metadata` is a new empty dictionary.
- `id` accepts a UUID or a string accepted by `UUID(str(value))`. Invalid values fail Pydantic
  validation. The field cannot be assigned after construction because it is frozen.
- `created_at=None` produces a new current timestamp. A float is retained; a `datetime` uses its
  `.timestamp()`; a string is first parsed as ISO text after replacing a space with `T`, and a
  timezone-naive parsed string is treated as UTC. If ISO parsing fails, a numeric string is
  converted with `float`; failure of both string conversions becomes
  `ValueError("Invalid datetime string: ...")`. Other numeric-like inputs use `float(value)`;
  failures become `ValueError("Invalid created_at: ...")`. The field is frozen after construction.
- `created_datetime` always projects the stored float as a timezone-aware UTC `datetime`.
- Falsey metadata becomes `{}`. A non-dictionary input is passed through the recursive `to_dict`
  utility; failure to obtain a dictionary raises `ValueError("Invalid metadata.")`.
- If input metadata already contains `lion_class`, it must equal the concrete validating class's
  fully qualified name. A mismatch raises `ValueError("Metadata class mismatch.")`.
- Unknown top-level model fields are rejected (`extra="forbid"`); arbitrary values remain allowed
  inside declared fields such as `metadata` and Node content.
- Equality between two Elements compares only UUIDs. Comparing to a non-Element returns
  `NotImplemented`. Hashing uses the UUID, and every Element is truthy even when a subclass also
  represents an empty collection. Collection subclasses may deliberately override truthiness.

The negation matters: without one identity field, Pile keys and Graph endpoints would each need
their own identity convention, and equality of mutable objects would be unsuitable for references.

### D2 — Serialization carries a concrete-class discriminator in metadata

Element has three dictionary modes and one JSON form. The discriminator is not a separate top-level
field; it is injected into the serialized metadata copy.

**The contract** (`lionagi/protocols/generic/element.py`):

```python
def to_dict(
    self,
    mode: Literal["python", "json", "db"] = "python",
    db_meta_key: str | None = None,
    **kw,
) -> dict: ...

def to_json(self, decode: bool = True, **kw) -> str: ...

@classmethod
def from_dict(cls, data: dict) -> Element: ...

@classmethod
def from_json(cls, json_str: str) -> Element: ...
```

The canonical Python shape is:

```python
{
    "id": "00000000-0000-0000-0000-000000000001",
    "created_at": 1.0,
    "metadata": {
        "source": "example",
        "lion_class": "lionagi.protocols.generic.element.Element",
    },
}
```

Database mode changes only the metadata key by default:

```python
{
    "id": "00000000-0000-0000-0000-000000000001",
    "created_at": 1.0,
    "node_metadata": {
        "source": "example",
        "lion_class": "lionagi.protocols.generic.element.Element",
    },
}
```

**Exact semantics**:

- Python mode uses Pydantic `model_dump(**kw)`, adds the concrete fully qualified class name to the
  dumped metadata, and removes sentinel-valued fields. It does not intentionally add
  `lion_class` to the live object's metadata.
- JSON mode serializes through `orjson` and parses the bytes back to JSON-compatible Python values;
  UUIDs therefore appear as strings.
- Database mode follows JSON mode, removes `metadata`, and installs it under `db_meta_key` or the
  default `node_metadata`.
- Any mode other than `python`, `json`, or `db` raises `ValueError("Unsupported mode: ...")`.
- `to_json(decode=True)` returns text. `decode=False` returns the underlying JSON bytes even though
  the public annotation says `str`; callers in `to_dict` rely on those bytes.
- `from_json` parses with `orjson` and delegates all recovery behavior to `from_dict`.
- `from_dict` shallow-copies the top-level dictionary and separately copies the chosen metadata
  dictionary before removing `lion_class`; the caller's input dictionaries are not mutated.
- When both metadata keys are present, `node_metadata` is selected first. The recovered in-memory
  metadata does not retain `lion_class`; the discriminator is regenerated on the next write.
- If no concrete subclass delegation occurs, reconstruction ends with `cls.model_validate(data)`.

The discriminator is fully qualified because short class names are ambiguous across extension
modules. Keeping it inside metadata preserves the stable three-field base envelope and lets older
database shapes move metadata as one unit.

### D3 — Class recovery is registry-first, import-capable, and legacy-aware

The resolver is deliberately compatibility-oriented rather than a uniform extension registry.

**The contract** (`lionagi/_class_registry.py`):

```python
LION_CLASS_REGISTRY: dict[str, type] = {}

def get_class(class_name: str) -> type: ...
```

Resolution proceeds in this exact order:

1. Return an exact `LION_CLASS_REGISTRY[class_name]` hit.
2. If the string contains `.`, split it at the last dot and attempt to import that module attribute;
   return it only when it is a class.
3. Import each module in the fixed `_BUILTIN_MODULES` tuple and look for an attribute matching the
   short name.
4. Re-scan registry keys for an exact short-name or `.<short-name>` suffix match, because importing
   built-ins may have registered Node subclasses.
5. Raise `ValueError("Unable to find class ...")`.

`Element.from_dict` removes the discriminator, asks `get_class`, restores the remaining metadata,
and delegates to the resolved type's `from_dict` whenever the resolved type differs from the class
currently reconstructing or supplies a different implementation. Resolver/import errors are
caught for a final direct dotted import attempt; an unresolved or malformed discriminator then
propagates an import, attribute, type, or value error rather than silently constructing the wrong
base type.

`Node` provides the only automatic registration hook:

```python
class Node(Element, Relational, AsyncAdaptable, Adaptable):
    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        LION_CLASS_REGISTRY[cls.class_name(full=True)] = cls
```

**Exact semantics**:

- The Node base itself is not inserted by its own subclass hook; each concrete Node subclass is.
- Registration is ordinary dictionary assignment. A second class with the same fully qualified
  key silently replaces the first; last writer wins.
- Non-Node Element subclasses do not auto-register. Built-in classes remain recoverable through
  module attribute lookup; importable third-party classes remain recoverable through dotted paths.
- A short-name suffix collision is resolved by registry insertion order, not by an explicit
  ambiguity error.
- Dotted-path recovery executes Python import machinery. Payloads must therefore come from a trust
  context permitted to name importable classes; the code does not maintain an allow-list.

This shape grew from two compatibility needs: qualified names for extensions and continued reads
of older built-in short names. It buys recovery breadth at the cost of an asymmetric registration
and trust model, which is retained as a delta rather than described as ideal.

### D4 — Node adds content and a compatibility embedding field

`Node` is the graph-addressable specialization of Element.

**The contract** (`lionagi/protocols/graph/node.py`):

```python
class Node(Element, Relational, AsyncAdaptable, Adaptable):
    node_config: ClassVar[Any] = None

    content: Any = None
    embedding: list[float] | None = None
```

The resulting constructor fields are `id`, `created_at`, `metadata`, `content=None`, and
`embedding=None`.

**Exact semantics**:

- `content` accepts any Python value. On serialization, an Element becomes its `to_dict()` shape, a
  Pydantic model becomes `model_dump()`, a LionAGI `DataClass` uses its configured `to_dict`, and
  any other value passes through unchanged.
- On input, a dictionary whose `metadata` contains `lion_class` is reconstructed with
  `Element.from_dict`; other dictionaries remain ordinary content.
- `embedding=None` remains null. A list is converted element-by-element with `float`; invalid list
  contents raise `ValueError("Invalid embedding list.")`.
- A string must decode as a JSON list and every member must convert to float; malformed JSON or a
  non-list JSON value raises `ValueError("Invalid embedding string.")`. Other input types raise an
  invalid-embedding-type error. An empty list is valid.
- Sync and async adapters force `to_dict(mode="db")` on writes and `from_dict` on reads. JSON and
  TOML adapters are registered at module import; the optional PostgreSQL adapter is checked and
  registered lazily on first PostgreSQL adaptation, with missing extras degrading to no adapter.
  The PostgreSQL availability check is one-shot per process: the checked marker is installed even
  when dependencies are missing or registration raises, so later calls do not retry after the
  environment changes.

The embedding field remains on the broad Node base because it is part of the persisted shape. Its
presence is not a vector-capability interface, and removing it safely requires an inventory and
migration rather than a local field deletion.

## Consequences

UUID references, heterogeneous collections, graph relationships, and persisted model payloads can
share one base envelope. Most concrete model types recover their own schema and behavior through
the discriminator, and legacy built-in short names remain readable.

Contributors extending the model must understand three non-obvious costs:

- subclass construction can violate the envelope if a custom `__init__` does not forward inherited
  fields; `Edge` currently demonstrates this failure and is captured in the delta table;
- the resolver's ability to import a dotted path is also a code-loading trust boundary; and
- automatic registration is a Node behavior, not an Element behavior, with silent overwrite and
  ambiguous short-suffix recovery.

Reversing D1 would invalidate UUID-keyed stores and graph endpoints. Reversing D2 or D3 requires a
persisted-data migration and a compatibility reader. Moving `Node.embedding` behind a capability is
less structurally invasive but still requires a persisted-shape migration.

The inherited timestamp and metadata appear on descendants that may not independently need them.
This is accepted for addressable framework models, while configuration and query value objects stay
outside the inheritance tree.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Add an explicit polymorphic-model registration and trusted-deserialization policy; acceptance requires qualified third-party model names to round-trip without `Node` inheritance, built-in short names to remain readable, and dotted-path imports to follow the documented trust policy. | M | (filled at issue-open time) |
| 2 | Separate `Node`'s stable content contract from optional vector capability; acceptance requires an inventory and migration plan for persisted embeddings before any base-field deprecation, plus an opt-in capability for new vector operations. | M | (filled at issue-open time) |
| 3 | Make `Edge` reconstruction preserve the inherited Element envelope; acceptance requires `Edge.from_dict(edge.to_dict())` to retain its UUID, creation timestamp, metadata values, and properties without nesting inherited fields under `properties`, with regression coverage for direct and polymorphic reconstruction. | S | (filled at issue-open time) |

## Alternatives considered

### No shared model envelope

Each model family could define its own identifier, timestamp, metadata, and serializer. That would
let small value objects carry only what they need, but Pile, Graph, adapters, and polymorphic
collections would need parallel identity and reconstruction rules. The repeated reference boundary
is concrete and already shared, so duplicated envelopes lost to the common Element contract.

### Value equality or Python object identity

Value equality would change identity when mutable content changes and can merge distinct records
with equal fields. Python object identity cannot survive JSON or database storage. UUID equality is
stable across both mutation and process boundaries, so both alternatives lost.

### A top-level `type` field

A dedicated top-level discriminator would make the type marker visually prominent. It would also
change the stable base record shape and require database adapters to map another reserved column.
Keeping `lion_class` inside metadata lets `metadata`/`node_metadata` move as one payload and remains
compatible with already persisted data.

### Short class names only

Short names are compact and matched early built-in data, but they cannot distinguish two extension
modules defining the same class name. Fully qualified names are written now; fixed built-in search
remains only as a compatibility reader.

### A closed registry with no dotted imports

A closed registry would make the deserialization trust boundary explicit and would detect unknown
extension classes before import. It would also make current importable third-party discriminators
unreadable unless every class was registered before the payload arrived. The shipped code chose
compatibility and extension recovery; a trusted registration policy is deferred as delta 1 rather
than retroactively asserted.

### Automatic registration for every Element subclass

Moving the subclass hook to Element would make extension behavior uniform. It also changes global
registration and collision behavior for every value in the hierarchy and does not by itself solve
trusted import or legacy short-name ambiguity. The current Node-only hook is recorded as-is pending
the explicit policy decision.

### Make every Pydantic model an Element

Uniform inheritance would simplify a few generic annotations, but it would assign UUIDs,
timestamps, and mutable metadata to configuration and query values that are compared and moved by
value. The boundary remains addressable or persisted framework objects, not all structured data.

### Remove `Node.embedding` immediately

An opt-in vector capability would narrow the base node contract. Immediate removal loses the
persisted compatibility field and provides no migration for stored embeddings. The capability
split remains a delta until the stored-data inventory and migration are defined.

## Notes

The Edge reconstruction delta was verified from `lionagi/protocols/graph/edge.py`: the custom
constructor forwards only `head`, `tail`, and a newly assembled `properties` dictionary to
`Element`, so inherited envelope keys arriving through `**kwargs` become nested properties while a
new Element UUID and timestamp are generated. ADR-0004 records the separate Graph-level
reconstruction failures without changing ownership of the base Edge serialization decision.
