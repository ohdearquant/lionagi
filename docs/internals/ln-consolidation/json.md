# JSON serialization contracts

Issue #3585 baseline: `ee5783cc689fbff53afef49f772e6584121b70e6`. Positions use this revision. Changed entries are implemented; focused validation passed. KEEP rows describe source contracts.

## Complete issue units

| Unit | Contract|
| --- | ---|
| A1 | Eight compatible decoders already use the shared loader; retain the native JSONDecodeError alias and caller behavior.|
| B1 | Keep each decoder input/admission/error contract, including bytes, numeric values and custom hooks.|
| B2 | Keep schema/spec fallback and structured extraction error behavior at each consumer.|
| B3a | Keep file writer bytes, encoding, indentation, sorting, custom defaults and write/error order.|
| B3b | Keep the distinct inline/output writer contract and its surrounding serializer/file ownership.|
| B4 | Keep existing SQL/document/transport serialization policy; two identifier-list query bindings are already shared separately.|
| B5 | Keep CLI/log output formatting and failure behavior rather than globally changing text/bytes.|
| B6 | Keep explicit custom-default object admission and serializer fallback policy.|
| B7 | Keep canonical digest input bytes and ordering; changing serialization changes durable identifiers.|

## Already-shared decoders and query bindings

**Contracts**

- **K2.1** — #3661
- **K2.2** — #3700

**Operations**

- **2.1** — `json_loads`(value); K2.1.
- **2.2** — `ln.json_loads`(`self.to_json`(decode=False)); K2.1.
- **2.3** — `ln.json_loads`(data); K2.1.
- **2.4** — `json_loads`(`self.to_json`(decode=False, **kw)); K2.1.
- **2.5** — `json_loads`(`json_str`); K2.1.
- **2.6** — `json_dumps`(`invocation_ids`); K2.2.
- **2.7** — `json_dumps`(`schedule_ids`); K2.2.

## Exact native-call contracts

These 322 locations are distinct source calls, not a count of tests or new migrations. Multiple calls on one physical line retain their columns.

All native encoders retain ASCII/spacing/format, non-finite and unsupported-type policy; decoders retain stdlib numeric/string/byte admission and existing type/error boundaries. Shared compact UTF-8/type-hook or broader loading policy is not substituted. Options and additional consumer-specific boundaries remain separate below. Columns disambiguate calls on the same source line.

**Contracts**

