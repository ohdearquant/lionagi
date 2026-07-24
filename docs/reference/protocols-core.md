# Core Protocols Reference

Reference material for `lionagi.protocols` primitives.

## Flow — thread-safety contract

`Flow` pairs an item store (`Pile[E]`) with named progressions (`Pile[P]`).
Every UUID that appears in any progression must exist in `items` (referential
integrity enforced at construction and on every mutating method).

**Lock ordering invariant**: `Flow._lock` must be acquired before any child
`Pile._lock`. `_lock` is a reentrant `threading.RLock`, so internal methods
that call other locked Flow methods (e.g. `add_item` → `get_progression`) are
safe within the same thread.

Do not mutate `flow.items` or `flow.progressions` directly in a concurrent
context — use the Flow methods, which hold the lock.

## NodeConfig — attribute reference

`NodeConfig` is a frozen dataclass set as `node_config: ClassVar` on Node
subclasses. When `node_config is None` (the default on the base `Node`), all
lifecycle methods (`touch`, `soft_delete`, `restore`, `rehash`) are no-ops.

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `table_name` | `str \| None` | `None` | DB table; `None` = no persistence |
| `schema` | `str` | `"public"` | DB schema |
| `soft_delete` | `bool` | `False` | Enables `soft_delete()` / `restore()`, generates `is_deleted` + `deleted_at` fields |
| `versioning` | `bool` | `False` | Increments `version` on each `touch()` |
| `content_hashing` | `bool` | `False` | Stores SHA-256 of content in `content_hash` on `touch()` |
| `track_updated_at` | `bool` | `False` | Stores ISO timestamp in `updated_at` on `touch()` |
| `embedding_enabled` | `bool` | `False` | Marks the node as embedding-aware |
| `embedding_dim` | `int \| None` | `None` | Vector dimension |
| `embedding_model` | `str \| None` | `None` | Model name |
| `content_type` | `type \| None` | `None` | Expected Python type for `content` |
| `flatten_content` | `bool` | `False` | Store content fields as top-level fields |
| `track_created_by` | `bool` | `False` | Generates `created_by` field, updated via `touch(by=...)` |
| `immutable_content` | `bool` | `False` | Config flag (not yet enforced at runtime) |

Properties: `is_persisted` → `table_name is not None`; `has_audit_fields` →
any of `content_hashing`, `soft_delete`, `versioning`, `track_updated_at`,
`track_created_by`; `has_embedding` → `embedding_enabled`.

## create_node — generated fields

`create_node(name, *, ...)` returns a `Node` subclass with `node_config` set
and real Pydantic fields generated for enabled features:

- `versioning=True` → `version: int = 0`
- `track_updated_at=True` → `updated_at: str | None = None`
- `track_created_by=True` → `created_by: str | None = None`
- `content_hashing=True` → `content_hash: str | None = None`
- `soft_delete=True` → `is_deleted: bool = False`, `deleted_at: str | None = None`
- `extra_fields={"x": (int, 0)}` → `x: int = Field(default=0)`

## Pile — storage vs ordering contract

`Pile` stores items in `collections: dict[UUID, T]` (O(1) keyed access) and
tracks insertion order via an embedded `Progression` (`progression`). Storage
and ordering are independent — multiple `Progression` objects can reference the
same item set. Index with `pile[uuid]`, not `pile[0]` (integer indexing goes
through `Progression`, which is O(n)).

## Observable — Pile admission contract

`Observable` (`lionagi.protocols._concepts.Observable`)
is a runtime-checkable **structural protocol**: `isinstance(item, Observable)`
is true for any object exposing an `id`, whether or not it inherits anything.
`Pile` admits on that contract — a duck-typed object with a UUID `id` is a
first-class item that can be included, found, retrieved, and removed by
identity. `Element` satisfies the protocol through its `id` field without
inheriting it (a runtime-checkable Protocol cannot be a pydantic base), so
every `Element` subclass conforms automatically.

Id resolution (`ID.get_id`, `validate_order`) is structural to match, so
nothing Pile admits is later unreachable by identity. `item_type` normalizes
the classes you pass but deliberately does not judge conformance: a class that
only assigns `self.id` in `__init__` declares nothing at class level while its
instances conform perfectly, so any class-level test would reject exactly the
types the pile itself accepts. Conformance is checked per item at admission,
where there is a real object to inspect.

Serializing a Pile is the one Element-shaped boundary that remains: dumping
calls `to_dict()` on each item, which a bare duck-typed object does not
provide.

This is intentional. A 2026-07 change briefly made admission nominal
(inheritance-only) and removed the structural contract; that was a regression
and has been reverted. Structural admission is the designed behavior, guarded
by `tests/protocols/test_observable_protocol.py` — it should not be "fixed"
back toward inheritance-only admission.
