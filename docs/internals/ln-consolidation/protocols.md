# Protocol and model consolidation

Issue #3584 baseline: `ee5783cc689fbff53afef49f772e6584121b70e6`. Positions use this revision. Changed entries are implemented; focused validation passed. KEEP rows describe source contracts.

| Unit | Current disposition|
| --- | ---|
| A1 | 24 string-value Enum classes and three allowed-value expressions already migrated; separate native Enum exclusions remain.|
| A2 | Store/DeepSeek/common validator public markers and NoCursorClaim singleton are already canonical; engine result marker stays private and dtype-specific validation stays explicit.|
| A3 | Run parameter promotion already uses `ChatParam.field_names` and preserves pass-through.|
| A4 | Five FieldModel transformations already use `_clone` after caller-specific metadata edits; validation warning stacklevel remains4.|
| A5 | HashableModel already shares JSON policy and lazily resolves the same default serializer.|
| A6 | Step and Structure already use the per-kind cached default Spec factory with existing order and identity.|
| A7 | MessageManager already uses Pile.update; the later Context host-built response seam is distinct from original migration credit.|
| A8 | Named progression construction/reconstruction already shares checks; `add_progression` keeps its distinct error text.|
| A9 | Memory store already uses structural `ID.get_id` after MemoryItem admission.|
| A10 | Four CLI UUID coercions already use canonical protocols helper at original lazy seams.|
| A11 | Explicit iModel id already uses `to_uuid`; generated id remains uuid4().|
| A12 | CodingRun and HypothesisRun already delegate typed find to ChainRun; no duplicate `events_of` remains.|
| A13 | Provider window iteration already reuses the loader/cache and ImportError policy.|
| A14 | All13 redundant retry-default writes are removed; provider constructors delegate to `Endpoint.__init__`, registered metadata and EndpointConfig default3.|
| B1 | The old self-cause bug is repaired. Keep current private excluded wrapper context; exclude helper is already separately available.|
| B2 | Keep Element validator; coercer differs in local timezone, float precision/admission and exact error text.|
| B3 | Current public coercer policy is already landed and must remain unchanged.|
| B4 | Keep iModel float-only `created_at` admission and error text.|
| B5 | Keep process-lifetime role dictionaries, cachedNone and missing/error retries; bounded eviction would cause rereads.|
| B6 | The reader/coding document-cache implementation named by the issue was removed upstream; no TTL API is needed for retired sites.|
| B7 | Keep Undefined-only rich comparison filter; dropping Unset or switching equality to identity changes inputs.|
| B8 | Keep legacy FieldModel default admission and exact diagnostic; CommonMeta adds a refusal and different message.|
| B9 | Flow structural ID coercion already landed via#3714; preserve current admission.|
| B10 | Keep Operate strict errors/None result and Step permissive unknown-entry skipping as distinct policies.|
| H1 | Retry exclude parameter already landed via#3718, with cancellation/deadline behavior.|
| H2 | Keep existing public bounded-cache API and its three current env/cap users; new optional-env/default/TTL surface is unnecessary for kept or retired consumers.|

The baseline address index uses `U:` plus the unit name for these original unit populations.

## Explicit protocol exclusions

Native provider-config tuple enums and TokenType retain stdlib Enum. Generic event/select admission retains stdlib Enum inputs. Operation.request/expansion retain current model/dict/string behavior. Engine-child condition polling retains return-False-on-error. HTTP non-retryable wrappers retain current exclusions and original-error unwraps. `Flow.get_progression` retains existing Pile dispatch. Grouped contracts retain every baseline location below.

No new public helper names or changed helper contracts are required.

## Exact call and factory contracts

**Contracts**

