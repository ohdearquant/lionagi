# Imports, copying and hashing

Issue #3583 baseline: `ee5783cc689fbff53afef49f772e6584121b70e6`. Positions use this revision. Changed entries are implemented; focused validation passed. KEEP rows describe source contracts.

Reference: `ee5783cc689fbff53afef49f772e6584121b70e6`. The source census contains 34 candidate expressions: 33 real native operations and one excluded local-list append. Of 17 native module imports, two swallow selected import failures and now use module-only shared loading (implemented, focused validation passed); the other 15 preserve caller-visible native diagnostics.

| Source | Current treatment | Reason|
|---|---|---|
| `lionagi/_auto.py:393` (`load_cli_command`) | Retain native | CLI realization checks its cache before importing the seed module. A failing import propagates through `build_cli_parser` before `parser_factory`; cli/main.py354–370 distinguishes ModuleNotFoundError using exc.name and renders every other error as type(exc).`__name__` plus str(exc). The shared helper preserves missing-module identity but wraps ordinary ImportError/custom subclasses and changes the command failure text/metadata. Keep the native load and successful-only cache/marker validation.|
| `lionagi/_auto.py:359` (`load_http_modules`) | Retain native | HTTP realization imports the fixed `_HTTP_MODULES` sequence then compiles each module marker. There is no selected-import error translator; an ordinary ImportError/custom subclass propagates unchanged and stops later module realization. Prefixing/wrapping it would change the startup failure, even though missing-module identity is preserved by the helper.|
| `lionagi/_class_registry.py:146` (`get_class`) | Implemented, focused validation passed | Only this built-in-module loop catches ImportError and continues, without displaying or assigning the skipped error to `load_error`. Use the existing module-only helper; retain getattr/class-origin checks, lookup order and the independent earlier dotted-load ValueError/cause chain. Never use helper selector mode or newly attach skipped exceptions.|
| `lionagi/agent/settings.py:208` (`_import_hook`) | Implemented, focused validation passed | The trusted hook module is admitted before loading and selected ImportError/AttributeError returns None. Reuse a function-local helper import inside the existing try after exact trust/colon checks, module-only load then existing getattr. Empty/missing attributes must not trigger package-child execution; no new wrapper or callable admission.|
| `lionagi/casts/catalog.py:22` (`_load_packaged_pack`) | Retain native | The packaged default YAML is handed to `Pack.from_file` only inside `as_file`. That context manager owns temporary extraction for non-filesystem resources; a path read/import helper does not preserve its materialized-file lifetime, and `raise_on_error` chooses propagation versus None.|
| `lionagi/casts/catalog.py:21` (`_load_packaged_pack`) | Retain native | files("lionagi.casts") returns a package Traversable used to join packs/default.yaml. It supports installed/zip resources without assuming a real filesystem path; ln module import/file readers do not provide that contract. The enclosing optional/raising catalogue policy remains unchanged.|
| `lionagi/cli/_code_identity.py:209` (`_worktree_fingerprint`) | Retain native | The seeded SHA256 object is updated with NUL-delimited diff bytes and per-path stat markers while spending the existing Git identity budget. It returns a truncated hexdigest only after all reads succeed; replacing it with one-shot `compute_hash`/`hash_file` requires new materialization and changes the incremental/budget failure boundary.|
| `lionagi/cli/_code_identity.py:449` (`_distribution_version`) | Retain native | The installed distribution version is read without importing lionagi, with a distinct PackageNotFoundError source-run diagnosis and type/string metadata failure text. Importing a module and reading `__version__` is not the installed-metadata operation.|
| `lionagi/cli/doctor.py:99` (`_check_imports`) | Retain native | Doctor probes each `_IMPORT_PROBES` module and displays type(exc).`__name__` plus str(exc) in its fail detail. ln wraps ordinary ImportError subclasses and prefixes text, changing the actual reported broken link. Keep this explicitly diagnostic native import and its existing monkeypatch seam.|
| `lionagi/cli/doctor.py:111` (`_check_core_deps`) | Retain native | Doctor probes the concrete import name for each `_CORE_DEPS` dependency and formats the native class/message into the dependency health result. An ordinary ImportError wrapper changes that diagnosis; ModuleNotFoundError retaining identity alone is insufficient. Preserve the direct probe and all dependency rows.|
| `lionagi/cli/engine.py:59` (`_import_engine_class`) | Retain native | The helper returns the selected engine class via an existing getattr. cli/engine.py340–361 logs str(exc), persists error=str(exc), records `import_failure`, closes its DB and returns1; `workflow_compile554`–560 also propagates this load. The ordinary shared-import prefix would therefore alter durable/logged errors; no own import-error translation currently exists.|
| `lionagi/cli/machine.py:822` (`_run_machine_command`) | Retain native | The relative native load invokes `machine_result`(rest) only after the handler/module-map fast paths. `dispatch_machine` distinguishes missing modules before/after run allocation; other exceptions become a credential-masked type-and-message envelope at791–806. Wrapping ordinary ImportError changes that envelope, so retain native relative resolution and dispatch timing.|
| `lionagi/cli/main.py:25` (`_load_orchestrate`) | Retain native | The orchestrate sibling is loaded lazily with the existing positional native package anchor. Its import failure reaches main496–516: missing-before-allocation is an environment78 diagnosis, missing-after-allocation re-raises, and ordinary import errors retain original text. Do not widen a catch or add a translator merely to use the shared helper.|
| `lionagi/cli/main.py:29` (`_load_machine`) | Retain native | The machine sibling is loaded before `dispatch_machine`, with native package anchoring and failure propagation to the allocation-aware main wrapper. The helper would change ordinary ImportError class/text; keep the original lazy load, module identity and timing.|
| `lionagi/cli/main.py:33` (`_load_studio`) | Retain native | The absolute Studio CLI load occurs at quick-create/dispatch demand and propagates native failure to the same invocation wrapper. Shared ordinary-error wrapping is caller-visible; keep the absolute native import and no eager loading.|
| `lionagi/cli/main.py:37` (`run_skill`) | Retain native | The skill sibling is loaded only when `run_skill` is invoked, then its `run_skill`(argv) executes separately. Native import failure is not translated here and reaches the outer allocation-aware entry point; shared wrapping would change the error class/text. Preserve sibling anchor and invocation order.|
| `lionagi/cli/orchestrate/_orchestration.py:249` (`_default_pack`) | Retain native | The lazy default-pack cache is populated from `Pack.from_file` while an `as_file` extraction context is alive, and any failure caches None. Existing resource extraction/cleanup ownership cannot be replaced by an ordinary module import or file helper.|
| `lionagi/cli/orchestrate/_orchestration.py:248` (`_default_pack`) | Retain native | The orchestration default pack locates packs/default.yaml through the package Traversable, including zipped distributions, before entering `as_file`. Preserve this resource lookup and failure-to-None cache behavior rather than assume a disk directory.|
| `lionagi/cli/orchestrate/_orchestration.py:1736` (`collect_worker_artifacts`) | Excluded: local list operation | `collect_worker_artifacts` declares local files: list[str] = [] before this call. files.append adds a relative artifact path to that local list; it does not refer to the separately imported importlib.resources.files function. Exclude from native-import candidate counts and do not migrate it.|
| `lionagi/libs/schema/load_pydantic_model_from_schema.py:354` (`_load_via_codegen`) | Retain native | The generated Python model file lives in a TemporaryDirectory outside sys.path. `spec_from_file_location` binds its selected deterministic module name to that exact file; missing file/spec/loader has existing FileNotFoundError/ImportError behavior. A dotted-name helper cannot load this file-location contract.|
| `lionagi/libs/schema/load_pydantic_model_from_schema.py:359` (`_load_via_codegen`) | Retain native | `module_from_spec` creates the generated module object before its selected `spec.loader.exec_module` call. Code-execution failures become the existing RuntimeError from the original exception. Ordinary importing by name would change file ownership, caching and loader execution behavior.|
| `lionagi/mcp/projection.py:526` (`_orchestrate`) | Retain native | The orchestrate module is loaded lazily for parser/path resolution. Import errors propagate outside playbook-resolution errors; the nearby OSError catch covers `hash_file` only. Shared ordinary ImportError wrapping would change this external failure and must not be normalized into playbook-not-found.|
| `lionagi/protocols/generic/pile.py:544` (`Pile.__deepcopy__`) | Retain native | `Pile.__deepcopy__` copies non-lock private state using the same caller-supplied memo as its public state, then creates fresh sync/async locks. ln.copy has no memo parameter; separate deep copies would break alias/cycle identity across the two state dictionaries.|
| `lionagi/protocols/generic/pile.py:539` (`Pile.__deepcopy__`) | Retain native | `Pile.__deepcopy__` registers result in memo before copying every `__dict__` value with that memo. This preserves cycles and shared references including later private-state copies. ln.copy cannot accept that memo graph; retaining the native call is required.|
| `lionagi/service/accounts/_pricing.py:413` (`quote_ceiling`) | Retain native | `quote_ceiling` calls `_parse`(body), whose json.loads accepts valid str, bytes or bytearray and has no exact-bytes guard. Native hashlib.sha256(body) then rejects str and accepts buffers; `compute_hash` would encode str or serialize bytearray/nonbytes through JSON instead. `freeze_request` normally returns bytes but the quote entry point does not enforce that domain. Preserve these visible admission/refusal results and the exact SHA256 body digest; no claim that exact bytes would produce a different hexdigest.|
| `lionagi/service/connections/endpoint.py:66` (`_pinned_ssl_context._PinnedSSLObject.do_handshake`) | Retain native | TLS pinning compares a 32-byte SHA256.digest() of the DER peer certificate to the caller-provided fingerprint. `compute_hash` returns hexadecimal text and accepts/serializes a broader object domain; there is no existing raw-buffer-digest helper with the same binary comparison/refusal interface.|
| `lionagi/service/connections/provider_config.py:30` (`LazyType.resolve`) | Retain native | LazyType resolves the module and getattr only on first use, assigns `_resolved` only after success and retries after failures. ProviderConfig.options directly propagates that failure. Ordinary shared-import wrapping would change class/text/name/path visible to the property caller; preserve this cache/error boundary.|
| `lionagi/service/connections/registry.py:625` (`_import_provider_module`) | Retain native | After explicit optional-dependency preflight, an actual module-body ImportError is warned with %s e and bootstrap continues. The debug path for a declared missing dependency is separate. Shared prefix/wrapping changes this warning; preserve native import and do not infer suppression from forged error metadata.|
| `lionagi/state/lifecycle/notify_settings.py:586` (`_make_python_handler`) | Retain native | The module/getattr failure reaches `build_handler616`–630, which logs the original exception, invokes the stable `python_adapter_unresolvable` callback and returns None. The constant callback reason does not make a changed warning equivalent. Preserve callable checks, registration/deadline handling and native import diagnostics.|
| `lionagi/studio/config.py:131` (`_named_zone_rules`) | Retain native | After a configured disk zone-file path is absent, timezone lookup walks tzdata.zoneinfo resources and may open a non-Path ZIP member with a bounded read into `tzif_rules`. Existing archive/I/O/import/refusal classes are caught. A normal filesystem/module helper cannot reproduce that Traversable/resource-stream behavior.|
| `lionagi/studio/launch_access.py:298` (`_digest`) | Retain native | The capability store uses the returned 32-byte digest as dictionary identity and capability.digest, not a textual fingerprint. `compute_hash` returns hexadecimal text and has broader input semantics; retaining the native bytes avoids changing credential-key representation or introducing a conversion layer.|
| `lionagi/studio/registry.py:129` (`load_studio_route_modules`) | Retain native | Studio mounts routes only after the fixed ordered area imports execute their decorators. There is no catch around `load_studio_route_modules` in `_mount_studio_routes451`–456, so native ImportError class/text propagates at startup. Shared ordinary-error wrapping changes that failure rather than merely internal implementation.|
| `lionagi/studio/scheduler/subprocess.py:495` (`resolve_li_executable`) | Retain native | The resolver reads `console_scripts` metadata only after checking an absolute interpreter and adjacent li executable. It selects the registered li module without importing entry points, and records exact metadata exception class/text on failure. Importing a module is not this installed-distribution discovery and would change startup effects/order.|
| `lionagi/studio/services/retention_archive.py:125` (`_write_member_stream`) | Retain native | The archive member hasher is updated with each exact encoded JSONL row immediately after that row is written to the ZIP stream, then returns count and hexdigest. Peak memory remains one row and partial failures preserve cleanup. A one-shot helper would require whole-member materialization or a new stream-hash interface.|

