# Protocol and model interfaces

Shared protocol helpers provide compatible enums, sentinels, identifiers, field transformations and model predicates. Native or local operations retain distinct structural admission, representation, cache lifetime and failure behavior.

## Operation boundaries

| Operation | Behavior |
| --- | --- |
| String enums and allowed values | String-value Enum classes share compatible values/bases and allowed-value expressions; provider tuple enums and native Enum inputs retain their own interfaces. |
| Sentinel identities | Public Unset/Undefined and NoCursorClaim identities are canonical. Private engine result markers remain distinct, and dtype-specific validation stays explicit. |
| Run parameter fields | Run parameter promotion uses ChatParam.field_names in declaration order and preserves pass-through fields. |
| FieldModel transformations | FieldModel transformations use `_clone` after their own metadata edits, preserving overwrite/filter behavior and warning stacklevel4. |
| HashableModel serialization | HashableModel shares its JSON flags and default serializer, resolved lazily to preserve import ordering. |
| Per-kind default specs | Step and Structure share the per-kind cached immutable default Spec while retaining field order, flags and identity. |
| Message insertion | MessageManager uses Pile.update so record admission and progression placement remain atomic, including host-built responses. |
| Progression construction | Named progression construction/reconstruction shares referential checks; add_progression retains its different error text. |
| Memory IDs | MemoryItem admission precedes structural ID.get_id normalization in memory storage. |
| CLI UUID coercion | CLI UUID coercion uses the canonical protocol helper at existing lazy import seams. |
| Model identity | Explicit iModel identity uses to_uuid; generated identity remains a fresh UUID4. |
| Typed event lookup | CodingRun and HypothesisRun delegate typed lookup to ChainRun and inherit events_of, retaining their narrower annotations. |
| Provider window lookup | Provider-window iteration uses the shared loader/cache and retains its swallowed ImportError policy. |
| Provider retry defaults | Provider constructors defer retry defaults to Endpoint.__init__ and EndpointConfig default3; supplied configuration and overrides keep their precedence. |
| Excluded retry exceptions | Retry wrappers retain excluded exception context and original-error unwrapping; the public exclusion helper remains a separate interface. |
| Element timestamp validation | Element retains local-zone interpretation, float precision/admission and exact timestamp errors rather than general coercer semantics. |
| Timestamp coercion | The public timestamp coercer keeps bool refusal, aware identity, naive UTC/ISO normalization and numeric-before-ISO string policy. |
| Model creation time | iModel created_at accepts a float timestamp or None and keeps its exact invalid-type diagnostic. |
| Role-cache lifetime | Role dictionaries keep process-lifetime caches, cached None and retry behavior for missing/error results; bounded eviction would reread them. |
| Retired document caches | Retired reader/coding document caches have no remaining consumer or required TTL interface. |
| Undefined comparison filtering | Rich comparisons filter Undefined only; Unset and equality-based input behavior remain distinct from identity filtering. |
| Field default admission | FieldModel retains legacy default admission and its diagnostic; CommonMeta would add a refusal and change the error. |
| Flow structural IDs | Flow ID coercion uses structural ID.get_id, preserving Observable admission and stringify-only refusal. |
| Operation and Step conversion | Operate retains strict conversion errors and None results; Step retains permissive skipping of unknown entries. |
| Retry cancellation policy | Retry exclusion runs before count/delay while preserving cancellation and deadline behavior. |
| Bounded-cache API | The existing bounded-cache API serves its explicit env/cap consumers; process-lifetime or retired caches do not gain new default/TTL semantics. |

## Shared helpers

Shared `Enum`, `Unset`, `Undefined`, protocol ID helpers, `Pile.update`, field cloning and nominal type helpers are used where their existing contracts match. A structural model predicate, provider tuple enum or concrete cache/collection API retains its own behavior.

## Behavioral reference