- **K2.1** — Shared Enum already retains current values/bases and stdlib Enum compatibility; no further public method/class change.
- **K2.2** — Companion allowed-value expression already delegates; preserve order/container shape.
- **K2.3** — Private result marker must remain distinct from public Unset/Undefined, both valid results. Preserve all omission/identity checks.
- **K2.4** — Undefined import replaces legacy alias; retain current membership behavior.
- **K2.5** — Required dtype uses isinstance(item,dtype); `is_same_dtype`(dtype=None) infers a type and changes None/invalid-dtype behavior.
- **K2.6** — Already SingletonType, with True bool/`NO_CURSOR_CLAIM` repr and identity-preserving copy/deepcopy.
- **K2.7** — Canonical typed singleton factory instance already used.
- **K2.8** — Shared Unset preserves omission versus explicit None in update signatures and identity checks; authored values and persisted row omission stay unchanged.
- **K2.9** — Shared Undefined distinguishes absent usage from zero and preserves provider-native priority; None does not erase an existing native value.
- **K2.10** — Already `ChatParam.field_names` declaration order and RunParam pass-through; no dataclasses.fields duplication.
- **K2.11** — Already delegates construction after exact own metadata/type transformation; preserve overwrite/filter and warning stacklevel.
- **K2.12** — Already shared `json_dumps` flags/decode and identical `DEFAULT_ELEMENT_SERIALIZER`; import remains lazy to avoid cycle.
- **K2.13** — Existing per-kind cached immutable Spec is shared; preserve current identity and lazy Structure import.
- **K2.14** — This exact standard kind already shares `_get_default_spec`; preserve field order/flags.
- **K2.15** — Pile.update retains admission/progression position atomically; host-built response is additional Context work, not original migration credit.
- **K2.16** — Already one named-progression missing-item predicate for construction/`from_dict`; preserve reconstruction and message.
- **K2.17** — Own missing-item message omits progression name; shared helper would change error text.
- **K2.18** — MemoryItem guard precedes structural `ID.get_id`; preserve existing id normalization.
- **K2.19** — Already canonical `protocols.to_uuid` with lazy binding; no deprecated ln shim.
- **K2.20** — Explicit id already `to_uuid`; preserve generated UUID default and current accepted id domain.
- **K2.21** — Typed override delegates to ChainRun.find; narrower annotation retained and duplicate `events_of` inherited.
- **K2.22** — Already loops over shared `_get_provider_windows`; same dict cache and swallowed ImportError policy.
- **K2.23** — The redundant `max_retries` setdefault is absent. This constructor delegates to `Endpoint.__init__`; its registered EndpointMeta creates EndpointConfig whose existing default is3, while supplied config/overrides retain their current precedence.
- **K2.24** — Original cause-cycle already repaired. Direct exclude delegation removes private wrapper traceback/context; preserve current exception diagnostics, attempt/deadline/cancellation behavior.
- **K2.25** — Retain float pass-through/nonfinite/precision, local-naive datetime, Decimal/bool fallback, ISO-first strings and exact Invalid* errors.
- **K2.26** — Already bool refusal, naiveUTC/Z/z/fraction normalization and numeric-before-ISO strings; public helper unchanged.
- **K2.27** — Retain float-or-None admission, exact stored float and `created_at` must be a float timestamp error; coerce widens types and changes float validation/rounding.
- **K2.28** — Keep unbounded dict factory and None-versus-missing contract; no bounded eviction/get(default) API.
- **K2.29** — (role,cwd) dict snapshots retain hits for process lifetime, cached None, and retry absent/invalid profiles. Eviction introduces disk rereads.
- **K2.30** — v!=Undefined retains Unset and rich-comparison behavior; `not_sentinel` drops both, identity-only spelling can differ for custom equality.
- **K2.31** — Keep Cannot have both default and `default_factory` wording and admitted noncallable factory metadata; CommonMeta changes message and adds callable rejection.
- **K2.32** — `Flow.from_dict` already uses `ID.get_id`; preserve Observable acceptance/stringify-only refusal.
- **K2.33** — Operate raises current TypeError for unknown field entries and returns None for no named specs; do not unify with Step skip.
- **K2.34** — Step skips unknown entries; shared strict converter would newly raise.
- **K2.35** — exclude=() already implemented before count/delay; preserve current public API. Service wrapper remains separate.
- **K2.36** — Existing caller provides env/cap; preserve structural/annotation/adapter cache semantics.
- **K2.37** — Constructor still requires env/cap; get has no default. Role dicts stay and old TTL cache is retired; no new public surface needed.