- **K3.1** — JSON parsing is one branch of the YAML/spec fallback, and the outer ValueError embeds the native failure and chains it.
- **K3.2** — The adapter forwards arbitrary caller `json_kwargs`; ln is not a compatible implementation of the stdlib keyword surface.
- **K3.3** — Native keyword policy is {'**': 'kw'}; `ln.json_loads` has no corresponding keyword surface. The adapter forwards arbitrary parse kwargs and reads e.pos/lineno/colno into its adapter error; hooks and native position metadata belong to this interface.
- **K3.4** — Preserve the ordered JSON-without-default → `minimal_yaml` → default=str fallback; broadening the first encoder changes which representation is selected.
- **K3.5** — Ambient codex server discovery wraps the native parse/subprocess error in an agent-setting refusal.
- **K3.6** — The explicit UTF-8 config read and its native parse/codec exception enter the existing forwarding warning.
- **K3.7** — Serialization/refusal occurs before the shell hook child exists; preserve the PermissionError text and original cause.
- **K3.8** — Serialization failure is warned and swallowed before spawning a post-hook child.
- **K3.9** — Retain the native operation, input domain and surrounding fallback or propagation behavior at each indexed consumer; no strict-orjson equivalence is asserted.
- **K3.10** — Native keyword policy is {'`parse_constant`': '`_reject_json_constant`'}; `ln.json_loads` has no corresponding keyword surface. This call supplies a native `parse_constant` or `object_pairs_hook`. The positional-only shared strict loader accepts neither hook; preserving duplicate-key/constant policy requires retaining this call.
- **K3.11** — A durable writer retains its existing `raise_if_non_finite` guard and the exact stdlib serialization path; guard removal or replacing the guarded default=str traversal is outside scope.
- **K3.12** — The sorted default=str output is hashed into the engine outcome fingerprint.
- **K3.13** — The exact encoded envelope is checked against `ENGINE_OUTCOME_BYTE_CAP`; changing spacing, strings or default conversion changes the truncation/refusal boundary. In particular, default=str retains full original stringification; `safe_fallback` is not equivalent.
- **K3.14** — This is a stdlib dump/load roundtrip. Preserve non-string key conversion, arbitrary-size integers and the existing native string/default policies; `as_loaded` is not equivalent.
- **K3.15** — This is a stdlib dump/load roundtrip. Preserve non-string key conversion, arbitrary-size integers and the existing native string/default policies; `as_loaded` is not equivalent. In particular, default=str retains full original stringification; `safe_fallback` is not equivalent.
- **K3.16** — The enclosing body-is-not-None branch intentionally leaves GET/HEAD/OPTIONS without a body; preserve request bytes and Content-Type admission.
- **K3.17** — The claim body is observability, followed by exactly one separate b"\n" write; it is not a new newline-delimited bytes-helper interface.
- **K3.18** — Native keyword policy is {'`object_pairs_hook`': '`_refuse_duplicate_json_keys`'}; `ln.json_loads` has no corresponding keyword surface. This call supplies a native `parse_constant` or `object_pairs_hook`. The positional-only shared strict loader accepts neither hook; preserving duplicate-key/constant policy requires retaining this call.
- **K3.19** — This writer owns an open text file, native incremental json.dump writes and failure cleanup. ln has no stream dump; prebuilding an encoded string changes write/failure timing and partial-write behavior.
- **K3.20** — The caller owns the already-open text stream, encoding and native json.load failure boundary; a path/default helper changes ownership or exception timing.
- **K3.21** — Native keyword policy is {'`object_pairs_hook`': '`_no_duplicate_keys`', '`parse_constant`': '`_reject_constant`'}; `ln.json_loads` has no corresponding keyword surface. This call supplies a native `parse_constant` or `object_pairs_hook`. The positional-only shared strict loader accepts neither hook; preserving duplicate-key/constant policy requires retaining this call.
- **K3.22** — Measurements are converted with default=str and sorted keys for downstream hypothesis seeds, not the ln `safe_fallback` object policy.
- **K3.23** — This native dumps call is an admission probe: unsupported values become str(value). A helper that admits extra types changes the hook envelope.
- **K3.24** — The exact serialized command participates in the content trust hash.
- **K3.25** — The stdlib roundtrip detaches the store-validated metadata before user embedding/filter work. No proof bounds all integer/string values to orjson-equivalent decoding.
- **K3.26** — Native `allow_nan`=False refusal and the encoded UTF-8 size determine metadata/tool-output budget admission.
- **K3.27** — A native encoding failure deliberately falls back to str(item); widening supported types changes the rendered schema text.
- **K3.28** — This is the native fallback after `ln.json_dumps` already raised. Routing it back to the same helper is not equivalent recovery.
- **K3.29** — The serialized scalar is inserted into TOML actor configuration; preserve the exact string escaping and rejection behavior at this cross-language boundary.
- **K3.30** — Sorted compact schema bytes determine `schema_fingerprint`.
- **K3.31** — The exact JSON scalar text is shown in a refusal describing a schema const.
- **K3.32** — Serialized JSON is passed as an external CLI argv value; caller formatting and unsupported-type behavior are part of that boundary.
- **K3.33** — Serialized sorted strings are set members used to detect uniqueItems; changing canonical spelling can change equality/refusal.
- **K3.34** — default=repr output forms a reactive dedupe key, with native failure falling back to repr(context).
- **K3.35** — the existing no-hook orjson encoder's refusal selects a str fallback; the shared serializer has a different type-admission hook. Bare orjson.dumps has no default hook and unsupported values fall back to str(`response_format`). ln default type support would change that selected text.
- **K3.36** — The exact sorted compact JSON bytes determine a persisted plugin trust manifest digest.
- **K3.37** — This is text JSONL joined with an empty str and one explicit newline per record; `json_lines_iter` yields bytes and is not interchangeable.
- **K3.38** — an incremental decoder instance owns partial-document state and consumed offsets, not a whole-document parse. The constructed JSONDecoder is retained across fragmented stdout; `raw_decode` is called at lines1136/1147 to obtain consumed offsets and preserve a partial buffer/tail repair. A whole-document loads helper is not an equivalent replacement.
- **K3.39** — The bounded binary record read treats absent data as b"null" and catches depth/parse/I/O failures; retain native byte decoding and this exact fallback.
- **K3.40** — Native sorted JSON bytes go through pwrite and length/truncation checks on an existing locked record; changing bytes alters the record protocol.
- **K3.41** — Anthropic SSE queue/rewrite handling preserves stdlib event acceptance and event serialization; rejected/parsed event state affects routed usage processing.
- **K3.42** — Native scalar JSON spelling is used for bool/int/float/None mapping keys; unsupported key types raise the caller TypeError.
- **K3.43** — JSON escaping length, with `ensure_ascii`=False, is counted as Unicode characters. UTF-8 byte length and a different encoder are not this budget.
- **K3.44** — Codex argument/tool-output bounding uses native JSON character lengths, default=str, and recursion fallback. Preserve the exact cap and shrink boundaries.
- **K3.45** — Canonical usage JSON is stored alongside account request accounting; retain its current accepted payload and persisted representation.
- **K3.46** — The function explicitly freezes the exact bytes quoted and later sent; native `allow_nan`=False and its refusal must remain identical.
- **K3.47** — Actual input-cap estimation and API telemetry count this exact `ensure_ascii`=False/default=str serialization. Compact helper output would change measured characters/tokens and retained receipts.
- **K3.48** — The exact sorted default=str material is hashed into MCP policy/security cache identity; preserve the current serialization and cache identity.
- **K3.49** — the native exception constructor preserves its document/position attributes and original cause. This is an error constructor, not decoding. It rebuilds native JSONDecodeError from e.msg/e.doc/e.pos and preserves from e; no helper error substitution.
- **K3.50** — This function explicitly provides deterministic native JSON for content hashing and persisted mirror identity.
- **K3.51** — Keep the explicit UTF-8 decode before parsing; changing decode/parse order changes UnicodeDecodeError versus native JSON errors and archive/mirror classifications.
- **K3.52** — Producer values are intentionally rendered with full default=str text; `safe_fallback` would change datetime/set/Enum/exception/duck-object and clipping behavior.
- **K3.53** — The StateDB metadata merge persists its existing native JSON representation, preserving fill-only versus merge behavior.
- **K3.54** — Parse failure returns the original raw text; a generic column parser/default helper must not replace this passthrough boundary.
- **K3.55** — This late schedule projection distinguishes a decoder depth-guard RecursionError by its native message and re-raises other recursion errors. A different parser removes that distinction.
- **K3.56** — The engine JSON serializer only extends native admission to UUID and uses `allow_nan`=False; ln would admit other objects and alter native refusal behavior.
- **K3.57** — Payload serialization occurs before launch argv/environment and child creation; preserve the terminal-callback process envelope.
- **K3.58** — Retain the native operation, input domain and surrounding fallback or propagation behavior at each indexed consumer; no strict-orjson equivalence is asserted. In particular, default=str retains full original stringification; `safe_fallback` is not equivalent.
- **K3.59** — Exact encoded JSON size decides truncation, midpoint search or message inclusion; retain current separators, ASCII escaping and default=str handling.
- **K3.60** — The operator canonical JSON representation participates in context/command hashes and byte accounting; retain the existing sorted compact UTF-8 contract.
- **K3.61** — Decision JSON is reflected into a provider denial response, including its safe error branch; preserve the exact decision representation.
- **K3.62** — Despite these function names, the current code counts len(json.dumps(...)) without encoding. Preserve this actual character-based boundary rather than silently changing it to UTF-8 bytes. In particular, default=str retains full original stringification; `safe_fallback` is not equivalent.
- **K3.63** — The `result_json` field uses a falsey-value-to-None branch before loading, distinct from unconditional hydration.
- **K3.64** — The hydrated command immediately selects a toolUseId match; replacing unguarded native loading with NULL passthrough changes failure behavior.
- **K3.65** — The caller distinguishes None/str/other iterables and does not add a parsed-list type check; a new generic hydration contract would alter admission.
- **K3.66** — The existing parse/type failure is translated into the exact `rate_limit` ValueError with its cause; retain that translation.
- **K3.67** — An empty string `action_args` becomes {}, whereas a nonempty string is loaded; preserve this existing empty-value distinction.
- **K3.68** — Native keyword policy is {'`object_pairs_hook`': '`_refuse_repeated_keys` if `unique_keys` else None'}; `ln.json_loads` has no corresponding keyword surface. This call supplies a native `parse_constant` or `object_pairs_hook`. The positional-only shared strict loader accepts neither hook; preserving duplicate-key/constant policy requires retaining this call.
- **K3.69** — This reader catches OSError and JSONDecodeError only; UnicodeDecodeError and other ValueError variants are not silently mapped to None.
- **K3.70** — Sorted compact native JSON is hashed for approval/content identity.
- **K3.71** — Base64-decoded bytes are parsed into versioned cursor/filter checks. Preserve native byte admission and the existing cursor error/cause boundary.
- **K3.72** — The caller writes private/config text with a separately appended single newline; do not change newline count or file representation.
- **K3.73** — This serialization is an SSE frame payload with framing/newlines owned by the caller, including literal completion frames.
- **K3.74** — Archive manifest/member bytes, per-row custom default and newline framing feed verification/checksums and streamed ZIP writes.
- **K3.75** — The parsed snapshot is passed to `Branch.from_dict` and then checked for exact branch identity; preserve file/parse/refusal semantics.
- **K3.76** — The resume worker unlinks its input config in finally, even when native decoding fails. A generic swallowing read helper changes control flow.
- **K3.77** — Sorted default=str JSON is hashed into schedule declaration identity.
- **K3.78** — This `allow_nan`=False call is a serialization probe that isolates an invalid recent summary before whole-page transport.
- **K3.79** — The exact native bytes are base64-encoded into externally retained versioned cursor tokens; preserve Unicode/spacing and existing token spelling.
- **K3.80** — SQLite `json_each` consumes `msg_ids` from the unvalidated stored progression load at `get_session`:1304→1357/1358. Annotation list[str] alone does not prove UUID/Unicode/integer admission; the existing two schedule-array migrations do not prove these additional arrays equivalent.
- **K3.81** — Stored progression JSON is loaded with native-error-to-empty-list fallback; retain its existing loader and NULL/type behavior.
- **K3.82** — Scripted endpoint records construct actual model content/tool-argument/stream JSON; changing native fixtures would also change the behavior they model.
- **K3.83** — ast-grep output first attempts a whole native JSON value, then native per-line NDJSON with a returned error containing the final native diagnostic.
- **K3.84** — The ruff subprocess parser returns the native parse text in CodeCheckResponse on malformed tool output.
- **K3.85** — The recovery encoder admits exactly stdlib JSON with sorted compact Unicode text and no fallback; broader type support changes bodies/global offsets and the JSON-versus-YAML whole-value path.
- **K3.86** — Native JSON quoting is embedded in the khive ops expression language for a specific scalar/list argument; preserve escaping, accepted input types and exact request text.