| Operation | Example consumer | Behavior |
| --- | --- | --- |
| String enums and allowed values | `lionagi/hooks/bus.py` — `HookPoint`; `lionagi/operations/builder.py` — `ExpansionStrategy` | Shared Enum preserves its values, bases and stdlib Enum compatibility. |
| String enums and allowed values | `lionagi/hooks/loader.py` — `load_hooks_for_agent`; `lionagi/protocols/messages/base.py` — `module initialization` | Companion allowed-value expressions delegate while preserving order and container shape. |
| Sentinel identities | `lionhub/src/lionhub/orchestration/engines/engine.py` — `module initialization`; `lionhub/src/lionhub/orchestration/engines/engine.py` — `Engine._degrade_export` | Private result marker must remain distinct from public Unset/Undefined, both valid results. Preserve all omission/identity checks. |
| Sentinel identities | `lionagi/libs/validate/common_field_validators.py` — `validate_same_dtype_flat_list` | Validation uses the canonical Undefined sentinel with its existing membership behavior. |
| Sentinel identities | `lionagi/libs/validate/common_field_validators.py` — `validate_same_dtype_flat_list` | Required dtype uses isinstance(item,dtype); `is_same_dtype`(dtype=None) infers a type and changes None/invalid-dtype behavior. |
| Sentinel identities | `lionagi/state/db.py` — `NoCursorClaim` | The SingletonType has true boolean value, `NO_CURSOR_CLAIM` repr and identity-preserving copy/deepcopy. |
| Sentinel identities | `lionagi/state/db.py` — `module initialization` | Canonical typed singleton factory instance used. |
| Sentinel identities | `lionagi/studio/operator/store.py` — `OperatorStore.update_conversation`; `lionagi/studio/operator/store.py` — `OperatorStore.update_conversation_from_turn` | Shared Unset preserves omission versus explicit None in update signatures and identity checks; authored values and persisted row omission stay unchanged. |
| Sentinel identities | `lionhub/src/lionhub/providers/deepseek/chat.py` — `normalize_deepseek_usage` | Shared Undefined distinguishes absent usage from zero and preserves provider-native priority; None does not erase an existing native value. |
| Run parameter fields | `lionagi/operations/run/run.py` — `_promote_to_run_param` | Promotion reads `ChatParam.field_names` in declaration order and preserves RunParam pass-through fields. |
| FieldModel transformations | `lionagi/models/field_model.py` — `FieldModel.as_nullable`; `lionagi/models/field_model.py` — `FieldModel.as_listable` | Construction delegates after each method's metadata/type transformation, preserving overwrite/filter behavior and warning stacklevel. |
| HashableModel serialization | `lionagi/models/hashable_model.py` — `HashableModel.to_json`; `lionagi/models/hashable_model.py` — `_get_default_hashable_serializer` | `json_dumps` flags/decode and identical `DEFAULT_ELEMENT_SERIALIZER`; import remains lazy to avoid cycle. |
| Per-kind default specs | `lionagi/operations/fields.py` — `_get_default_spec` | The per-kind cache shares an immutable Spec, preserving identity and the lazy Structure import. |
| Per-kind default specs | `lionagi/operations/operate/step.py` — `Step.request_operative`; `lionagi/operations/schema/structure.py` — `Structure._build_operable` | This exact standard kind shares `_get_default_spec`; preserve field order/flags. |
| Message insertion | `lionagi/protocols/messages/manager.py` — `MessageManager.a_add_message`; `lionagi/protocols/messages/manager.py` — `MessageManager.add_message` | Pile.update retains admission/progression position atomically; host-built responses use the same atomic insertion boundary. |
| Progression construction | `lionagi/protocols/generic/flow.py` — `Flow._check_progression_items`; `lionagi/protocols/generic/flow.py` — `Flow._validate_referential_integrity` | Construction and `from_dict` use the named-progression missing-item predicate, preserving reconstruction and its error message. |
| Progression construction | `lionagi/protocols/generic/flow.py` — `Flow.add_progression` | Own missing-item message omits progression name; shared helper would change error text. |
| Memory IDs | `lionagi/integrations/ruvector/memory.py` — `RuVectorMemoryStore.store` | MemoryItem guard precedes structural `ID.get_id`; preserve existing id normalization. |
| CLI UUID coercion | `lionagi/cli/orchestrate/flow.py` — `_reconstruct_spawned_nodes`; `lionagi/cli/orchestrate/flow.py` — `_execute_dag._checkpoint_record` | UUID coercion uses canonical `protocols.to_uuid` with lazy binding. |
| Model identity | `lionagi/service/imodel.py` — `iModel.__init__` | Explicit id `to_uuid`; preserve generated UUID default and current accepted id domain. |
| Typed event lookup | `lionhub/src/lionhub/orchestration/engines/coding.py` — `CodingRun.find`; `lionhub/src/lionhub/orchestration/engines/hypothesis.py` — `HypothesisRun.find` | Typed override delegates to ChainRun.find; narrower annotation retained and duplicate `events_of` inherited. |
| Provider window lookup | `lionagi/service/token_budget.py` — `_all_provider_windows` | Provider-window iteration uses shared `_get_provider_windows`, its dictionary cache and swallowed ImportError policy. |
| Provider retry defaults | `lionhub/src/lionhub/providers/exa/contents.py` — `ExaContentsEndpoint.__init__`; `lionhub/src/lionhub/providers/exa/find_similar.py` — `ExaFindSimilarEndpoint.__init__` | The constructor delegates to `Endpoint.__init__`; its registered EndpointMeta creates EndpointConfig with default max_retries=3, while supplied configuration and overrides retain their precedence. |
| Excluded retry exceptions | `lionagi/service/resilience.py` — `retry_with_backoff` | Direct exclude delegation removes private wrapper traceback/context; preserve current exception diagnostics, attempt/deadline/cancellation behavior. |
| Element timestamp validation | `lionagi/protocols/generic/element.py` — `Element._coerce_created_at` | Retain float pass-through/nonfinite/precision, local-naive datetime, Decimal/bool fallback, ISO-first strings and exact Invalid* errors. |
| Timestamp coercion | `lionagi/ln/_utils.py` — `coerce_created_at` | The timestamp helper rejects bool, normalizes naive UTC, Z/z and fractional timestamps, and tries numeric strings before ISO strings. |
| Model creation time | `lionagi/service/imodel.py` — `iModel.__init__` | Retain float-or-None admission, exact stored float and `created_at` must be a float timestamp error; coerce widens types and changes float validation/rounding. |
| Role-cache lifetime | `lionhub/src/lionhub/orchestration/engines/engine.py` — `module initialization` | unbounded dict factory and None-versus-missing contract; no bounded eviction/get(default) API. |
| Role-cache lifetime | `lionhub/src/lionhub/orchestration/engines/engine.py` — `role_profile_route`; `lionhub/src/lionhub/orchestration/engines/engine.py` — `role_profile_injection` | (role,cwd) dict snapshots retain hits for process lifetime, cached None, and retry absent/invalid profiles. Eviction introduces disk rereads. |
| Undefined comparison filtering | `lionagi/adapters/spec_adapters/pydantic_field.py` — `PydanticSpecAdapter.fuzzy_match_fields` | v!=Undefined retains Unset and rich-comparison behavior; `not_sentinel` drops both, identity-only spelling can differ for custom equality. |
| Field default admission | `lionagi/models/field_model.py` — `FieldModel._convert_kwargs_to_params` | Cannot have both default and `default_factory` wording and admitted noncallable factory metadata; CommonMeta changes message and adds callable rejection. |
| Flow structural IDs | `lionagi/protocols/generic/flow.py` — `Flow.from_dict` | `Flow.from_dict` uses `ID.get_id`; preserve Observable acceptance/stringify-only refusal. |
| Operation and Step conversion | `lionagi/operations/operate/operate.py` — `_specs_from_fields` | Operate raises current TypeError for unknown field entries and returns None for no named specs; do not unify with Step skip. |
| Operation and Step conversion | `lionagi/operations/operate/step.py` — `Step.request_operative` | Step skips unknown entries; shared strict converter would newly raise. |
| Retry cancellation policy | `lionagi/ln/concurrency/patterns.py` — `retry` | exclude=() before count/delay; preserve current public API. Service wrapper remains separate. |
| Bounded-cache API | `lionagi/adapters/spec_adapters/pydantic_field.py` — `module initialization`; `lionagi/ln/_structural.py` — `module initialization` | Existing caller provides env/cap; preserve structural/annotation/adapter cache semantics. |
| Bounded-cache API | `lionagi/ln/_cache.py` — `BoundedLRUCache.__init__`; `lionagi/ln/_cache.py` — `BoundedLRUCache.get` | Constructor still requires env/cap; get has no default. Role dictionaries keep process-lifetime ownership; retired document caches have no active TTL interface. |
| TokenType | `lionagi/lndl/lexer.py` — `TokenType` | Native automatic integer Enum values define token identity; the string-vocabulary allowed-value method is a different interface. |
| AG2Configs; AnthropicConfigs; ClaudeCodeConfigs; DeepSeekConfigs; ExaConfigs; FirecrawlConfigs; GeminiChatConfigs; GeminiCodeConfigs; GroqConfigs; LatticeConfigs; NvidiaNimConfigs; OllamaConfigs; OpenAIConfigs; CodexConfigs; OpenRouterConfigs; PerplexityConfigs; PiConfigs; TavilyConfigs | `lionhub/src/lionhub/providers/ag2/_config.py` — `AG2Configs`; `lionhub/src/lionhub/providers/anthropic/_config.py` — `AnthropicConfigs` | Native provider-config tuple enum; preserve current provider membership/constructor and avoid new public allowed semantics. |
| `_make_enum` | `lionagi/libs/schema/load_pydantic_model_from_schema.py` — `_make_enum` | Functional stdlib enum accepts arbitrary schema values; preserve validation/naming and avoid allowed() policy. |
| `Flow.get_progression` | `lionagi/protocols/generic/flow.py` — `Flow.get_progression` | Preserve integer/slice/callable/type Pile lookup rather than narrow UUID-only resolution. |
| Operation.request | `lionagi/operations/node.py` — `Operation.request` | Operation.request returns a model/dict result or {}; `ln.to_dict` would parse strings/sequence inputs and change the existing empty-dict boundary. |
| `OperationGraphBuilder.expand_from_result` | `lionagi/operations/builder.py` — `OperationGraphBuilder.expand_from_result` | Non-model expansion items remain stringified under item/`item_index`; `ln.to_dict` would parse strings/sequence inputs and change request shape. |
| `_link_engine_child` | `lionagi/studio/operator/coordinator.py` — `_link_engine_child` | This is a repeated condition poll: false permits the next observation, success stops, and any Exception logs and returnsFalse immediately. A retry-on-exception helper would invert that policy. |
| `<module>`; select; `select_v1`; `get_choice_representation`; `parse_to_representation`; `parse_selection` | `lionagi/protocols/generic/event.py` — `module initialization`; `lionagi/operations/select/select.py` — `select` | An ln-only narrowing would refuse existing stdlib Enum inputs; no new public Enum method is required. |
| `_NonRetryableClientError`; `Endpoint._call_http`; `Endpoint._request_attempt`; `Endpoint.call.call_func` | `lionagi/service/connections/endpoint.py` — `_NonRetryableClientError`; `lionagi/service/connections/endpoint.py` — `Endpoint._call_http` | The HTTP wrapper classifies non-retryable client errors before delegating `exclude_exceptions`; that classification and exception diagnostics remain caller-owned. |
| `functools.lru_cache` | `lionhub/src/lionhub/orchestration/engines/review.py` — `module initialization` | Preserve synchronous keying, eviction and `cache_info`/`cache_clear` decorator API; an object cache wrapper is a different interface. |
| enum.Enum | `lionagi/libs/schema/load_pydantic_model_from_schema.py` — `_make_enum` | Preserve functional Enum construction over arbitrary schema values and native member naming; the shared string-vocabulary Enum is not equivalent. |
| enum.auto | `lionagi/lndl/lexer.py` — `TokenType` | Preserve native automatic integer Enum member values; adding string-value vocabulary behavior changes this token/type contract. |
| functools.partial | `lionagi/operations/run/run.py` — `_write_branch_snapshot`; `lionhub/src/lionhub/providers/_cli_subprocess.py` — `module initialization` | Preserve prebinding of this exact callable/arguments/keywords and its callable identity/introspection; it is not a concurrency dispatcher or a model converter. |
| `functools.cmp_to_key` | `lionagi/service/accounts/_ledger.py` — `CredentialLedger._reserve_sync` | Preserve the exact comparator-to-ordering adapter used by the surrounding sort; no model/protocol helper replaces that ordering object. |
| functools.wraps | `lionagi/service/resilience.py` — `circuit_breaker.decorator`; `lionagi/service/resilience.py` — `with_retry.decorator` | Preserve wrapper metadata and `__wrapped__` signature/introspection through the native decorator. |