**Operations**

- **2.1** — A1 / Already shared / #3616; K2.1.
- **2.2** — A1 / Already shared / #3616; K2.2.
- **2.3** — A2 / KEEP / ; K2.3.
- **2.4** — A2 / Already shared / #3621; K2.4.
- **2.5** — A2 / KEEP / ; K2.5.
- **2.6** — A2 / Already shared / #3621; K2.6.
- **2.7** — A2 / Already shared / #3621; K2.7.
- **2.8** — A2 / Already shared / #3621; K2.8.
- **2.9** — A2 / Already shared / #3621; K2.9.
- **2.10** — A3 / Already shared / #3621; K2.10.
- **2.11** — A4 / Already shared / #3621; K2.11.
- **2.12** — A5 / Already shared / #3621; K2.12.
- **2.13** — A6 / Already shared / #3621; K2.13.
- **2.14** — A6 / Already shared / #3621; K2.14.
- **2.15** — A7 / Already shared / #3621; K2.15.
- **2.16** — A7 / Already shared / #3732; K2.15.
- **2.17** — A8 / Already shared / #3621; K2.16.
- **2.18** — A8 / KEEP / ; K2.17.
- **2.19** — A9 / Already shared / #3621; K2.18.
- **2.20** — A10 / Already shared / #3621; K2.19.
- **2.21** — A11 / Already shared / #3621; K2.20.
- **2.22** — A12 / Already shared / #3621; K2.21.
- **2.23** — A13 / Already shared / #3620; K2.22.
- **2.24** — A14 / Already shared / #3620; K2.23.
- **2.25** — B1 / KEEP / ; K2.24.
- **2.26** — B2 / KEEP / ; K2.25.
- **2.27** — B3 / Already shared / #3629; K2.26.
- **2.28** — B4 / KEEP / ; K2.27.
- **2.29** — B5 / KEEP / ; K2.28.
- **2.30** — B5 / KEEP / ; K2.29.
- **2.31** — B7 / KEEP / ; K2.30.
- **2.32** — B8 / KEEP / ; K2.31.
- **2.33** — B9 / Already shared / #3714; K2.32.
- **2.34** — B10 / KEEP / ; K2.33.
- **2.35** — B10 / KEEP / ; K2.34.
- **2.36** — H1 / Already shared / #3718; K2.35.
- **2.37** — H2 / KEEP / ; K2.36.
- **2.38** — H2 / KEEP / ; K2.37.

## Native Enum classes and other explicit exclusions

**Contracts**

- **K3.1** — Explicit native/auto Enum exclusion; shared allowed method adds an unrequested public surface.
- **K3.2** — Native provider-config tuple enum; preserve current provider membership/constructor and avoid new public allowed semantics.
- **K3.3** — Functional stdlib enum accepts arbitrary schema values; preserve validation/naming and avoid allowed() policy.
- **K3.4** — Preserve integer/slice/callable/type Pile lookup rather than narrow UUID-only resolution.
- **K3.5** — Operation.request returns a model/dict result or {}; `ln.to_dict` would parse strings/sequence inputs and change the existing empty-dict boundary.
- **K3.6** — Non-model expansion items remain stringified under item/`item_index`; `ln.to_dict` would parse strings/sequence inputs and change request shape.
- **K3.7** — This is a repeated condition poll: false permits the next observation, success stops, and any Exception logs and returnsFalse immediately. A retry-on-exception helper would invert that policy.
- **K3.8** — An ln-only narrowing would refuse existing stdlib Enum inputs; no new public Enum method is required.
- **K3.9** — This endpoint already delegates `exclude_exceptions`; removing the HTTP classification wrapper is outside behavior-neutral consolidation.

**Operations**