**Operations**

- **3.1** — json.loads; options {}; K3.1.
- **3.2** — json.dumps; options {"**":"`json_kwargs`"}; K3.2.
- **3.3** — json.loads; options {"**":"kw"}; K3.3.
- **3.4** — json.dumps; options {"`ensure_ascii`":"False"}; K3.4.
- **3.5** — json.dumps; options {"default":"str","`ensure_ascii`":"False"}; K3.4.
- **3.6** — json.loads; options {}; K3.5.
- **3.7** — json.loads; options {}; K3.6.
- **3.8** — json.dumps; options {}; K3.7.
- **3.9** — json.dumps; options {}; K3.8.
- **3.10** — json.loads; options {}; K3.9.
- **3.11** — json.loads; options {"`parse_constant`":"`_reject_json_constant`"}; K3.10.
- **3.12** — json.dumps; options {}; K3.9.
- **3.13** — json.dumps; options {"indent":"2"}; K3.11.
- **3.14** — json.dumps; options {"indent":"2","`sort_keys`":"True"}; K3.9.
- **3.15** — json.dumps; options {"indent":"2"}; K3.9.
- **3.16** — json.dumps; options {"`ensure_ascii`":"False","indent":"2"}; K3.9.
- **3.17** — json.dumps; options {"default":"str","`sort_keys`":"True"}; K3.12.
- **3.18** — json.dumps; options {"default":"str","separators":"(',', ':')"}; K3.13.
- **3.19** — json.dumps; options {"`ensure_ascii`":"False"}; K3.9.
- **3.20** — json.loads; options {}; K3.14.
- **3.21** — json.dumps; options {"default":"str"}; K3.15.
- **3.22** — json.dumps; options {}; K3.16.
- **3.23** — json.dumps; options {}; K3.11.
- **3.24** — json.dumps; options {"default":"str"}; K3.11.
- **3.25** — json.dumps; options {"`sort_keys`":"True"}; K3.17.
- **3.26** — json.loads; options {"`object_pairs_hook`":"`_refuse_duplicate_json_keys`"}; K3.18.
- **3.27** — json.dump; options {"indent":"2","`sort_keys`":"True"}; K3.19.
- **3.28** — json.load; options {}; K3.20.
- **3.29** — json.dumps; options {"default":"str","indent":"2"}; K3.11.
- **3.30** — json.loads; options {"`object_pairs_hook`":"`_no_duplicate_keys`","`parse_constant`":"`_reject_constant`"}; K3.21.
- **3.31** — json.dumps; options {"default":"str","`sort_keys`":"True"}; K3.22.
- **3.32** — json.dumps; options {}; K3.23.
- **3.33** — json.dumps; options {}; K3.24.
- **3.34** — json.loads; options {}; K3.25.
- **3.35** — json.dumps; options {"`allow_nan`":"False","`ensure_ascii`":"False","separators":"(',', ':')"}; K3.26.
- **3.36** — json.dumps; options {"`allow_nan`":"False","`ensure_ascii`":"False"}; K3.26.
- **3.37** — json.dumps; options {"`ensure_ascii`":"False","indent":"2"}; K3.27.
- **3.38** — json.dumps; options {}; K3.28.
- **3.39** — json.dumps; options {"`ensure_ascii`":"False"}; K3.29.
- **3.40** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.30.
- **3.41** — json.dumps; options {}; K3.14.
- **3.42** — json.dumps; options {}; K3.31.
- **3.43** — json.dumps; options {}; K3.32.
- **3.44** — json.dumps; options {"`sort_keys`":"True"}; K3.33.
- **3.45** — json.dumps; options {"default":"repr","separators":"(',', ':')","`sort_keys`":"True"}; K3.34.
- **3.46** — orjson.dumps; options {}; K3.35.
- **3.47** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.36.
- **3.48** — json.dumps; options {"`ensure_ascii`":"False","separators":"(',', ':')"}; K3.37.
- **3.49** — json.JSONDecoder; options {}; K3.38.
- **3.50** — json.loads; options {}; K3.39.
- **3.51** — json.dumps; options {"`sort_keys`":"True"}; K3.40.
- **3.52** — json.loads; options {}; K3.41.
- **3.53** — json.dumps; options {}; K3.41.
- **3.54** — json.dumps; options {}; K3.42.
- **3.55** — json.dumps; options {"`ensure_ascii`":"False"}; K3.43.
- **3.56** — json.dumps; options {"default":"str","`ensure_ascii`":"False"}; K3.44.
- **3.57** — json.dumps; options {"`ensure_ascii`":"False"}; K3.44.
- **3.58** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.45.
- **3.59** — json.dumps; options {"`allow_nan`":"False","`ensure_ascii`":"False","separators":"(',', ':')"}; K3.46.
- **3.60** — json.dumps; options {"default":"str","`ensure_ascii`":"False"}; K3.47.
- **3.61** — json.dumps; options {"default":"str","`sort_keys`":"True"}; K3.48.
- **3.62** — json.JSONDecodeError; options {}; K3.49.
- **3.63** — json.dumps; options {"`ensure_ascii`":"False","separators":"(',', ':')","`sort_keys`":"True"}; K3.50.
- **3.64** — json.loads; options {}; K3.51.
- **3.65** — json.dumps; options {"default":"str"}; K3.52.
- **3.66** — json.dumps; options {}; K3.53.
- **3.67** — json.loads; options {}; K3.54.
- **3.68** — json.loads; options {}; K3.55.
- **3.69** — json.dumps; options {"`allow_nan`":"False","default":"`_json_serializer`"}; K3.56.
- **3.70** — json.dumps; options {}; K3.57.
- **3.71** — json.dumps; options {"default":"str","indent":"2","`sort_keys`":"True"}; K3.58.
- **3.72** — json.dumps; options {"default":"str","separators":"(',', ':')"}; K3.59.
- **3.73** — json.dumps; options {"separators":"(',', ':')"}; K3.59.
- **3.74** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.9.
- **3.75** — json.dumps; options {"`ensure_ascii`":"False","separators":"(',', ':')","`sort_keys`":"True"}; K3.60.
- **3.76** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.61.
- **3.77** — json.dumps; options {"`sort_keys`":"True"}; K3.61.
- **3.78** — json.dumps; options {"default":"str"}; K3.62.
- **3.79** — json.loads; options {}; K3.63.
- **3.80** — json.loads; options {}; K3.64.
- **3.81** — json.loads; options {}; K3.65.
- **3.82** — json.loads; options {}; K3.66.
- **3.83** — json.loads; options {}; K3.67.
- **3.84** — json.loads; options {"`object_pairs_hook`":"`_refuse_repeated_keys` if `unique_keys` else None"}; K3.68.
- **3.85** — json.loads; options {}; K3.69.
- **3.86** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.70.
- **3.87** — json.loads; options {}; K3.71.
- **3.88** — json.dumps; options {"indent":"2"}; K3.72.
- **3.89** — json.dumps; options {}; K3.73.
- **3.90** — json.dumps; options {"indent":"2","`sort_keys`":"True"}; K3.72.
- **3.91** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.73.
- **3.92** — json.dumps; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.74.
- **3.93** — json.dumps; options {"default":"`_json_default`","separators":"(',', ':')","`sort_keys`":"True"}; K3.74.
- **3.94** — json.loads; options {}; K3.75.
- **3.95** — json.dump; options {"separators":"(',', ':')","`sort_keys`":"True"}; K3.19.
- **3.96** — json.loads; options {}; K3.76.
- **3.97** — json.dumps; options {"default":"str","`sort_keys`":"True"}; K3.77.
- **3.98** — json.dumps; options {"`allow_nan`":"False"}; K3.78.
- **3.99** — json.dumps; options {"separators":"(',', ':')"}; K3.79.
- **3.100** — json.dumps; options {}; K3.80.
- **3.101** — json.loads; options {}; K3.81.
- **3.102** — json.dumps; options {}; K3.82.
- **3.103** — json.loads; options {}; K3.83.
- **3.104** — json.loads; options {}; K3.84.
- **3.105** — json.dumps; options {"`ensure_ascii`":"False","separators":"(',', ':')","`sort_keys`":"True"}; K3.85.
- **3.106** — json.dumps; options {}; K3.86.

