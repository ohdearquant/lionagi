# ln vs libs Boundary Memo

**Status:** Decision-support. No code changed.
**Scope:** `lionagi/ln/` and `lionagi/libs/` in their state as of this writing.

---

## 1. Inventory

### 1.1 `lionagi/ln/`

`ln` is the internal primitive layer. Everything it exports is either
runtime-independent infrastructure or a fundamental building block that higher
layers depend on without restriction.

| Module | Key exports | Theme |
|---|---|---|
| `_async_call.py` | `alcall`, `bcall`, `AlcallParams`, `BcallParams` | async fan-out with retry and concurrency |
| `_cache.py` | `BoundedLRUCache` | LRU cache; env-configurable max size |
| `_hash.py` | `compute_hash`, `hash_dict`, `hash_obj`, `GENESIS_HASH`, `HashAlgorithm` | deterministic object hashing |
| `_json_dump.py` | `json_dumps`, `json_dumpb`, `json_lines_iter`, `get_orjson_default` | orjson serialisation helpers |
| `_lazy_init.py` | `LazyInit`, `lazy_import` | deferred attribute initialisation |
| `_list_call.py` | `lcall` | sync list fan-out |
| `_proc.py` | `terminate_process_group`, `aterminate_process_group` | safe subprocess teardown |
| `_ssrf.py` | `is_ssrf_safe` | SSRF guard for outbound HTTP |
| `_to_list.py` | `to_list`, `ToListParams` | normalise any value to a flat list |
| `_utils.py` | `acreate_path`, `create_path`, `now_utc`, `to_uuid`, `coerce_created_at`, `synchronized`, `async_synchronized`, `copy`, `import_module`, `is_import_installed`, `load_type_from_string`, `register_type_prefix`, `extract_types`, `is_union_type`, `is_same_dtype`, `union_members`, `get_bins` | mixed bag of cross-cutting micro-utilities |
| `concurrency/` | `retry`, `race`, `gather`, `bounded_map`, `create_task_group`, `fail_after`, `move_on_after`, `Lock`, `Semaphore`, `Queue`, ... | anyio wrappers and structured concurrency |
| `fuzzy/` | `fuzzy_json`, `fuzzy_match_keys`, `fuzzy_validate_mapping`, `fuzzy_validate_pydantic`, `to_dict`, `extract_json`, `string_similarity` | LLM-output repair and fuzzy coercion |
| `types/` | `Undefined`, `Unset`, sentinels, `Params`, `DataClass`, `ModelConfig`, `Spec`, `CommonMeta`, `Operable`, `Filter` family | base types, sentinel singletons, filter DSL |

Total public symbols in `ln.__all__`: approximately 100. Import fanout: 168 direct import
sites within the package.

### 1.2 `lionagi/libs/`

`libs` holds domain-specific utilities that are logically separate from
infrastructure. They depend on `ln` (and sometimes on higher-level modules such
as `lionagi.models`), never the reverse.

| Module | Key exports | Theme |
|---|---|---|
| `nested.py` | `nget`, `nset`, `npop`, `flatten`, `unflatten`, `deep_update`, `deep_merge` | nested dict/list manipulation |
| `path_safety.py` | `resolve_workspace_path`, `check_path_safe`, `contain_and_resolve`, `safe_join`, `validate_name`, `validate_bare_name`, `validate_path_component`, `check_add_dir_safe` | path sanitisation and containment |
| `frontmatter.py` | `parse_frontmatter` | YAML frontmatter parser (yaml dep) |
| `schema/` | `function_to_schema`, `FunctionSchema`, `extract_code_block`, `extract_docstring`, `as_readable`, `minimal_yaml`, `load_pydantic_model_from_schema`, `breakdown_pydantic_annotation` | reflection over callables and Pydantic models; LLM schema generation |
| `validate/` | `validate_boolean`, `to_num`, `validate_model_to_type`, `validate_callable`, `validate_same_dtype_flat_list`, `validate_nullable_string_field`, `validate_list_dict_str_keys` | input coercion and field validators |
| `file/` | `chunk_by_chars`, `chunk_by_tokens`, `chunk_content`, `dir_to_files`, `chunk` | text chunking and directory ingestion |