- **3.1** — TokenType; K3.1.
- **3.2** — AG2Configs; K3.2.
- **3.3** — AnthropicConfigs; K3.2.
- **3.4** — ClaudeCodeConfigs; K3.2.
- **3.5** — DeepSeekConfigs; K3.2.
- **3.6** — ExaConfigs; K3.2.
- **3.7** — FirecrawlConfigs; K3.2.
- **3.8** — GeminiChatConfigs; K3.2.
- **3.9** — GeminiCodeConfigs; K3.2.
- **3.10** — GroqConfigs; K3.2.
- **3.11** — LatticeConfigs; K3.2.
- **3.12** — NvidiaNimConfigs; K3.2.
- **3.13** — OllamaConfigs; K3.2.
- **3.14** — OpenAIConfigs; K3.2.
- **3.15** — CodexConfigs; K3.2.
- **3.16** — OpenRouterConfigs; K3.2.
- **3.17** — PerplexityConfigs; K3.2.
- **3.18** — PiConfigs; K3.2.
- **3.19** — TavilyConfigs; K3.2.
- **3.20** — `_make_enum`; K3.3.
- **3.21** — `Flow.get_progression`; K3.4.
- **3.22** — Operation.request; K3.5.
- **3.23** — `OperationGraphBuilder.expand_from_result`; K3.6.
- **3.24** — `_link_engine_child`; K3.7.
- **3.25** — `<module>`; K3.8.
- **3.26** — select; K3.8.
- **3.27** — `select_v1`; K3.8.
- **3.28** — `get_choice_representation`; K3.8.
- **3.29** — `parse_to_representation`; K3.8.
- **3.30** — `parse_selection`; K3.8.
- **3.31** — `_NonRetryableClientError`; K3.9.
- **3.32** — `Endpoint._call_http`; K3.9.
- **3.33** — `Endpoint._request_attempt`; K3.9.
- **3.34** — `Endpoint.call.call_func`; K3.9.

## Additional static native candidates

These are separate from the original issue-arm counts. Operations and baseline positions are retained; dynamic receiver types are not inferred from spelling alone. They remain unchanged.

**Contracts**

- **K4.1** — Preserve synchronous keying, eviction and `cache_info`/`cache_clear` decorator API; an object cache wrapper is a different interface.
- **K4.2** — Preserve functional Enum construction over arbitrary schema values and native member naming; the shared string-vocabulary Enum is not equivalent.
- **K4.3** — Preserve native automatic integer Enum member values; adding string-value vocabulary behavior changes this token/type contract.
- **K4.4** — Preserve prebinding of this exact callable/arguments/keywords and its callable identity/introspection; it is not a concurrency dispatcher or a model converter.
- **K4.5** — Preserve the exact comparator-to-ordering adapter used by the surrounding sort; no model/protocol helper replaces that ordering object.
- **K4.6** — Preserve wrapper metadata and `__wrapped__` signature/introspection through the native decorator.

**Operations**

- **4.1** — `functools.lru_cache`; K4.1.
- **4.2** — enum.Enum; K4.2.
- **4.3** — enum.auto; K4.3.
- **4.4** — functools.partial; K4.4.
- **4.5** — `functools.cmp_to_key`; K4.5.
- **4.6** — functools.wraps; K4.6.

## Baseline address index

Entries use `contract=lines`, or `line:column` for JSON calls. `U:` identifies unit rows; `×N` preserves occurrences. Ranges cover original spans only. All positions use the baseline.