## Non-call exception and type references

These references remain attached to their native exception/type contracts and are not extra serializer calls.

**Contracts**

- **K4.1** — Retain native exception/type identity and the enclosing catch or admission boundary.

**Operations**

- **4.1** — json.JSONDecodeError; K4.1.

## Baseline address index

Entries use `contract=lines`, or `line:column` for JSON calls. `U:` identifies unit rows; `×N` preserves occurrences. Ranges cover original spans only. All positions use the baseline.

- `lionagi/_flow_spec.py`: `3.1=77:19,86:23`.
- `lionagi/adapters/json_.py`: `3.2=124:15; 3.3=49:19; 4.1=32`.
- `lionagi/agent/context_fold.py`: `3.4=152:15; 3.5=161:19`.
- `lionagi/agent/factory.py`: `3.6=1277:17; 3.7=1146:19; 4.1=1147,1278`.
- `lionagi/agent/settings.py`: `3.8=251:26; 3.9=273:26`.
- `lionagi/cli/_argtypes.py`: `3.10=67:20`.
- `lionagi/cli/_context_from.py`: `3.10=99:23,129:22,150:23`.
- `lionagi/cli/_mcp_resolve.py`: `3.11=140:15; 4.1=141`.
- `lionagi/cli/_runs.py`: `3.10=313:15,412:23,541:23,564:19,709:11,756:25,1052:40,1468:26,1899:28,2136:28; 3.12=732:40,1057:45,1925:33; 3.13=106:21; 4.1=291,761`.
- `lionagi/cli/agent.py`: `3.10=200:23,617:19,1212:34,1218:34; 3.14=2504:14`.
- `lionagi/cli/dispatch.py`: `3.15=68:28`.
- `lionagi/cli/doctor.py`: `3.10=128:18; 3.15=298:14`.
- `lionagi/cli/engine.py`: `3.16=494:14; 3.17=627:8; 3.18=651:14,655:18; 3.19=371:26`.
- `lionagi/cli/hooks.py`: `3.10=240:15; 4.1=241`.
- `lionagi/cli/invoke.py`: `3.10=240:17; 4.1=241`.
- `lionagi/cli/kill.py`: `3.10=645:19,738:22`.
- `lionagi/cli/machine.py`: `3.10=178:25; 3.19=383:15; 4.1=179`.
- `lionagi/cli/machine_schedule.py`: `3.10=103:15,122:17; 3.20=352:21; 3.21=353:12; 3.22=55:11; 4.1=104,125`.
- `lionagi/cli/mirror.py`: `3.10=242:14,382:18,436:25,641:14; 3.23=290:11; 4.1=243,383,437,642`.
- `lionagi/cli/monitor.py`: `3.10=295:22,595:32,639:21,1245:24`.
- `lionagi/cli/orchestrate/_checkpoint.py`: `3.10=171:11; 3.24=165:18; 4.1=204`.
- `lionagi/cli/orchestrate/_control.py`: `3.10=96:19,106:19`.
- `lionagi/cli/orchestrate/_finalize.py`: `3.25=192:11`.
- `lionagi/cli/orchestrate/_manifest.py`: `3.26=187:18; 4.1=192`.
- `lionagi/cli/orchestrate/_notify.py`: `3.12=83:23,95:26`.
- `lionagi/cli/orchestrate/_orchestration.py`: `3.10=2147:28; 3.20=2150:24; 3.21=2150:35; 3.24=1802:25`.
- `lionagi/cli/orchestrate/_round_records.py`: `3.27=110:12; 3.28=128:20`.
- `lionagi/cli/orchestrate/fanout.py`: `3.10=71:18`.
- `lionagi/cli/orchestrate/flow.py`: `3.12=1188:65,2757:65`.
- `lionagi/cli/state.py`: `3.10=213:26,1160:27,1248:27`.
- `lionagi/cli/stats.py`: `3.12=203:14`.
- `lionagi/cli/status.py`: `3.10=523:23; 3.12=656:15,751:14`.
- `lionagi/cli/team.py`: `3.10=53:15,93:19; 3.29=101:21; 4.1=54`.
- `lionagi/config.py`: `3.30=106:20`.
- `lionagi/dispatch/outbox.py`: `3.10=275:25,315:27; 3.12=440:68`.
- `lionagi/engines/coding.py`: `3.12=211:46; 3.29=399:32; 3.31=375:36`.
- `lionagi/engines/engine.py`: `3.15=267:23`.
- `lionagi/engines/hypothesis.py`: `3.29=666:31`.
- `lionagi/hooks/builtins.py`: `3.12=142:38`.
- `lionagi/hooks/external.py`: `3.10=620:15; 3.12=686:25; 3.32=108:8; 3.33=138:24; 4.1=621`.
- `lionagi/integrations/ruvector/memory.py`: `3.34=76:19,119:18`.
- `lionagi/integrations/ruvector/store.py`: `3.10=233:37,244:24,254:58,265:49,276:50,277:60; 3.35=164:21; 3.36=351:24`.
- `lionagi/libs/schema/as_readable.py`: `3.37=183:36`.
- `lionagi/libs/schema/load_pydantic_model_from_schema.py`: `3.10=427:26; 3.38=470:28; 4.1=428`.
- `lionagi/libs/schema/minimal_yaml.py`: `2.1=75`.
- `lionagi/lndl/assembler.py`: `3.10=60:17`.
- `lionagi/mcp/_child_actor.py`: `3.39=209:16,210:26,211:33`.
- `lionagi/mcp/_notify_hook.py`: `3.10=255:21,319:18; 3.12=432:48; 4.1=256`.
- `lionagi/mcp/_run_detail.py`: `3.10=132:21,188:18; 4.1=189`.
- `lionagi/mcp/_terminal_cause.py`: `3.10=182:17; 3.12=161:32`.
- `lionagi/mcp/dispatch.py`: `3.10=963:19; 3.20=80:17; 3.40=208:11; 3.41=80:28; 3.42=302:30; 3.43=462:46; 3.44=362:13; 4.1=964`.
- `lionagi/mcp/jobs.py`: `3.10=563:17,607:19,625:15,1186:19,1966:18,1985:17; 3.13=400:23,431:20; 4.1=1187`.
- `lionagi/models/hashable_model.py`: `2.2=52,54; 2.3=68`.
- `lionagi/operations/flow.py`: `3.45=1366:26`.
- `lionagi/operations/schema/json_structure.py`: `3.46=70:22`.
- `lionagi/orchestration/patterns.py`: `3.15=97:30`.
- `lionagi/plugins/trust.py`: `3.47=61:16`.
- `lionagi/protocols/generic/element.py`: `2.4=158,160; 2.5=213`.
- `lionagi/protocols/generic/pile.py`: `3.48=49:12`.
- `lionagi/protocols/graph/node.py`: `2.1=41`.
- `lionagi/providers/_cli_subprocess.py`: `3.43=1322:25; 3.49=996:19; 4.1=1140,1150`.
- `lionagi/providers/_subagent_admission.py`: `3.50=461:17; 3.51=370:15`.
- `lionagi/providers/anthropic/claude_code.py`: `3.10=57:15; 3.43=628:20`.
- `lionagi/providers/anthropic/messages.py`: `3.52=842:24,938:32; 3.53=942:27`.
- `lionagi/providers/openai/codex.py`: `3.10=2910:18; 3.54=2781:15; 3.55=2669:15; 3.56=2759:19,2790:19; 3.57=2912:19`.
- `lionagi/providers/pi/cli.py`: `3.10=404:19; 4.1=403`.
- `lionagi/service/accounts/_ledger.py`: `3.58=165:11`.
- `lionagi/service/accounts/_pricing.py`: `3.30=368:18; 3.59=347:15`.
- `lionagi/service/connections/api_calling.py`: `3.60=342:11`.
- `lionagi/service/connections/endpoint.py`: `3.10=1085:12,1116:20; 4.1=827,1086,1117`.
- `lionagi/service/connections/mcp_wrapper.py`: `3.28=1727:23; 3.61=1581:15,1605:15; 3.62=1729:18; 4.1=1728`.
- `lionagi/state/_mirror_common.py`: `3.63=96:11; 3.64=248:17; 4.1=249`.
- `lionagi/state/claude_mirror.py`: `3.65=78:38`.
- `lionagi/state/codex_mirror.py`: `3.10=252:21,673:23; 3.65=380:15; 4.1=253`.
- `lionagi/state/content_pruned.py`: `3.10=71:22; 4.1=72`.
- `lionagi/state/db.py`: `3.10=3198:18,3297:26,3717:37,5043:33,7280:30,7407:33,7412:36,7464:43,7508:37,7556:31,7578:31,7605:35,7647:33,7861:35,7977:31; 3.66=4243:19,4248:15; 3.67=573:19; 3.68=8015:29; 4.1=7281,7408,7413,7465,7509,7557,7579,7606,7648,7862,7978`.
- `lionagi/state/engine.py`: `3.69=266:11`.
- `lionagi/state/lifecycle/notify_settings.py`: `3.70=473:18`.
- `lionagi/studio/cli.py`: `3.10=298:31,1241:19,1362:17,1436:28,1444:31,2179:28,2210:25; 3.12=1678:14,1692:14,1717:14; 3.14=1784:16,1871:16,1886:12; 3.15=1287:10; 3.22=1230:11; 3.71=1802:12; 4.1=1363`.
- `lionagi/studio/operator/application_mcp.py`: `3.10=1365:22; 3.72=563:14,640:20; 3.73=570:24; 3.74=1351:28,1370:29; 4.1=1366`.
- `lionagi/studio/operator/engine.py`: `3.10=755:17; 3.75=121:11; 4.1=756`.
- `lionagi/studio/operator/permission_mcp.py`: `3.10=204:22; 3.74=209:23; 3.76=161:36; 3.77=185:36; 4.1=205`.
- `lionagi/studio/operator/redact.py`: `3.78=345:19,365:15`.
- `lionagi/studio/operator/run_control.py`: `3.10=173:23,192:23`.
- `lionagi/studio/operator/store.py`: `3.10=409:23,421:23,536:12,987:15,1331:22,1483:23,1626:22,1864:26; 3.75=325:15; 3.79=415:17; 3.80=2480:23`.
- `lionagi/studio/scheduler/admit.py`: `3.10=114:21; 3.81=336:15; 3.82=135:20`.
- `lionagi/studio/scheduler/coordination.py`: `3.10=39:21,90:22`.
- `lionagi/studio/scheduler/worker.py`: `3.81=175:15; 3.83=550:34`.
- `lionagi/studio/services/_io.py`: `3.67=79:19; 3.84=56:16; 3.85=71:15; 4.1=72,80`.
- `lionagi/studio/services/admin.py`: `3.10=414:19,444:19`.
- `lionagi/studio/services/approvals.py`: `3.86=99:16,104:16`.
- `lionagi/studio/services/artifact_verification.py`: `3.10=34:23`.
- `lionagi/studio/services/engine_runs.py`: `3.10=24:19; 3.74=51:10; 3.87=64:18; 4.1=25,76`.
- `lionagi/studio/services/hooks_library.py`: `3.10=91:18; 3.88=102:29; 4.1=92`.
- `lionagi/studio/services/invocations.py`: `3.10=103:28,276:28; 4.1=104,277`.
- `lionagi/studio/services/leo.py`: `3.89=333:20`.
- `lionagi/studio/services/mcp_servers.py`: `3.90=156:24,172:31`.
- `lionagi/studio/services/operator.py`: `3.91=298:34`.
- `lionagi/studio/services/operator_hooks.py`: `3.10=84:31; 3.88=94:28,121:30; 4.1=85`.
- `lionagi/studio/services/retention_archive.py`: `3.64=262:23,297:36,309:19; 3.92=199:29; 3.93=130:16; 4.1=282`.
- `lionagi/studio/services/run_resume.py`: `3.10=178:27,480:27; 3.94=210:21; 3.95=285:12; 4.1=179,424,481`.
- `lionagi/studio/services/run_resume_worker.py`: `3.96=29:16`.
- `lionagi/studio/services/schedule_declaration.py`: `3.97=403:24`.
- `lionagi/studio/services/schedules.py`: `2.6=818; 2.7=802; 3.68=660:22; 3.98=901:12`.
- `lionagi/studio/services/sessions.py`: `3.10=75:15; 3.100=909:9,929:9; 3.101=1304:30; 3.87=670:18,716:18; 3.89=1766:30,1841:35; 3.99=663:10,698:10; 4.1=76,1305`.
- `lionagi/studio/services/shows.py`: `3.10=297:34; 3.89=741:23,744:23,768:27,797:31`.
- `lionagi/studio/services/signals.py`: `3.10=49:26; 4.1=50`.
- `lionagi/studio/services/workflow_compile.py`: `3.65=532:56,536:11`.
- `lionagi/testing/_endpoint.py`: `3.102=185:49,276:49,290:37`.
- `lionagi/testing/_script.py`: `3.28=111:19`.
- `lionagi/testing/loaders.py`: `3.28=50:19`.
- `lionagi/tools/code/ast_search.py`: `3.103=122:22,126:27; 4.1=123,127`.
- `lionagi/tools/code/check.py`: `3.104=182:20; 4.1=183`.
- `lionagi/tools/context/context.py`: `3.105=38:11`.
- `lionagi/tools/khive_injection.py`: `3.106=272:26,462:43,463:52,501:35,514:45,515:27,516:40,535:41; 3.67=122:15`.