Total import sites within the package: 62 (26 for `path_safety`, 16 for `schema`,
9 for `validate`, 5 for `frontmatter`, 4 for `nested`, 2 for `file`).

---

## 2. Intended Boundary

Based on the current contents and import directions, the intended separation is:

**`ln` is the zero-domain primitive layer.** It provides building blocks that
carry no business meaning: async concurrency primitives, serialisation helpers,
type sentinels, generic coercion (any-to-list, fuzzy JSON repair), and
infrastructure utilities (hashing, path creation, SSRF guard, subprocess
teardown). Nothing in `ln` imports from `libs`, `protocols`, `models`, or any
higher layer. It could in principle be extracted as a standalone package.

**`libs` is the domain-aware utility layer.** It provides utilities that need
awareness of lionagi's specific domains: tool schema generation (knows about
Pydantic and OpenAI function schema format), path safety for agent workspaces
(knows about the workspace model), field validators for operation models (knows
about `UNDEFINED` and `to_list`), and text chunking for document ingestion.
`libs` is allowed to import from `ln`, `lionagi.models`, and other package-level
modules.

The public `lionagi/utils.py` shim re-exports a curated subset of `ln` symbols
and is the stable surface used by code that does not need to know which `ln`
submodule a symbol lives in.

---

## 3. Observed Overlap and Ambiguity

### 3.1 Path utilities split across two modules

`ln._utils` exports `acreate_path` and `create_path` (path creation with
traversal guards, symlink safety, and timestamp suffixes). `libs/path_safety.py`
exports `resolve_workspace_path`, `check_path_safe`, `safe_join`,
`contain_and_resolve`, and related validators.

The two modules serve different purposes. `ln._utils.create_path` creates files.
`libs.path_safety` validates and resolves caller-supplied paths before any file
operation. The split is reasonable but the presence of `acreate_path` in `ln`
(which includes its own traversal guard) means traversal-prevention logic is
duplicated in two places. See candidate C1 below.

### 3.2 `libs/validate/validate_boolean.py` and `ln/fuzzy/_fuzzy_validate.py`

Both handle input coercion. `validate_boolean` (in `libs`) converts
strings/numbers to bool. `fuzzy_validate_pydantic` / `fuzzy_validate_mapping`
(in `ln/fuzzy`) repair malformed LLM JSON into Pydantic models. The scope is
different (bool coercion vs. full-model fuzzy repair) so this is not a true
duplicate, but both live on the coercion spectrum and the `ln/fuzzy` family
arguably owns the concept. See candidate C2.

### 3.3 `libs/nested.py` depends on `lionagi.utils.UNDEFINED`

`nested.py` imports `UNDEFINED` from `lionagi.utils`, which re-exports it from
`ln.types`. This makes `nested.py` depend on `ln` indirectly. The nested
operations (`nget`, `nset`, `npop`, `flatten`, `unflatten`, `deep_update`,
`deep_merge`) have no domain semantics. They are generic data-structure utilities
comparable to the utilities already in `ln._utils`. See candidate C3.

### 3.4 `libs/validate/common_field_validators.py` depends on `ln.copy` and `lionagi.utils`

The field validators import `copy` from `lionagi.ln` and `UNDEFINED`, `to_list`
from `lionagi.utils`. These validators are Pydantic-flavoured helpers (they
accept a `cls` first argument) and are used exclusively inside Pydantic
`@field_validator` callbacks. They are domain-specific enough to stay in `libs`.
The dependency direction is correct: `libs` depends on `ln`, not the other way.

### 3.5 `libs/path_safety.py` has no dependency on `ln`

`path_safety.py` imports only from `pathlib` and `re`. It could live in `ln`
without creating any import cycle. However, its content is workspace-safety
policy specific to the agent tool layer. See candidate C4 below.

---

## 4. Candidate Consolidations