- `lionagi/adapters/spec_adapters/pydantic_field.py`: `2.31=140; 2.37=31; U:B7=140; U:H2=31`.
- `lionagi/cli/orchestrate/flow.py`: `2.20=1379,1403,1891,1912; U:A10=1379,1403,1891,1912`.
- `lionagi/engines/coding.py`: `2.22=358; U:A12=358`.
- `lionagi/engines/engine.py`: `2.29=125,160; 2.3=46,1709,1761,1762,1838,1840; 2.30=134,163; U:A2=46,1709,1761,1762,1838,1840; U:B5=125,134,160,163`.
- `lionagi/engines/hypothesis.py`: `2.22=633; U:A12=633`.
- `lionagi/engines/review.py`: `4.1=73`.
- `lionagi/hooks/bus.py`: `2.1=42; U:A1=42`.
- `lionagi/hooks/loader.py`: `2.2=70; U:A1=70`.
- `lionagi/integrations/ruvector/memory.py`: `2.19=64; U:A9=64`.
- `lionagi/libs/schema/load_pydantic_model_from_schema.py`: `3.20=77; 4.2=88`.
- `lionagi/libs/validate/common_field_validators.py`: `2.4=27; 2.5=37; U:A2=27,37`.
- `lionagi/ln/_cache.py`: `2.38=33,93; U:H2=33,93`.
- `lionagi/ln/_structural.py`: `2.37=291; U:H2=291`.
- `lionagi/ln/_utils.py`: `2.27=443; U:B3=443`.
- `lionagi/ln/concurrency/patterns.py`: `2.36=222; U:H1=222`.
- `lionagi/ln/types/_annotation.py`: `2.37=19; U:H2=19`.
- `lionagi/lndl/lexer.py`: `3.1=10; 4.3=11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29`.
- `lionagi/models/field_model.py`: `2.11=170,175,182,205,211; 2.32=87; U:A4=170,175,182,205,211; U:B8=87`.
- `lionagi/models/hashable_model.py`: `2.12=77,98; U:A5=77,98`.
- `lionagi/operations/builder.py`: `2.1=21; 3.23=105; U:A1=21`.
- `lionagi/operations/fields.py`: `2.13=310; U:A6=310`.
- `lionagi/operations/node.py`: `3.22=62`.
- `lionagi/operations/operate/operate.py`: `2.34=35; U:B10=35`.
- `lionagi/operations/operate/step.py`: `2.14=93,97,98,99; 2.35=76; U:A6=93,97,98,99; U:B10=76`.
- `lionagi/operations/run/run.py`: `2.10=856; 4.4=67; U:A3=856`.
- `lionagi/operations/schema/structure.py`: `2.14=161,163,164,165; U:A6=161,163,164,165`.
- `lionagi/operations/select/select.py`: `3.26=24; 3.27=64`.
- `lionagi/operations/select/utils.py`: `3.28=65; 3.29=25,39; 3.30=77,96`.
- `lionagi/plugins/registry.py`: `2.1=38; U:A1=38`.
- `lionagi/plugins/trust.py`: `2.1=34; U:A1=34`.
- `lionagi/protocols/generic/element.py`: `2.26=75; U:B2=75`.
- `lionagi/protocols/generic/event.py`: `3.25=27`.
- `lionagi/protocols/generic/flow.py`: `2.17=44,52,106; 2.18=142; 2.33=120; 3.21=177; U:A8=44,52,106,142; U:B9=120`.
- `lionagi/protocols/messages/base.py`: `2.1=21,35; 2.2=47,64; U:A1=21,35,47,64`.
- `lionagi/protocols/messages/manager.py`: `2.15=117,423; 2.16=127; U:A7=117,127,423`.
- `lionagi/providers/_cli_subprocess.py`: `4.4=1374`.
- `lionagi/providers/ag2/_config.py`: `3.2=10`.
- `lionagi/providers/anthropic/_config.py`: `3.3=10; 3.4=26`.
- `lionagi/providers/deepseek/_config.py`: `3.5=10`.
- `lionagi/providers/deepseek/chat.py`: `2.9=74,75; U:A2=74,75`.
- `lionagi/providers/exa/_config.py`: `3.6=10`.
- `lionagi/providers/exa/_schemas.py`: `2.1=11; U:A1=11`.
- `lionagi/providers/exa/contents.py`: `2.24=46; U:A14=46`.
- `lionagi/providers/exa/find_similar.py`: `2.24=86; U:A14=86`.
- `lionagi/providers/exa/search.py`: `2.1=16,28; 2.24=76; U:A1=16,28; U:A14=76`.
- `lionagi/providers/firecrawl/_config.py`: `3.7=10`.
- `lionagi/providers/firecrawl/_schemas.py`: `2.1=9; U:A1=9`.
- `lionagi/providers/firecrawl/crawl.py`: `2.24=119; U:A14=119`.
- `lionagi/providers/firecrawl/map.py`: `2.24=49; U:A14=49`.
- `lionagi/providers/firecrawl/scrape.py`: `2.24=60; U:A14=60`.
- `lionagi/providers/google/_config.py`: `3.8=10; 3.9=26`.
- `lionagi/providers/groq/_config.py`: `3.10=7`.
- `lionagi/providers/groq/audio_transcription.py`: `2.24=21; U:A14=21`.
- `lionagi/providers/lattice/_config.py`: `3.11=17`.
- `lionagi/providers/nvidia_nim/_config.py`: `3.12=10`.
- `lionagi/providers/ollama/_config.py`: `3.13=10`.
- `lionagi/providers/openai/_chat_schemas.py`: `2.1=124; U:A1=124`.
- `lionagi/providers/openai/_config.py`: `3.14=10; 3.15=82`.
- `lionagi/providers/openai/audio.py`: `2.24=32,63; U:A14=32,63`.
- `lionagi/providers/openai/images.py`: `2.24=109,122; U:A14=109,122`.
- `lionagi/providers/openrouter/_config.py`: `3.16=10`.
- `lionagi/providers/perplexity/_config.py`: `3.17=10`.
- `lionagi/providers/perplexity/chat.py`: `2.1=16; U:A1=16`.
- `lionagi/providers/pi/_config.py`: `3.18=10`.
- `lionagi/providers/tavily/_config.py`: `3.19=10`.
- `lionagi/providers/tavily/search.py`: `2.1=15,20; 2.24=93,101; U:A1=15,20; U:A14=93,101`.
- `lionagi/service/accounts/_ledger.py`: `4.5=545`.
- `lionagi/service/connections/endpoint.py`: `3.31=237; 3.32=926,942; 3.33=794,821,853; 3.34=685,698`.
- `lionagi/service/hooks/_utils.py`: `4.4=30`.
- `lionagi/service/imodel.py`: `2.21=187; 2.28=190; U:A11=187; U:B4=190`.
- `lionagi/service/resilience.py`: `2.1=71; 2.25=282; 4.6=361,385; U:A1=71; U:B1=282`.
- `lionagi/service/token_budget.py`: `2.23=58; U:A13=58`.
- `lionagi/session/control.py`: `2.1=13; U:A1=13`.
- `lionagi/state/compat.py`: `4.6=2400`.
- `lionagi/state/db.py`: `2.6=166; 2.7=191; 4.6=758; U:A2=166,191`.
- `lionagi/state/health.py`: `2.1=17; U:A1=17`.
- `lionagi/studio/operator/coordinator.py`: `3.24=57`.
- `lionagi/studio/operator/store.py`: `2.8=601,602,603,604,624,625,629,631,645,646,647,648,652,654,698,701,704,707; U:A2=601,602,603,604,624,625,629,631,645,646,647,648,652,654,698,701,704,707`.
- `lionagi/studio/services/admin.py`: `4.4=873,1028,1211,1242`.
- `lionagi/studio/services/agents.py`: `4.4=396,407,420,433`.
- `lionagi/studio/services/definitions.py`: `4.4=178,244,291,439,593`.
- `lionagi/studio/services/leo.py`: `4.4=207`.
- `lionagi/studio/services/mcp_servers.py`: `4.4=605,622,636,646,654,662`.
- `lionagi/studio/services/playbooks.py`: `4.4=343,354,367,414,428`.
- `lionagi/studio/services/plugins.py`: `4.4=352,358,366,377`.
- `lionagi/studio/services/skills.py`: `4.4=150,158`.
- `lionagi/tools/base.py`: `2.1=22; U:A1=22`.
- `lionagi/tools/code/search.py`: `2.1=27; U:A1=27`.
- `lionagi/tools/coding.py`: `2.1=47; U:A1=47`.
- `lionagi/tools/communication/messenger.py`: `2.1=28; U:A1=28`.
- `lionagi/tools/context/context.py`: `2.1=78; U:A1=78`.
- `lionagi/tools/file/editor.py`: `2.1=39; U:A1=39`.
- `lionagi/tools/file/reader.py`: `2.1=21; U:A1=21`.