The eleven original no-memo copies, original hash/type/lazy migrations and the H1–H7 helper foundations are already present. Native memo-aware graph copies, streamed multi-input hashes, binary SHA256 results, resource lifetimes, installed metadata and file-location module factories retain distinct contracts. Native ordinary ImportError diagnostics remain unchanged; ModuleNotFoundError preservation by the shared helper does not make its ordinary-error wrapper equivalent.

## Complete issue units

| Unit | Contract|
| --- | ---|
| 1 | All eleven original no-memo copies already shared; retain the two actual memo-aware Pile copies.|
| 2 | Existing calls preserve original strings, truncation and `max_bytes`=None where uncapped; retain incremental and raw-binary consumers, with exact per-call reasons.|
| 3 | H1-H4 and original byte/file/fd/BLAKE2b migrations exist. Keep incremental objects, raw32byte outputs and pricing buffer-admission semantics.|
| 4 | H5/H6 nominal checks/Optional unwrapping exist. Generic list/dict introspection and repr/structural-model contracts remain explicitly distinct; additional source keeps are enumerated.|
| 5 | CLI/state lazy loading and provider availability moved; settings uses a module-only loader, implemented, focused validation passed. Resources, installed metadata and file-location specs retain specific ownership contracts.|
| 6 | Current H7 already supplies leaf/relative/list/empty/MNF contracts. Two module-only loaders are implemented, focused validation passed; thirteen selected native keeps and two additional doctor keeps preserve ordinary error diagnostics.|
| 7 | CLI/schedule/plugin canonical JSON bytes stay unchanged before hashing (#3699/#3622); generated module naming already uses deterministic uncapped hash under #3719. No old salted-hash requirement is reopened.|
| 8 | The actual logged/persisted/propagated errors retain native class/text/name/path; no translator or helper-prefix policy change is proposed.|
| 9 | Keep isinstance(type)+hasattr(`model_fields`) and local multi-member union preference; nominal BaseModel checks would change admitted values/rendering.|

## Already-shared source locations

**Contracts**

- **K3.1** — Already shared; retain the existing callable, exact arguments, result/error behavior and enclosing consumer. The index identifies each baseline call.

**Operations**

- **3.1** — copy; #3601; K3.1.
- **3.2** — copy; #3661; K3.1.
- **3.3** — `compute_hash`; #3622; K3.1.
- **3.4** — `hash_file`; #3622; K3.1.
- **3.5** — Existing definition `_hash_fd`; #3622; K3.1.
- **3.6** — `hash_fd`; #3622; K3.1.
- **3.7** — `_hash_fd`; #3622; K3.1.
- **3.8** — `is_pydantic_model_class`; #3622; K3.1.
- **3.9** — `_unwrap_optional`; #3622; K3.1.
- **3.10** — Existing definition `_unwrap_optional`; #3622; K3.1.
- **3.11** — `unwrap_optional`; #3622; K3.1.
- **3.12** — `is_union_type`; #3622; K3.1.
- **3.13** — `lazy_import`; #3622; K3.1.
- **3.14** — `is_import_installed`; #3622; K3.1.
- **3.15** — `import_module`; #3725; K3.1.

## Additional named contracts

| Baseline source | Consumer | KEEP reason|
| --- | --- | ---|
| `lionagi/studio/services/approvals.py:108` | `_compute_chain_hash` | Keep concatenated `content_hash`+`previous_hash` and its existing zero-valued genesis; `ln.compute_chain_hash` uses a colon and GENESIS, so it is not the same chain.|
| `lionagi/studio/services/approvals.py:112` | `_compute_hmac_sig` | Keep native keyed HMAC over `chain_hash`+`created_at` UTF8 bytes with hashlib.sha256 passed as constructor. It is not an unkeyed `compute_hash` operation; the constructor reference is not another native digest call.|
| `lionagi/lndl/assembler.py:46` | `_is_model_cls` | Keep structural class-with-`model_fields` admission; a non-BaseModel class can satisfy it, unlike the nominal shared Pydantic helper.|
| `lionagi/lndl/assembler.py:114` | `_unwrap_optional` | Keep local multi-member union preference for the first structural model class, else first non-None member. Shared `unwrap_optional` deliberately leaves unions with multiple non-None members unchanged.|
| `lionagi/operations/lndl_middle/lndl_middle.py:55` | `_render_type` | Keep generic list/dict `get_origin`/`get_args` and structural `model_fields` formatting, including classes that are not BaseModel subclasses.|
| `lionagi/models/field_model.py:338` | `__repr__` | Keep the PEP604 UnionType-specific repr branch and existing typing.Union alternative; `is_union_type` would merge distinct formatting paths.|
| `lionagi/libs/schema/breakdown_pydantic_annotation.py:8` | `breakdown_pydantic_annotation` | Keep list generic origin/element extraction. The nominal Pydantic predicate already moved; union helpers are not substitutes for list arguments.|

## Existing helper foundations

| Baseline source | Helper | Compatibility|
| --- | --- | ---|
| `lionagi/ln/_hash.py:261` | HashAlgorithm | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_hash.py:279` | `compute_hash` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_hash.py:307` | `hash_file` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_hash.py:323` | `hash_fd` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_utils.py:290` | `import_module` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_utils.py:519` | copy | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_utils.py:551` | `is_pydantic_model_class` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_utils.py:563` | `is_union_type` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_utils.py:579` | `union_members` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_utils.py:595` | `unwrap_optional` | Existing signature, values, admission and errors remain unchanged.|
| `lionagi/ln/_lazy_init.py:37` | `lazy_import` | Existing signature, values, admission and errors remain unchanged.|

## Excluded alias

| Baseline source | Expression | Exclusion reason|
| --- | --- | ---|
| `lionagi/cli/orchestrate/_orchestration.py:1736` | files.append(str(`path.relative_to`(adir))) | `collect_worker_artifacts` declares local files: list[str] = [] before this call. files.append adds a relative artifact path to that local list; it does not refer to the separately imported importlib.resources.files function. Exclude from native-import candidate counts and do not migrate it.|

## Baseline address index

Entries use `contract=lines`, or `line:column` for JSON calls. `U:` identifies unit rows; `×N` preserves occurrences. Ranges cover original spans only. All positions use the baseline.

- `lionagi/_types.py`: `3.15=17`.
- `lionagi/casts/pattern.py`: `3.15=64,75,167,173`.
- `lionagi/cli/__init__.py`: `3.13=14`.
- `lionagi/cli/_runs.py`: `3.2=1814`.
- `lionagi/cli/mirror.py`: `3.3=388`.
- `lionagi/cli/orchestrate/_manifest.py`: `3.3=333`.
- `lionagi/engines/review.py`: `3.3=414,423`.
- `lionagi/hooks/builtins.py`: `3.1=275`.
- `lionagi/hooks/external.py`: `3.3=138; 3.4=168; 3.5=236; 3.6=240; 3.7=274`.
- `lionagi/libs/schema/breakdown_pydantic_annotation.py`: `3.8=13,22,26`.
- `lionagi/libs/schema/load_pydantic_model_from_schema.py`: `3.8=367`.
- `lionagi/libs/validate/common_field_validators.py`: `3.8=89`.
- `lionagi/lndl/assembler.py`: `3.10=114; 3.9=93,138,141,227,327,368`.
- `lionagi/lndl/types.py`: `3.11=130`.
- `lionagi/mcp/dispatch.py`: `3.3=209`.
- `lionagi/mcp/jobs.py`: `3.1=513`.
- `lionagi/mcp/projection.py`: `3.4=545`.
- `lionagi/models/_build_model.py`: `3.8=34`.
- `lionagi/models/field_model.py`: `3.12=68`.
- `lionagi/operations/lndl_middle/lndl_middle.py`: `3.11=57`.
- `lionagi/operations/operate/operate.py`: `3.8=208`.
- `lionagi/operations/parse/parse.py`: `3.8=206,351`.
- `lionagi/operations/select/utils.py`: `3.8=37,85`.
- `lionagi/plugins/_user_settings.py`: `3.1=169`.
- `lionagi/plugins/registry.py`: `3.3=403,642`.
- `lionagi/plugins/trust.py`: `3.3=41; 3.4=53`.
- `lionagi/protocols/action/tool_hooks.py`: `3.1=103`.
- `lionagi/protocols/generic/event.py`: `3.1=356,362`.
- `lionagi/protocols/generic/pile.py`: `3.12=105,1036`.
- `lionagi/protocols/messages/instruction.py`: `3.8=186,280`.
- `lionagi/protocols/messages/message.py`: `3.1=491`.
- `lionagi/protocols/messages/prepare.py`: `3.1=128`.
- `lionagi/service/connections/endpoint_config.py`: `3.8=154`.
- `lionagi/service/connections/registry.py`: `3.14=615; 3.3=443; 3.4=436`.
- `lionagi/service/token_budget.py`: `3.15=47`.
- `lionagi/state/__init__.py`: `3.13=41`.
- `lionagi/state/_mirror_common.py`: `3.3=100,244`.
- `lionagi/studio/operator/engine.py`: `3.3=265`.
- `lionagi/studio/operator/store.py`: `3.3=375`.
- `lionagi/studio/services/approvals.py`: `3.3=100,105,109`.
- `lionagi/studio/services/mcp_servers.py`: `3.1=538`.
- `lionagi/studio/services/playbooks.py`: `3.4=106`.
- `lionagi/studio/services/retention_archive.py`: `3.3=270`.
- `lionagi/studio/services/run_resume_worker.py`: `3.3=53`.
- `lionagi/testing/_endpoint.py`: `3.1=225`.