Priority: H = high value and low risk; M = medium value or non-trivial risk; L = low value or high churn risk.

| ID | Symbol(s) | Current home | Recommended home | Rationale | Risk | Priority |
|---|---|---|---|---|---|---|
| C1 | Traversal-check logic in `acreate_path` / `create_path` | `ln/_utils.py` | Delegate to `libs/path_safety.py`; `ln` calls into `libs` OR extract shared helper into `ln` | Avoids duplicating the `..`-in-parts guard | Low: internal refactor, no API change | M |
| C2 | `validate_boolean`, `to_num` | `libs/validate/` | Move to `ln/types/` or a new `ln/_coerce.py` | They are domain-free coercion utilities with no Pydantic or schema dependency; comparable scope to `fuzzy` | Low API risk; callers would need import update | M |
| C3 | `nget`, `nset`, `npop`, `flatten`, `unflatten`, `deep_update`, `deep_merge` | `libs/nested.py` | Move to `ln/` as `ln/_nested.py`; export from `ln.__all__` | Generic data-structure utilities; the only `libs` dependency is `UNDEFINED` from `ln.types`, which would become a local import | Moderate churn (4 call sites); `libs.__init__` must re-export for compat | H |
| C4 | `parse_frontmatter` | `libs/frontmatter.py` | Stay in `libs` (yaml dep is optional) | The yaml dependency is not appropriate for the zero-dep `ln` layer | N/A | not recommended |
| C5 | `is_ssrf_safe` | `ln/_ssrf.py` | Stay in `ln` | It is a network-safety primitive with no domain semantics, correctly placed | N/A | no change |

---

## 5. What Should NOT Be Merged

**`libs/schema/` must stay in `libs`.** The schema subpackage imports
`lionagi.models.SchemaModel` and Pydantic internals. Moving it to `ln` would
create an upward import cycle: `ln` would then depend on `models`, which depends
on `ln`.

**`libs/file/` must stay in `libs`.** `chunk_content` optionally imports
`lionagi.protocols.graph.node.Node`. The file layer is domain-aware.

**`libs/path_safety.py` should stay in `libs`.** Its policy (blocking `.env`,
`id_rsa`, glob chars, symlinks) is specific to the agent workspace model, not
general infrastructure. Placing it in `ln` would leak tool-layer policy into the
primitive layer.

**`libs/validate/common_field_validators.py` should stay in `libs`.** It
imports `lionagi.ln.copy` and `lionagi.utils.to_list`, and it is used only by
Pydantic field validators in the operations layer. It is domain-aware glue, not
a primitive.

**`ln/fuzzy/` should not be folded into `libs/validate/`.** Fuzzy JSON repair
is a protocol-level operation (repairing LLM output) with no dependency on
schema or Pydantic models. It belongs in `ln` where it is accessible to all
higher layers without creating cycles.

---

## 6. Top Three Recommendations

1. **Move `libs/nested.py` into `ln/` as `ln/_nested.py`** and export its
   symbols from `ln.__all__`. Nested dict/list operations are generic
   data-structure primitives; their sole external dependency is `ln.types.Undefined`.
   The 4 call sites inside `libs/` and `models/` would then import from `ln`,
   which is the correct direction. Re-export from `libs.__init__` for one release
   cycle to keep external consumers unbroken.

2. **Move `validate_boolean` and `to_num` from `libs/validate/` into `ln/`**
   (as `ln/_coerce.py` or appended to `ln/types/`). Both are pure coercion
   functions with no Pydantic or schema dependency. Having them in `libs` forces
   code in `lndl/types.py` and `operations/fields.py` to reach into `libs` for
   what are fundamentally primitive operations. The `common_field_validators.py`
   wrappers around them can stay in `libs`.

3. **Document the single invariant that enforces the boundary**: nothing in `ln`
   may import from `libs`, `models`, `protocols`, `operations`, or any other
   peer package. A lint rule (e.g., a `ruff` import-check or a small CI script
   that greps `from lionagi.libs` inside `lionagi/ln/`) would catch regressions
   automatically, and is lower cost than any structural move.
