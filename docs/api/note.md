# `Note`

Lightweight nested dict container with path-indexed access.

## Constructor

```python
class Note(BaseModel)
```

All kwargs become top-level content keys. One special case: `Note(content={...})` unwraps the dict
directly rather than nesting it under a `"content"` key.

```python
n = Note(a=1, b={"x": 10})          # {"a": 1, "b": {"x": 10}}
n = Note(content={"a": 1, "b": 2})  # {"a": 1, "b": 2}  (unwrapped)
n = Note(content="hello", a=1)      # {"content": "hello", "a": 1}  (no unwrap: 2 kwargs)
```

## Core methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `get` | `get(indices, default=UNDEFINED)` | Nested get; raises `KeyError` if path missing and no default |
| `set` | `set(indices, value)` | Nested set; auto-creates intermediate dicts/lists |
| `pop` | `pop(indices, default=UNDEFINED)` | Remove and return value at path |
| `update` | `update(indices, value)` | Smart merge at path (see below) |

```python
n = Note(a={"b": {"c": 0}}, items=[10, 20])

n.get(("a", "b", "c"))          # 0
n.get(("a", "x"), default=None) # None
n.set(("a", "b", "d"), 99)      # n["a"]["b"] == {"c": 0, "d": 99}
n.pop(("a", "b", "c"))          # returns 0, removes key
```

### `update` semantics

| Existing value | Incoming value | Behavior |
|----------------|---------------|----------|
| `None` (missing) | list or dict | `set` directly |
| `None` (missing) | scalar | wrap in list, then `set` |
| `list` | list | `extend` |
| `list` | scalar | `append` |
| `dict` | dict or Note | `deep_update` (recursive merge) |
| `dict` | scalar | raises `ValueError` |
| scalar | any | raises `TypeError` — use `set()` to overwrite |

```python
n = Note(tags=["a"], meta={"v": 1})
n.update("tags", ["b", "c"])    # tags == ["a", "b", "c"]
n.update("tags", "d")           # tags == ["a", "b", "c", "d"]
n.update("meta", {"w": 2})      # meta == {"v": 1, "w": 2}
```

## Path syntax

`indices` accepts: `str`, `int`, a `tuple[str | int, ...]`, or a `list[str | int]`.
A single string or int is treated as a one-element path. Digit-strings (e.g. `"0"`) are
coerced to `int` when the container is a list.

```python
n["key"]                  # top-level str key
n[("a", "b", 0)]          # tuple path: n.content["a"]["b"][0]
n.get(["a", "b"])         # list path
n.get("0")                # int coerced when container is a list
```

## Flatten / unflatten

```python
n = Note(a={"b": 1, "c": [2, 3]})

flat = n.flatten(sep="|")
# {"a|b": 1, "a|c|0": 2, "a|c|1": 3}

restored = Note.unflatten(flat, sep="|")
# Note(a={"b": 1, "c": [2, 3]})
```

`flatten` accepts `max_depth: int | None` to limit recursion depth.
Empty containers (`{}`, `[]`) are dropped during flatten — round-trip is lossy for those values.

## Serialization

```python
data = n.to_dict()                              # deep copy of content, Python mode
data = n.to_dict(mode="json")                   # JSON-safe (enums serialized, models dumped)
data = n.to_dict(exclude_none=True)             # strip None values at all levels
data = n.to_dict(exclude_empty=True)            # strip None + empty containers

n2 = Note.from_dict({"a": 1, "b": 2})          # equivalent to Note(a=1, b=2)
n2 = Note.model_validate({"content": {...}})    # Pydantic validation path
raw = n.model_dump()                            # Pydantic model_dump (wraps in {"content": ...})
```

## Dict-like interface

```python
n = Note(a=1, b={"x": 10})

list(n.keys())               # ["a", "b"]
list(n.keys(flat=True))      # ["a", "b|x"]

list(n.values(flat=True))    # [1, 10]
list(n.items(flat=True))     # [("a", 1), ("b|x", 10)]

len(n)                       # 2  (top-level keys)
"a" in n                     # True  (top-level only)

for key in n:                # iterates top-level keys
    print(key, n[key])

n.clear()                    # empties content
```

`keys()`, `values()`, `items()` all accept `flat=True` and a `sep` kwarg (default `"|"`).

## Usage in flows

`Session.flow()` uses `Note` internally to accumulate operation context across DAG nodes.
Pass a `Note` as the `value` argument to `update()` — it is automatically unwrapped to its
`content` dict before merging.
